//! 格納データを NVMe（SSD）に置いたときの速さとメモリを測る（docs/out-of-core.md の「測定」）。
//!
//!     cargo run --release -p nanashi-engine --example out_of_core --manifest-path native/Cargo.toml -- [セル数] [置き場所]
//!
//! エンジンの本体は使わず、Store の本体と同じ形（昇順の u64 のキーと f64 の値の 2 列）のファイルを作り、
//! 次の持ち方で、エンジンの主な操作に当たる処理を測る。macOS と Linux で動く。
//!
//! - heap: 今のエンジン。2 列をヒープに読み込み、エンジンの本体と同じ型（不変の平らな列 Column）で持つ
//! - mmap 暖 / 冷: ファイルを読み出し専用で写像し、ページの出し入れを OS に任せる。
//!   暖はページキャッシュに載った状態、冷は処理の前にページキャッシュから追い出した状態
//! - pread: ページキャッシュに残さずに、決まった数のセルずつ読み込む。
//!   メモリには、16 KB ごとの先頭のキー（フェンス）だけを持つ
//!
//! ページキャッシュに残さない方法は OS で違う。
//!
//! - macOS: ファイルに F_NOCACHE を付ける。冷の写像は msync(MS_INVALIDATE) で追い出す
//! - Linux: 読んだ範囲と書き終えたファイルを posix_fadvise(POSIX_FADV_DONTNEED) で追い出す
//!   （書いたものは sync_file_range でデバイスへ送ってから）。冷の写像は madvise(MADV_DONTNEED) で外してから、
//!   同じように追い出す。O_DIRECT は、バッファと位置を 4 KB にそろえる必要があるので使わない
//!
//! 処理:
//! - scan: 全セルを読む
//! - agg: 2 つの軸（部署 × 月に当たる、12 万グループ）へ、集計先の配列に読みながら足し込む
//! - merge: 同じ軸の 2 つの Metric を足す（キー順の突き合わせ）。heap は結果を Vec に、ほかはファイルに書き出す
//! - lookup: キーを 1 つずつ引く（get、転置索引、ハッシュ結合の探索のような飛び飛びの読み出し）
//! - rekey: 軸の順番を変えて並べ直す。heap はメモリ内で並べ替え、ほかは外部ソート
//!   （決まった量ずつ並べてファイルに書き、キーの範囲ごとに並列に併合する）
//!
//! ディスクから実際に読んだ量（macOS は ri_diskio_bytesread、Linux は /proc/self/io の read_bytes）も出すので、
//! 冷の測定が本当にディスクを読んだかを確かめられる。

use nanashi_engine::Column;
use rayon::prelude::*;
use std::alloc::{GlobalAlloc, Layout, System};
use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::fs::{self, File};
use std::io::{BufWriter, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
use std::time::Instant;

/// ヒープの使用量と、その最大を数えるアロケーター。
struct Counting;

static HEAP: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);

fn grew(n: usize) {
    let now = HEAP.fetch_add(n, Relaxed) + n;
    PEAK.fetch_max(now, Relaxed);
}

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        grew(l.size());
        System.alloc(l)
    }
    unsafe fn alloc_zeroed(&self, l: Layout) -> *mut u8 {
        grew(l.size());
        System.alloc_zeroed(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        HEAP.fetch_sub(l.size(), Relaxed);
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        grew(new);
        HEAP.fetch_sub(l.size(), Relaxed);
        System.realloc(p, l, new)
    }
}

#[global_allocator]
static ALLOC: Counting = Counting;

/// 再現できる擬似乱数（xorshift64*）。
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
}

/// 軸のメンバー数（社員、商品、部門、月のような 4 軸）と、キーの中のビットの位置。
const SIZES: [u64; 4] = [2000, 1000, 100, 60];
const SHIFT: [u32; 4] = [23, 13, 6, 0];
/// 集計先（軸 0 × 軸 3）のグループ数。
const GROUPS: usize = 2000 * 60;
/// フェンスの間隔。キー 2,048 個で 16 KB（Apple Silicon のページ 1 枚、Linux の x86 ではページ 4 枚）。
const PAGE: usize = 2048;
/// 1 回に読むセル数（キーと値で 16 MB）。PAGE の倍数。
const BLOCK: usize = 1 << 20;
/// 外部ソートで 1 回に並べるセル数（128 MB）。
const RUN: usize = 8 << 20;

fn pack(mut linear: u64) -> u64 {
    let mut key = 0;
    for a in (0..4).rev() {
        key |= (linear % SIZES[a]) << SHIFT[a];
        linear /= SIZES[a];
    }
    key
}

