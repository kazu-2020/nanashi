//! セルのキーの表し方による速さとメモリの違いを測る（docs/member-numbering.md の「キーの表し方の測定」）。
//!
//!     cargo run --release --example key_layout --manifest-path native/Cargo.toml -- [セル数]
//!
//! エンジンの本体は使わず、同じ座標の集まりを 4 つの表し方で持ち、エンジンの主な操作に当たる
//! 並べ替え、結合（キー順の 2 列の突き合わせ）、集計（1 つの軸を外して足し込む）を 1 スレッドで測る。
//!
//! - u64: 今のエンジン。各軸の番号を詰めた 64 ビットのキー（収まる軸の組み合わせだけ）
//! - [u64; N]: 語数を型で決める。N = 2 が 128 ビット案で、上位の語から辞書順で比べる
//! - stride: Metric ごとに語数を実行時に決め、キーを平たい Vec<u64> に語数おきに並べる
//! - Vec<u32>: セルごとに軸の番号の列をヒープに持つ（可変長のキーのいちばん素直な形）

use std::alloc::{GlobalAlloc, Layout, System};
use std::cmp::Ordering;
use std::hint::black_box;
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
use std::time::Instant;

/// ヒープの使用量を数えるアロケーター（表し方ごとのメモリを測る）。
struct Counting;

static HEAP: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        HEAP.fetch_add(l.size(), Relaxed);
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        HEAP.fetch_sub(l.size(), Relaxed);
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        HEAP.fetch_add(new, Relaxed);
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
    fn below(&mut self, n: u32) -> u32 {
        (self.next() % n as u64) as u32
    }
}

fn bits_for(size: u32) -> u32 {
    if size <= 1 {
        1
    } else {
        32 - (size - 1).leading_zeros()
    }
}

/// 軸の詰め方。先頭の軸が上位に来る。軸は語の境目をまたがない（words 語に収める）。
struct Spec {
    sizes: Vec<u32>,
    word: Vec<usize>, // 軸ごとの語の位置（0 が上位の語）
    shift: Vec<u32>,  // 語の中での位置
    mask: Vec<u64>,
    words: usize,
}

impl Spec {
    fn new(sizes: &[u32]) -> Spec {
        let bits: Vec<u32> = sizes.iter().map(|&s| bits_for(s)).collect();
        // 上位の語から順に、語に収まるだけ軸を入れる
        let (mut word, mut used) = (vec![0; sizes.len()], vec![0u32]);
        for (i, &b) in bits.iter().enumerate() {
            if used.last().unwrap() + b > 64 {
                used.push(0);
            }
            word[i] = used.len() - 1;
            *used.last_mut().unwrap() += b;
        }
        // 語の中では、先の軸ほど上位のビットに置く
        let mut shift = vec![0; sizes.len()];
        let mut acc = vec![0u32; used.len()];
        for i in (0..sizes.len()).rev() {
            shift[i] = acc[word[i]];
            acc[word[i]] += bits[i];
        }
        let mask = bits.iter().map(|&b| if b == 64 { u64::MAX } else { (1 << b) - 1 }).collect();
        Spec { sizes: sizes.to_vec(), word, shift, mask, words: used.len() }
    }

    fn total_bits(&self) -> u32 {
        self.sizes.iter().map(|&s| bits_for(s)).sum()
    }

    fn pack(&self, c: &[u32], out: &mut [u64]) {
        out.fill(0);
        for (i, &m) in c.iter().enumerate() {
            out[self.word[i]] |= (m as u64) << self.shift[i];
        }
    }

    fn clear(&self, key: &mut [u64], i: usize) {
        key[self.word[i]] &= !(self.mask[i] << self.shift[i]);
    }
}

/// 測る操作。どの表し方も、キー順に並んだセルの列を持つ。
trait Cells: Sized {
    fn name() -> String;
    /// 座標（セル数 × 軸数の平たい列）と値から作る。並べてはいない。
    fn build(spec: &Spec, coords: &[u32], vals: &[f64]) -> Self;
    fn sort(&mut self);
    /// 両方にあるキーだけを、値の積で残す（キー順の 2 列の突き合わせ）。
    fn intersect(&self, other: &Self) -> Self;
    /// 軸 dim を外して足し込む（外した軸の番号を 0 にし、並べ直して同じキーを足す）。
    fn remove(&self, spec: &Spec, dim: usize) -> Self;
    fn len(&self) -> usize;
    fn checksum(&self) -> f64;
}

/// 並んだ (キー, 値) の列で、同じキーの値を足す。
fn reduce_sorted<K: PartialEq + Copy>(v: Vec<(K, f64)>) -> Vec<(K, f64)> {
    let mut out: Vec<(K, f64)> = Vec::with_capacity(v.len());
    for (k, x) in v {
        match out.last_mut() {
            Some(last) if last.0 == k => last.1 += x,
            _ => out.push((k, x)),
        }
    }
    out
}

fn merge<K: Ord + Copy>(a: &[(K, f64)], b: &[(K, f64)]) -> Vec<(K, f64)> {
    let (mut i, mut j) = (0, 0);
    let mut out = Vec::new();
    while i < a.len() && j < b.len() {
        match a[i].0.cmp(&b[j].0) {
            Ordering::Less => i += 1,
            Ordering::Greater => j += 1,
            Ordering::Equal => {
                out.push((a[i].0, a[i].1 * b[j].1));
                i += 1;
                j += 1;
            }
        }
    }
    out
}

struct U64(Vec<(u64, f64)>);

impl Cells for U64 {
    fn name() -> String {
        "u64".into()
    }
    fn build(spec: &Spec, coords: &[u32], vals: &[f64]) -> Self {
        let d = spec.sizes.len();
        let mut k = [0u64; 1];
        U64(vals.iter().enumerate().map(|(r, &v)| {
            spec.pack(&coords[r * d..(r + 1) * d], &mut k);
            (k[0], v)
        }).collect())
    }
    fn sort(&mut self) {
        self.0.sort_unstable_by_key(|c| c.0);
    }
    fn intersect(&self, other: &Self) -> Self {
        U64(merge(&self.0, &other.0))
    }
    fn remove(&self, spec: &Spec, dim: usize) -> Self {
        let mut v: Vec<(u64, f64)> = self.0.iter().map(|&(k, x)| {
            let mut key = [k];
            spec.clear(&mut key, dim);
            (key[0], x)
        }).collect();
        v.sort_unstable_by_key(|c| c.0);
        U64(reduce_sorted(v))
    }
    fn len(&self) -> usize {
        self.0.len()
    }
    fn checksum(&self) -> f64 {
        self.0.iter().map(|c| c.1).sum()
    }
}

/// 語数を型で決めたキー。N = 2 が 128 ビット案。
struct Fixed<const N: usize>(Vec<([u64; N], f64)>);

impl<const N: usize> Cells for Fixed<N> {
    fn name() -> String {
        format!("[u64; {N}]")
    }
    fn build(spec: &Spec, coords: &[u32], vals: &[f64]) -> Self {
        let d = spec.sizes.len();
        Fixed(vals.iter().enumerate().map(|(r, &v)| {
            let mut k = [0u64; N];
            spec.pack(&coords[r * d..(r + 1) * d], &mut k[..spec.words]);
            (k, v)
        }).collect())
    }
    fn sort(&mut self) {
        self.0.sort_unstable_by_key(|c| c.0);
    }
    fn intersect(&self, other: &Self) -> Self {
        Fixed(merge(&self.0, &other.0))
    }
    fn remove(&self, spec: &Spec, dim: usize) -> Self {
        let mut v: Vec<([u64; N], f64)> = self.0.iter().map(|&(mut k, x)| {
            spec.clear(&mut k, dim);
            (k, x)
        }).collect();
        v.sort_unstable_by_key(|c| c.0);
        Fixed(reduce_sorted(v))
    }
    fn len(&self) -> usize {
        self.0.len()
    }
    fn checksum(&self) -> f64 {
        self.0.iter().map(|c| c.1).sum()
    }
}