fn group(k: u64) -> usize {
    (k >> SHIFT[0]) as usize * 60 + (k & 63) as usize
}

/// 軸 3 を先頭に動かしたキー（rekey）。
fn rekey(k: u64) -> u64 {
    ((k & 63) << 28) | (k >> 6)
}

// ---- ファイル ----

/// 開いたファイルを、ページキャッシュに残さない読み書きにする（macOS）。
/// Linux では何もせず、読み書きのあとで read_at と settle が追い出す。
fn nocache(f: &File) {
    #[cfg(target_os = "macos")]
    unsafe {
        libc::fcntl(f.as_raw_fd(), libc::F_NOCACHE, 1)
    };
    #[cfg(not(target_os = "macos"))]
    let _ = f;
}

/// [off, off + len) を含むページを、ページキャッシュから追い出す（Linux）。
#[cfg(target_os = "linux")]
fn fadvise_dontneed(f: &File, off: u64, len: u64) {
    let lo = off & !4095;
    let hi = (off + len).next_multiple_of(4096);
    unsafe { libc::posix_fadvise(f.as_raw_fd(), lo as i64, (hi - lo) as i64, libc::POSIX_FADV_DONTNEED) };
}

/// off から buf の長さだけ読む。Linux では読んだページをページキャッシュから追い出す。
fn read_at(f: &File, buf: &mut [u8], off: u64) {
    f.read_exact_at(buf, off).unwrap();
    #[cfg(target_os = "linux")]
    fadvise_dontneed(f, off, buf.len() as u64);
}

/// 書き終えたファイルを、ページキャッシュに残さない（Linux）。デバイスへ送り終えるのを待ってから追い出す。
/// sync_file_range はデバイスのキャッシュの書き出しまでは命じないので、macOS の F_NOCACHE の書き込みに近い。
fn settle(f: &File) {
    #[cfg(target_os = "linux")]
    unsafe {
        let flags = libc::SYNC_FILE_RANGE_WAIT_BEFORE | libc::SYNC_FILE_RANGE_WRITE | libc::SYNC_FILE_RANGE_WAIT_AFTER;
        libc::sync_file_range(f.as_raw_fd(), 0, 0, flags);
        libc::posix_fadvise(f.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED);
    }
    #[cfg(not(target_os = "linux"))]
    let _ = f;
}

fn create(path: &Path) -> BufWriter<File> {
    let f = File::create(path).unwrap();
    nocache(&f);
    BufWriter::with_capacity(8 << 20, f)
}

/// data を 1 つのファイルに書く。途中結果なので fsync はしない（落ちたら計算し直せばよい）。
fn write_file(path: &Path, data: &[u8]) {
    let f = File::create(path).unwrap();
    nocache(&f);
    (&f).write_all(data).unwrap();
    settle(&f);
}

fn bytes<T>(s: &[T]) -> &[u8] {
    unsafe { std::slice::from_raw_parts(s.as_ptr() as *const u8, std::mem::size_of_val(s)) }
}

fn bytes_mut<T>(s: &mut [T]) -> &mut [u8] {
    unsafe { std::slice::from_raw_parts_mut(s.as_mut_ptr() as *mut u8, std::mem::size_of_val(s)) }
}

/// 1 つの Metric の 2 列のファイルの置き場所。
struct Paths {
    keys: PathBuf,
    vals: PathBuf,
}

impl Paths {
    fn new(dir: &Path, name: &str) -> Paths {
        Paths { keys: dir.join(format!("{name}.keys")), vals: dir.join(format!("{name}.vals")) }
    }
}

struct Writer {
    keys: BufWriter<File>,
    vals: BufWriter<File>,
    n: usize,
    fence: Vec<u64>,
}

impl Writer {
    fn new(p: &Paths) -> Writer {
        Writer { keys: create(&p.keys), vals: create(&p.vals), n: 0, fence: Vec::new() }
    }
    fn push(&mut self, k: u64, v: f64) {
        if self.n.is_multiple_of(PAGE) {
            self.fence.push(k);
        }
        self.keys.write_all(&k.to_ne_bytes()).unwrap();
        self.vals.write_all(&v.to_ne_bytes()).unwrap();
        self.n += 1;
    }
    /// 書き終え、ディスクまで書き出す（std の sync_data は macOS では F_FULLFSYNC、Linux では fdatasync になる）。
    fn finish(self) -> (usize, Vec<u64>) {
        for w in [self.keys, self.vals] {
            let f = w.into_inner().unwrap();
            f.sync_data().unwrap();
            settle(&f);
        }
        (self.n, self.fence)
    }
}

/// 測るデータ。A と B は同じ軸の 2 つの Metric で、B は A の 8 割のキーに、隣のキーを足したもの。
struct Data {
    a: Paths,
    b: Paths,
    n_a: usize,
    n_b: usize,
    fence_a: Vec<u64>,
    fence_b: Vec<u64>,
    /// 引くキー（半分は A にあるキー、半分はほぼ A にないキー）。順番は混ぜてある。
    probes: Vec<u64>,
}

fn generate(dir: &Path, n: usize) -> Data {
    let space: u64 = SIZES.iter().product();
    let gap = space / n as u64;
    assert!(gap >= 4, "セル数が多すぎる");
    let (pa, pb) = (Paths::new(dir, "a"), Paths::new(dir, "b"));
    let (mut wa, mut wb) = (Writer::new(&pa), Writer::new(&pb));
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
    let mut probes = Vec::new();
    let stride = (n / 500_000).max(1);
    let mut cur = rng.next() % gap;
    for i in 0..n {
        let next = cur + 1 + rng.next() % (2 * gap - 1);
        let v = (rng.next() % 1000) as f64;
        wa.push(pack(cur), v);
        let r = rng.next() % 5;
        if r != 0 {
            wb.push(pack(cur), v * 0.5);
        }
        if r <= 1 && cur + 1 < next {
            wb.push(pack(cur + 1), v * 0.25);
        }
        if i % stride == 0 {
            probes.push(pack(cur));
            probes.push(pack(rng.next() % space));
        }
        cur = next;
    }
    for i in (1..probes.len()).rev() {
        probes.swap(i, (rng.next() % (i as u64 + 1)) as usize);
    }
    let (n_a, fence_a) = wa.finish();
    let (n_b, fence_b) = wb.finish();
    Data { a: pa, b: pb, n_a, n_b, fence_a, fence_b, probes }
}

/// 読み出し専用の写像。
struct Map {
    ptr: *mut libc::c_void,
    len: usize,
    file: File,
}

unsafe impl Send for Map {}
unsafe impl Sync for Map {}

impl Map {
    fn open(path: &Path) -> Map {
        let f = File::open(path).unwrap();
        let len = f.metadata().unwrap().len() as usize;
        let ptr = unsafe {
            libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ, libc::MAP_SHARED, f.as_raw_fd(), 0)
        };
        assert!(ptr != libc::MAP_FAILED, "mmap に失敗した");
        Map { ptr, len, file: f }
    }
    fn slice<T>(&self) -> &[T] {
        unsafe { std::slice::from_raw_parts(self.ptr as *const T, self.len / std::mem::size_of::<T>()) }
    }
    /// ページキャッシュから追い出す。Linux の msync(MS_INVALIDATE) はキャッシュを捨てないので、
    /// 写像からページを外したうえで、ファイルのページキャッシュを捨てる。
    fn evict(&self) {
        #[cfg(target_os = "linux")]
        unsafe {
            libc::madvise(self.ptr, self.len, libc::MADV_DONTNEED);
            libc::posix_fadvise(self.file.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED);
        }
        #[cfg(not(target_os = "linux"))]
        unsafe {
            libc::msync(self.ptr, self.len, libc::MS_INVALIDATE)
        };
    }
    /// 先読みを強める（MADV_SEQUENTIAL）か、既定に戻す。
    fn sequential(&self, on: bool) {
        let advice = if on { libc::MADV_SEQUENTIAL } else { libc::MADV_NORMAL };
        unsafe { libc::madvise(self.ptr, self.len, advice) };
    }
}

impl Drop for Map {
    fn drop(&mut self) {
        unsafe { libc::munmap(self.ptr, self.len) };
    }
}

/// ページキャッシュを通さずに読む 2 列。
struct FileCols {
    keys: File,
    vals: File,
    n: usize,
    fence: Vec<u64>,
}

impl FileCols {
    fn open(p: &Paths, n: usize, fence: &[u64]) -> FileCols {
        let (keys, vals) = (File::open(&p.keys).unwrap(), File::open(&p.vals).unwrap());
        nocache(&keys);
        nocache(&vals);
        FileCols { keys, vals, n, fence: fence.to_vec() }
    }
}

/// 処理が読む 2 列。
enum Cols<'a> {
    Mem(&'a [u64], &'a [f64]),
    File(&'a FileCols),
}

/// File から読み込むときの、スレッドごとの置き場所。
#[derive(Default)]
struct Buf {
    k: Vec<u64>,
    v: Vec<f64>,
}