/// キーを語数おきに並べた平たい列と、値の列。
struct Stride {
    w: usize,
    keys: Vec<u64>,
    vals: Vec<f64>,
}

impl Stride {
    fn key(&self, r: usize) -> &[u64] {
        &self.keys[r * self.w..(r + 1) * self.w]
    }
    fn push(&mut self, k: &[u64], v: f64) {
        self.keys.extend_from_slice(k);
        self.vals.push(v);
    }
    fn sorted(&self) -> Stride {
        let mut idx: Vec<u32> = (0..self.vals.len() as u32).collect();
        idx.sort_unstable_by(|&a, &b| self.key(a as usize).cmp(self.key(b as usize)));
        let mut out = Stride { w: self.w, keys: Vec::with_capacity(self.keys.len()), vals: Vec::with_capacity(self.vals.len()) };
        for &r in &idx {
            out.push(self.key(r as usize), self.vals[r as usize]);
        }
        out
    }
}

impl Cells for Stride {
    fn name() -> String {
        "stride".into()
    }
    fn build(spec: &Spec, coords: &[u32], vals: &[f64]) -> Self {
        let (d, w) = (spec.sizes.len(), spec.words);
        let mut keys = vec![0u64; vals.len() * w];
        for r in 0..vals.len() {
            spec.pack(&coords[r * d..(r + 1) * d], &mut keys[r * w..(r + 1) * w]);
        }
        Stride { w, keys, vals: vals.to_vec() }
    }
    fn sort(&mut self) {
        *self = self.sorted();
    }
    fn intersect(&self, other: &Self) -> Self {
        let (mut i, mut j) = (0, 0);
        let mut out = Stride { w: self.w, keys: Vec::new(), vals: Vec::new() };
        while i < self.vals.len() && j < other.vals.len() {
            match self.key(i).cmp(other.key(j)) {
                Ordering::Less => i += 1,
                Ordering::Greater => j += 1,
                Ordering::Equal => {
                    out.push(self.key(i), self.vals[i] * other.vals[j]);
                    i += 1;
                    j += 1;
                }
            }
        }
        out
    }
    fn remove(&self, spec: &Spec, dim: usize) -> Self {
        let mut cleared = Stride { w: self.w, keys: self.keys.clone(), vals: self.vals.clone() };
        for r in 0..cleared.vals.len() {
            spec.clear(&mut cleared.keys[r * self.w..(r + 1) * self.w], dim);
        }
        let s = cleared.sorted();
        drop(cleared);
        let mut out = Stride { w: self.w, keys: Vec::with_capacity(s.keys.len()), vals: Vec::with_capacity(s.vals.len()) };
        for r in 0..s.vals.len() {
            if r > 0 && s.key(r) == s.key(r - 1) {
                *out.vals.last_mut().unwrap() += s.vals[r];
            } else {
                out.push(s.key(r), s.vals[r]);
            }
        }
        out
    }
    fn len(&self) -> usize {
        self.vals.len()
    }
    fn checksum(&self) -> f64 {
        self.vals.iter().sum()
    }
}

/// セルごとに軸の番号の列をヒープに持つ。
struct Heap(Vec<(Vec<u32>, f64)>);