impl Cols<'_> {
    fn len(&self) -> usize {
        match self {
            Cols::Mem(k, _) => k.len(),
            Cols::File(f) => f.n,
        }
    }

    /// [lo, hi) のセルを f に渡す。
    fn with<R>(&self, lo: usize, hi: usize, buf: &mut Buf, f: impl FnOnce(&[u64], &[f64]) -> R) -> R {
        match self {
            Cols::Mem(k, v) => f(&k[lo..hi], &v[lo..hi]),
            Cols::File(c) => {
                buf.k.resize(hi - lo, 0);
                buf.v.resize(hi - lo, 0.0);
                read_at(&c.keys, bytes_mut(&mut buf.k), lo as u64 * 8);
                read_at(&c.vals, bytes_mut(&mut buf.v), lo as u64 * 8);
                f(&buf.k, &buf.v)
            }
        }
    }

    /// key 以上の最初の位置。File はフェンスでページを決め、そのページだけを読む。
    fn lower_bound(&self, key: u64, buf: &mut Buf) -> usize {
        match self {
            Cols::Mem(k, _) => k.partition_point(|&x| x < key),
            Cols::File(c) => {
                let page = c.fence.partition_point(|&x| x < key);
                if page == 0 {
                    return 0;
                }
                let lo = (page - 1) * PAGE;
                let hi = (page * PAGE).min(c.n);
                buf.k.resize(hi - lo, 0);
                read_at(&c.keys, bytes_mut(&mut buf.k), lo as u64 * 8);
                lo + buf.k.partition_point(|&x| x < key)
            }
        }
    }

    fn get(&self, key: u64, buf: &mut Buf) -> Option<f64> {
        let pos = self.lower_bound(key, buf);
        if pos >= self.len() {
            return None;
        }
        match self {
            Cols::Mem(k, v) => (k[pos] == key).then(|| v[pos]),
            Cols::File(c) => {
                // lower_bound が読んだページに pos のキーがある（ページの末尾なら次のページの先頭）
                let found = match c.fence.get(pos / PAGE) {
                    Some(&first) if pos.is_multiple_of(PAGE) => first,
                    _ => buf.k[pos % PAGE],
                };
                (found == key).then(|| {
                    let mut v = [0f64];
                    read_at(&c.vals, bytes_mut(&mut v), pos as u64 * 8);
                    v[0]
                })
            }
        }
    }
}

// ---- 処理 ----

fn blocks(n: usize) -> Vec<(usize, usize)> {
    (0..n.div_ceil(BLOCK)).map(|i| (i * BLOCK, ((i + 1) * BLOCK).min(n))).collect()
}

fn scan(c: &Cols) -> f64 {
    blocks(c.len())
        .into_par_iter()
        .map_init(Buf::default, |buf, (lo, hi)| {
            c.with(lo, hi, buf, |k, v| {
                let x = k.iter().fold(0u64, |a, &k| a ^ k);
                v.iter().sum::<f64>() + (x & 1) as f64
            })
        })
        .sum()
}

fn agg(c: &Cols) -> f64 {
    let out = blocks(c.len())
        .into_par_iter()
        .fold(
            || (vec![0f64; GROUPS], Buf::default()),
            |(mut acc, mut buf), (lo, hi)| {
                c.with(lo, hi, &mut buf, |k, v| {
                    for (&k, &v) in k.iter().zip(v) {
                        acc[group(k)] += v;
                    }
                });
                (acc, buf)
            },
        )
        .map(|(acc, _)| acc)
        .reduce(
            || vec![0f64; GROUPS],
            |mut a, b| {
                a.iter_mut().zip(&b).for_each(|(a, b)| *a += b);
                a
            },
        );
    out.iter().sum()
}

fn merge_into(ak: &[u64], av: &[f64], bk: &[u64], bv: &[f64], ok: &mut Vec<u64>, ov: &mut Vec<f64>) {
    let (mut i, mut j) = (0, 0);
    while i < ak.len() && j < bk.len() {
        let (x, y) = (ak[i], bk[j]);
        if x < y {
            ok.push(x);
            ov.push(av[i]);
            i += 1;
        } else if y < x {
            ok.push(y);
            ov.push(bv[j]);
            j += 1;
        } else {
            ok.push(x);
            ov.push(av[i] + bv[j]);
            i += 1;
            j += 1;
        }
    }
    ok.extend_from_slice(&ak[i..]);
    ov.extend_from_slice(&av[i..]);
    ok.extend_from_slice(&bk[j..]);
    ov.extend_from_slice(&bv[j..]);
}

/// 結果の 1 つの範囲（キーと値の 2 列）。
type Part = (Vec<u64>, Vec<f64>);

/// A + B。範囲を A の BLOCK おきのキーで切り、範囲ごとに並べて突き合わせる。
/// out が None なら結果を Vec に持ち、Some なら範囲ごとのファイルに書き出す。返すのは結果のセル数。
fn merge(a: &Cols, b: &Cols, out: Option<&Path>) -> (usize, Vec<Part>) {
    let mut buf = Buf::default();
    let mut bounds = vec![(0usize, 0usize)];
    for lo in (BLOCK..a.len()).step_by(BLOCK) {
        let key = match a {
            Cols::Mem(k, _) => k[lo],
            Cols::File(f) => f.fence[lo / PAGE],
        };
        bounds.push((lo, b.lower_bound(key, &mut buf)));
    }
    bounds.push((a.len(), b.len()));
    let parts: Vec<(usize, Option<Part>)> = bounds
        .par_windows(2)
        .enumerate()
        .map_init(
            || (Buf::default(), Buf::default(), Buf::default()),
            |(ba, bb, o), (i, w)| {
                let ((alo, blo), (ahi, bhi)) = (w[0], w[1]);
                let (mut ok, mut ov) = match out {
                    Some(_) => (std::mem::take(&mut o.k), std::mem::take(&mut o.v)),
                    None => (Vec::new(), Vec::new()),
                };
                ok.clear();
                ov.clear();
                a.with(alo, ahi, ba, |ak, av| b.with(blo, bhi, bb, |bk, bv| merge_into(ak, av, bk, bv, &mut ok, &mut ov)));
                let n = ok.len();
                match out {
                    Some(dir) => {
                        let p = Paths::new(dir, &format!("merge.{i:05}"));
                        write_file(&p.keys, bytes(&ok));
                        write_file(&p.vals, bytes(&ov));
                        (o.k, o.v) = (ok, ov);
                        (n, None)
                    }
                    None => (n, Some((ok, ov))),
                }
            },
        )
        .collect();
    let n = parts.iter().map(|p| p.0).sum();
    (n, parts.into_iter().filter_map(|p| p.1).collect())
}

fn lookup(c: &Cols, probes: &[u64]) -> usize {
    probes
        .par_iter()
        .map_init(Buf::default, |buf, &k| c.get(k, buf).is_some() as usize)
        .sum()
}

/// 軸の順番を変えて、メモリ内で並べ替える。
fn rekey_heap(c: &Cols) -> Vec<(u64, f64)> {
    let mut buf = Buf::default();
    let mut out = Vec::with_capacity(c.len());
    for (lo, hi) in blocks(c.len()) {
        c.with(lo, hi, &mut buf, |k, v| out.extend(k.iter().zip(v).map(|(&k, &v)| (rekey(k), v))));
    }
    out.par_sort_unstable_by_key(|p| p.0);
    out
}

/// 外部ソートの 1 つのラン（並べ終えた (キー, 値) の組の列）。
struct Run {
    file: File,
    path: PathBuf,
    n: usize,
    /// PAGE 組おきの先頭のキー
    fence: Vec<u64>,
}

impl Run {
    /// key 以上の最初の位置。フェンスでページを決め、そのページだけを読む。
    fn lower_bound(&self, key: u64) -> usize {
        let page = self.fence.partition_point(|&x| x < key);
        if page == 0 {
            return 0;
        }
        let lo = (page - 1) * PAGE;
        let mut buf = vec![(0u64, 0f64); (page * PAGE).min(self.n) - lo];
        read_at(&self.file, bytes_mut(&mut buf), lo as u64 * 16);
        lo + buf.partition_point(|p| p.0 < key)
    }
}