impl Cells for Heap {
    fn name() -> String {
        "Vec<u32>".into()
    }
    fn build(spec: &Spec, coords: &[u32], vals: &[f64]) -> Self {
        let d = spec.sizes.len();
        Heap(vals.iter().enumerate().map(|(r, &v)| (coords[r * d..(r + 1) * d].to_vec(), v)).collect())
    }
    fn sort(&mut self) {
        self.0.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    }
    fn intersect(&self, other: &Self) -> Self {
        let (a, b) = (&self.0, &other.0);
        let (mut i, mut j) = (0, 0);
        let mut out = Vec::new();
        while i < a.len() && j < b.len() {
            match a[i].0.cmp(&b[j].0) {
                Ordering::Less => i += 1,
                Ordering::Greater => j += 1,
                Ordering::Equal => {
                    out.push((a[i].0.clone(), a[i].1 * b[j].1));
                    i += 1;
                    j += 1;
                }
            }
        }
        Heap(out)
    }
    fn remove(&self, _spec: &Spec, dim: usize) -> Self {
        // 軸が減るので、外した残りの番号で新しい列を作る
        let mut v: Vec<(Vec<u32>, f64)> = self.0.iter().map(|(k, x)| {
            let mut nk = Vec::with_capacity(k.len() - 1);
            nk.extend_from_slice(&k[..dim]);
            nk.extend_from_slice(&k[dim + 1..]);
            (nk, *x)
        }).collect();
        v.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        let mut out: Vec<(Vec<u32>, f64)> = Vec::with_capacity(v.len());
        for (k, x) in v {
            match out.last_mut() {
                Some(last) if last.0 == k => last.1 += x,
                _ => out.push((k, x)),
            }
        }
        Heap(out)
    }
    fn len(&self) -> usize {
        self.0.len()
    }
    fn checksum(&self) -> f64 {
        self.0.iter().map(|c| c.1).sum()
    }
}

/// 重ならない座標を n 個、ばらばらの順で作る。
fn gen_coords(spec: &Spec, n: usize, rng: &mut Rng) -> Vec<u32> {
    let d = spec.sizes.len();
    let mut keys: Vec<(u128, Vec<u32>)> = Vec::with_capacity(n);
    let mut seen = std::collections::HashSet::with_capacity(n);
    while keys.len() < n {
        let c: Vec<u32> = spec.sizes.iter().map(|&s| rng.below(s)).collect();
        let k = c.iter().fold(0u128, |acc, &m| acc * 1_000_003 + m as u128);
        if seen.insert(k) {
            keys.push((k, c));
        }
    }
    let mut out = Vec::with_capacity(n * d);
    for (_, c) in keys {
        out.extend_from_slice(&c);
    }
    out
}

/// 結合の相手。a の半分のセルと、同じ数の別のセルを混ぜる。
fn partner(spec: &Spec, a: &[u32], rng: &mut Rng) -> Vec<u32> {
    let d = spec.sizes.len();
    let n = a.len() / d;
    let other = gen_coords(spec, n / 2, rng);
    let mut out = Vec::with_capacity(a.len());
    for r in (0..n).step_by(2) {
        out.extend_from_slice(&a[r * d..(r + 1) * d]);
    }
    out.extend_from_slice(&other);
    out
}

fn best_of<T>(runs: usize, mut f: impl FnMut() -> T) -> (f64, T) {
    let mut best = f64::MAX;
    let mut last = None;
    for _ in 0..runs {
        let t = Instant::now();
        let r = f();
        best = best.min(t.elapsed().as_secs_f64() * 1e3);
        last = Some(black_box(r));
    }
    (best, last.unwrap())
}

struct Row {
    name: String,
    bytes: f64,
    sort: f64,
    join: f64,
    remove: f64,
}

fn measure<C: Cells>(spec: &Spec, a: &[u32], va: &[f64], b: &[u32], vb: &[f64], dim: usize) -> Row {
    let n = va.len();
    let before = HEAP.load(Relaxed);
    let mut x = C::build(spec, a, va);
    x.sort();
    let bytes = (HEAP.load(Relaxed) - before) as f64 / n as f64;
    drop(x);

    // 並べ替えは、作り直した並んでいない列を毎回並べる（作る時間は含めない）
    let mut sort = f64::MAX;
    for _ in 0..3 {
        let mut x = C::build(spec, a, va);
        let t = Instant::now();
        x.sort();
        sort = sort.min(t.elapsed().as_secs_f64() * 1e3);
        black_box(x.len());
    }

    let mut x = C::build(spec, a, va);
    x.sort();
    let mut y = C::build(spec, b, vb);
    y.sort();
    let (join, j) = best_of(3, || x.intersect(&y));
    let (remove, r) = best_of(3, || x.remove(spec, dim));
    // 表し方が違っても同じ結果になることを確かめる
    eprintln!("  {:<9} 結合 {} セル（合計 {:.6e}）、集計 {} セル（合計 {:.6e}）", C::name(), j.len(), j.checksum(), r.len(), r.checksum());
    Row { name: C::name(), bytes, sort, join, remove }
}

fn scenario(title: &str, names: &[&str], sizes: &[u32], n: usize) {
    let spec = Spec::new(sizes);
    let axes: Vec<String> = names.iter().zip(sizes).map(|(a, s)| format!("{a} {s}")).collect();
    println!("\n### {title}（{} ビット、{} 語、{n} セル）\n", spec.total_bits(), spec.words);
    println!("軸: {}\n", axes.join("、"));
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
    let a = gen_coords(&spec, n, &mut rng);
    let b = partner(&spec, &a, &mut rng);
    let va: Vec<f64> = (0..n).map(|_| (rng.below(1000) + 1) as f64).collect();
    let vb: Vec<f64> = (0..b.len() / sizes.len()).map(|_| (rng.below(1000) + 1) as f64).collect();
    // 集計で外すのは、メンバー数の最も多い軸（集計先がいちばん小さくなる）
    let dim = (0..sizes.len()).max_by_key(|&i| sizes[i]).unwrap();

    let mut rows = Vec::new();
    if spec.total_bits() <= 64 {
        rows.push(measure::<U64>(&spec, &a, &va, &b, &vb, dim));
    }
    if spec.words <= 2 {
        rows.push(measure::<Fixed<2>>(&spec, &a, &va, &b, &vb, dim));
    }
    if spec.words == 3 {
        rows.push(measure::<Fixed<3>>(&spec, &a, &va, &b, &vb, dim));
    }
    rows.push(measure::<Stride>(&spec, &a, &va, &b, &vb, dim));
    rows.push(measure::<Heap>(&spec, &a, &va, &b, &vb, dim));

    let base = &rows[0];
    println!("| 表し方 | 1 セルのメモリ | 並べ替え | 結合 | 集計（{} を外す） |", names[dim]);
    println!("|---|---|---|---|---|");
    for r in &rows {
        println!(
            "| {} | {:.1} バイト（{:.2} 倍） | {:.0} ms（{:.2} 倍） | {:.0} ms（{:.2} 倍） | {:.0} ms（{:.2} 倍） |",
            r.name, r.bytes, r.bytes / base.bytes, r.sort, r.sort / base.sort, r.join, r.join / base.join, r.remove, r.remove / base.remove,
        );
    }
}

fn main() {
    let n: usize = std::env::args().nth(1).map(|s| s.parse().expect("セル数は整数")).unwrap_or(5_000_000);
    println!("## キーの表し方の測定（1 スレッド、3 回の最小値）");
    scenario(
        "64 ビットに収まる 6 軸",
        &["Month", "Version", "Entity", "Department", "Account", "Product"],
        &[120, 10, 200, 500, 2_000, 20_000],
        n,
    );
    scenario(
        "64 ビットを超える 7 軸",
        &["Month", "Version", "Entity", "Department", "Account", "Product", "Customer"],
        &[120, 10, 200, 500, 2_000, 20_000, 200_000],
        n,
    );
    scenario(
        "128 ビットを超える 13 軸",
        &["Month", "Version", "Entity", "Department", "Account", "Product", "Customer", "Channel", "Region", "Project", "Currency", "Segment", "Employee"],
        &[120, 10, 200, 500, 2_000, 20_000, 200_000, 50, 300, 5_000, 40, 100, 50_000],
        n,
    );
}