/// 軸の順番を変えて、外部ソートで並べ直し、dir/sorted.* に範囲ごとに書く。
/// ヒープは RUN セル分と、範囲ごとの併合の置き場所だけを使う。
///
/// 1. 入力を RUN セルずつ読み、キーを変えて並べ、ランとしてファイルに書く
/// 2. 全ランのフェンスから区切りのキーを選び、区切りごとに各ランの位置を求める
/// 3. 範囲ごとに、各ランの該当部分を読んで併合し、書く（範囲どうしは並列）
fn rekey_external(c: &Cols, dir: &Path) -> usize {
    let mut buf = Buf::default();
    let mut run: Vec<(u64, f64)> = Vec::with_capacity(RUN);
    let mut runs: Vec<Run> = Vec::new();
    let flush = |run: &mut Vec<(u64, f64)>, runs: &mut Vec<Run>| {
        run.par_sort_unstable_by_key(|p| p.0);
        let path = dir.join(format!("run.{:04}", runs.len()));
        write_file(&path, bytes(run));
        let file = File::open(&path).unwrap();
        nocache(&file);
        runs.push(Run { file, path, n: run.len(), fence: run.iter().step_by(PAGE).map(|p| p.0).collect() });
        run.clear();
    };
    for (lo, hi) in blocks(c.len()) {
        c.with(lo, hi, &mut buf, |k, v| {
            for (&k, &v) in k.iter().zip(v) {
                run.push((rekey(k), v));
                if run.len() == RUN {
                    flush(&mut run, &mut runs);
                }
            }
        });
    }
    if !run.is_empty() {
        flush(&mut run, &mut runs);
    }
    drop(run);
    drop(buf);

    let mut fences: Vec<u64> = runs.iter().flat_map(|r| r.fence.iter().copied()).collect();
    fences.sort_unstable();
    let parts = rayon::current_num_threads() * 4;
    let mut bounds: Vec<u64> = (1..parts).map(|i| fences[i * fences.len() / parts]).collect();
    bounds.dedup();
    drop(fences);
    // cuts[r][j]: ラン r の j 番目の範囲の始まり
    let cuts: Vec<Vec<usize>> = runs
        .par_iter()
        .map(|r| {
            let mut c = vec![0];
            c.extend(bounds.iter().map(|&b| r.lower_bound(b)));
            c.push(r.n);
            c
        })
        .collect();

    let n = (0..=bounds.len())
        .into_par_iter()
        .map(|j| {
            let segs: Vec<Vec<(u64, f64)>> = runs
                .iter()
                .zip(&cuts)
                .map(|(r, c)| {
                    let mut s = vec![(0u64, 0f64); c[j + 1] - c[j]];
                    read_at(&r.file, bytes_mut(&mut s), c[j] as u64 * 16);
                    s
                })
                .collect();
            let total: usize = segs.iter().map(Vec::len).sum();
            let (mut ok, mut ov) = (Vec::with_capacity(total), Vec::with_capacity(total));
            let mut at = vec![0usize; segs.len()];
            let mut heap: BinaryHeap<Reverse<(u64, usize)>> =
                segs.iter().enumerate().filter(|(_, s)| !s.is_empty()).map(|(i, s)| Reverse((s[0].0, i))).collect();
            while let Some(Reverse((k, i))) = heap.pop() {
                assert!(ok.last().is_none_or(|&l| l < k), "外部ソートの結果が昇順でない");
                ok.push(k);
                ov.push(segs[i][at[i]].1);
                at[i] += 1;
                if let Some(&(k, _)) = segs[i].get(at[i]) {
                    heap.push(Reverse((k, i)));
                }
            }
            drop(segs);
            let p = Paths::new(dir, &format!("sorted.{j:05}"));
            write_file(&p.keys, bytes(&ok));
            write_file(&p.vals, bytes(&ov));
            ok.len()
        })
        .sum();
    for r in runs {
        fs::remove_file(r.path).unwrap();
    }
    n
}

// ---- 測定 ----

/// プロセスの I/O とメモリ（バイト）。
struct Usage {
    /// ディスクから読んだ量
    read: u64,
    /// ディスクへ書いた量
    written: u64,
    /// 無名のページ（macOS は phys_footprint、Linux は RssAnon）
    anon: u64,
    /// 常駐する量（写像したファイルのページを含む）
    rss: u64,
}

/// Usage の anon の列の名前。
#[cfg(target_os = "macos")]
const ANON: &str = "footprint";
#[cfg(target_os = "linux")]
const ANON: &str = "RssAnon";

#[cfg(target_os = "macos")]
fn usage() -> Usage {
    let u = unsafe {
        let mut u: libc::rusage_info_v4 = std::mem::zeroed();
        libc::proc_pid_rusage(libc::getpid(), libc::RUSAGE_INFO_V4, &mut u as *mut _ as *mut libc::rusage_info_t);
        u
    };
    Usage {
        read: u.ri_diskio_bytesread,
        written: u.ri_diskio_byteswritten,
        anon: u.ri_phys_footprint,
        rss: u.ri_resident_size,
    }
}

/// /proc/self/io（バイト）と /proc/self/status（kB）から読む。
#[cfg(target_os = "linux")]
fn usage() -> Usage {
    fn field(text: &str, name: &str) -> u64 {
        let line = text.lines().find(|l| l.starts_with(name)).unwrap_or_else(|| panic!("{name} がない"));
        line[name.len()..].split_whitespace().next().unwrap().parse().unwrap()
    }
    let io = fs::read_to_string("/proc/self/io").unwrap();
    let status = fs::read_to_string("/proc/self/status").unwrap();
    Usage {
        read: field(&io, "read_bytes:"),
        written: field(&io, "write_bytes:"),
        anon: field(&status, "RssAnon:") * 1024,
        rss: field(&status, "VmRSS:") * 1024,
    }
}

const MB: f64 = (1 << 20) as f64;

struct Row {
    op: &'static str,
    mode: &'static str,
    threads: usize,
    /// 1 単位（セルまたは引いたキー）あたりのナノ秒の分母
    units: usize,
    /// 読んだ入力の量（GB/s の分子）
    in_bytes: usize,
}

fn measure<R: Send>(row: Row, f: impl FnOnce() -> R + Send) -> R {
    let pool = rayon::ThreadPoolBuilder::new().num_threads(row.threads).build().unwrap();
    let base = HEAP.load(Relaxed);
    PEAK.store(base, Relaxed);
    let before = usage();
    let t = Instant::now();
    let r = pool.install(f);
    let secs = t.elapsed().as_secs_f64();
    let after = usage();
    println!(
        "| {} | {} | {} | {:.0} | {:.1} | {} | {:.0} | {:.0} | {:.0} | {:.0} | {:.0} |",
        row.op,
        row.mode,
        row.threads,
        secs * 1e3,
        secs * 1e9 / row.units as f64,
        if row.in_bytes == 0 { "-".to_string() } else { format!("{:.2}", row.in_bytes as f64 / secs / 1e9) },
        (after.read - before.read) as f64 / MB,
        (after.written - before.written) as f64 / MB,
        (PEAK.load(Relaxed) - base) as f64 / MB,
        after.anon as f64 / MB,
        after.rss as f64 / MB,
    );
    r
}

fn clean(dir: &Path, prefix: &str) {
    for e in fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.file_name().unwrap().to_str().unwrap().starts_with(prefix) {
            fs::remove_file(p).unwrap();
        }
    }
}

fn load(p: &Paths, n: usize) -> (Vec<u64>, Vec<f64>) {
    let (mut k, mut v) = (vec![0u64; n], vec![0f64; n]);
    File::open(&p.keys).unwrap().read_exact(bytes_mut(&mut k)).unwrap();
    File::open(&p.vals).unwrap().read_exact(bytes_mut(&mut v)).unwrap();
    (k, v)
}

/// 持ち方ごとの検算（どの持ち方でも同じになる）。
fn check(mode: &str, scan: f64, agg: f64, merged: usize, hits: usize, sorted: usize) {
    println!("\n検算（{mode}）: scan {scan:.0}、agg {agg:.0}、merge {merged} セル、lookup {hits} 件、rekey {sorted} セル\n");
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n: usize = args.get(1).map_or(64 << 20, |s| s.parse().unwrap());
    let dir = PathBuf::from(args.get(2).map_or("out_of_core.tmp", |s| s.as_str()));
    let modes: Vec<&str> = match args.get(3..) {
        Some(m) if !m.is_empty() => m.iter().map(|s| s.as_str()).collect(),
        _ => vec!["pread", "mmap", "heap"],
    };
    let probes_hot = 1_000_000;
    let probes_cold = 20_000;
    let threads = [1, rayon::current_num_threads()];
    fs::create_dir_all(&dir).unwrap();

    let t = Instant::now();
    let d = generate(&dir, n);
    println!(
        "A {} セル、B {} セル、ファイル {:.0} MB を {:.1} 秒で作った（スレッド {:?}）\n",
        d.n_a,
        d.n_b,
        (d.n_a + d.n_b) as f64 * 16.0 / MB,
        t.elapsed().as_secs_f64(),
        threads
    );
    println!("| 処理 | 持ち方 | スレッド | ms | ns/単位 | GB/s | 読んだ MB | 書いた MB | ヒープの最大 MB | {ANON} MB | RSS MB |");
    println!("|---|---|---|---|---|---|---|---|---|---|---|");
    let (in_a, in_ab) = (d.n_a * 16, (d.n_a + d.n_b) * 16);
    let row = |op, mode, threads, units, in_bytes| Row { op, mode, threads, units, in_bytes };
    let mut lines = Vec::new();

    for mode in modes {
        match mode {
            // ページキャッシュを通さない
            "pread" => {
                let (fa, fb) = (FileCols::open(&d.a, d.n_a, &d.fence_a), FileCols::open(&d.b, d.n_b, &d.fence_b));
                let (a, b) = (Cols::File(&fa), Cols::File(&fb));
                let (mut s, mut g, mut m, mut h) = (0.0, 0.0, 0, 0);
                for &t in &threads {
                    s = measure(row("scan", "pread", t, d.n_a, in_a), || scan(&a));
                    g = measure(row("agg", "pread", t, d.n_a, in_a), || agg(&a));
                    m = measure(row("merge", "pread", t, d.n_a + d.n_b, in_ab), || merge(&a, &b, Some(&dir)).0);
                    clean(&dir, "merge.");
                    h = measure(row("lookup", "pread", t, probes_cold, 0), || lookup(&a, &d.probes[..probes_cold]));
                }
                let r = measure(row("rekey", "pread", threads[1], d.n_a, in_a), || rekey_external(&a, &dir));
                clean(&dir, "sorted.");
                lines.push(("pread", s, g, m, h, r));
            }
            "mmap" => {
                let maps = [Map::open(&d.a.keys), Map::open(&d.a.vals), Map::open(&d.b.keys), Map::open(&d.b.vals)];
                let evict = || maps.iter().for_each(Map::evict);
                let a = Cols::Mem(maps[0].slice(), maps[1].slice());
                let b = Cols::Mem(maps[2].slice(), maps[3].slice());
                let (mut s, mut g, mut m, mut h) = (0.0, 0.0, 0, 0);
                for &t in &threads {
                    evict();
                    s = measure(row("scan", "mmap 冷", t, d.n_a, in_a), || scan(&a));
                    evict();
                    g = measure(row("agg", "mmap 冷", t, d.n_a, in_a), || agg(&a));
                    evict();
                    m = measure(row("merge", "mmap 冷", t, d.n_a + d.n_b, in_ab), || merge(&a, &b, Some(&dir)).0);
                    clean(&dir, "merge.");
                    evict();
                    h = measure(row("lookup", "mmap 冷", t, probes_cold, 0), || lookup(&a, &d.probes[..probes_cold]));
                }
                evict();
                let r = measure(row("rekey", "mmap 冷", threads[1], d.n_a, in_a), || rekey_external(&a, &dir));
                clean(&dir, "sorted.");
                lines.push(("mmap 冷", s, g, m, h, r));
                maps.iter().for_each(|m| m.sequential(true));
                for &t in &threads {
                    evict();
                    measure(row("scan", "mmap 冷 SEQUENTIAL", t, d.n_a, in_a), || scan(&a));
                }
                maps.iter().for_each(|m| m.sequential(false));
                scan(&a);
                scan(&b);
                for &t in &threads {
                    s = measure(row("scan", "mmap 暖", t, d.n_a, in_a), || scan(&a));
                    g = measure(row("agg", "mmap 暖", t, d.n_a, in_a), || agg(&a));
                    m = measure(row("merge", "mmap 暖", t, d.n_a + d.n_b, in_ab), || merge(&a, &b, Some(&dir)).0);
                    clean(&dir, "merge.");
                    measure(row("merge (Vec へ)", "mmap 暖", t, d.n_a + d.n_b, in_ab), || merge(&a, &b, None).0);
                    measure(row("lookup", "mmap 暖", t, probes_hot, 0), || lookup(&a, &d.probes[..probes_hot]));
                }
                h = lookup(&a, &d.probes[..probes_cold]);
                let r = measure(row("rekey", "mmap 暖", threads[1], d.n_a, in_a), || rekey_external(&a, &dir));
                clean(&dir, "sorted.");
                lines.push(("mmap 暖", s, g, m, h, r));
                evict();
            }
            // 今のエンジン
            "heap" => {
                let ((ak, av), (bk, bv)) =
                    measure(row("load", "heap", 1, d.n_a + d.n_b, in_ab), || (load(&d.a, d.n_a), load(&d.b, d.n_b)));
                // エンジンの本体（store.rs の Base）と同じく、Vec ではなく不変の平らな列で持って読む
                let (ak, av): (Column<u64>, Column<f64>) = (ak.into(), av.into());
                let (bk, bv): (Column<u64>, Column<f64>) = (bk.into(), bv.into());
                let (a, b) = (Cols::Mem(&ak, &av), Cols::Mem(&bk, &bv));
                let (mut s, mut g, mut m) = (0.0, 0.0, 0);
                for &t in &threads {
                    s = measure(row("scan", "heap", t, d.n_a, in_a), || scan(&a));
                    g = measure(row("agg", "heap", t, d.n_a, in_a), || agg(&a));
                    m = measure(row("merge (Vec へ)", "heap", t, d.n_a + d.n_b, in_ab), || merge(&a, &b, None).0);
                    measure(row("lookup", "heap", t, probes_hot, 0), || lookup(&a, &d.probes[..probes_hot]));
                }
                let h = lookup(&a, &d.probes[..probes_cold]);
                let r = measure(row("rekey", "heap", threads[1], d.n_a, in_a), || rekey_heap(&a).len());
                lines.push(("heap", s, g, m, h, r));
            }
            _ => panic!("持ち方は pread、mmap、heap のどれか: {mode}"),
        }
    }
    for (mode, s, g, m, h, r) in lines {
        check(mode, s, g, m, h, r);
    }
    clean(&dir, "a.");
    clean(&dir, "b.");
}
