//! 格納データの本体の平らな形式（保存と読み込み用）。
//!
//! Store の本体（キー順の u64 のキーと f64 の値の 2 列）を、作り直さずに読めるバイト列にする。
//! 読むときは、詰め方（軸の並びとビット幅）が今の Catalog で作る詰め方と同じなら、バイト列の持ち主を
//! そのまま列の持ち主にして参照する（`Column::from_bytes`。写さない）。違えば詰め直す。
//! Parquet（`pq.rs`）は列を 1 つずつ作り直して符号化するので、交換用の形式として残し、スナップショットには
//! こちらを使う。
//!
//! 並びは次のとおり（数はすべてリトルエンディアン。ヘッダは 8 バイトの倍数なので、続く列は 8 バイトにそろう）。
//!
//! | 位置 | 大きさ | 内容 |
//! |---|---|---|
//! | 0 | 8 | 印 `NSHFLAT1` |
//! | 8 | 4 | 形式の版（1） |
//! | 12 | 4 | 旗（ビット 0: 値が真偽値） |
//! | 16 | 8 | 行数 |
//! | 24 | 4 | 詰め方の軸の数 n |
//! | 28 | 4 | 予備（0） |
//! | 32 | 8 × n | 詰め方の順（分割軸が先頭）に、軸の名札（呼び出し側が決める u32。モデルの軸の ID）とビット幅 |
//! | 32 + 8n | 8 × 行数 | キー（昇順） |
//! | ... | 8 × 行数 | 値 |
//!
//! 軸の名札は Catalog の番号（DimId）ではなく呼び出し側が渡す（Catalog の番号はプロセスごとに違いうる）。
//! キーのビット幅はメンバー数で決まるので、同じモデル定義（`model.json`）と組で読む。

#[allow(unused_imports)]
use crate::*;

const MAGIC: &[u8; 8] = b"NSHFLAT1";
const VERSION: u32 = 1;
const FLAG_BOOL: u32 = 1;
const HEAD: usize = 32;

/// ヘッダの大きさ（バイト。8 の倍数）。
fn header_len(n_dims: usize) -> usize {
    HEAD + 8 * n_dims
}

/// T の列をバイト列として見る（Plain な型なので、どのビット列も値として正しい）。
fn bytes_of<T: Plain>(s: &[T]) -> &[u8] {
    // SAFETY: Plain な T は内部にポインタを持たず、どのビット列も有効な値である
    unsafe { std::slice::from_raw_parts(s.as_ptr() as *const u8, std::mem::size_of_val(s)) }
}

/// バイト列を T の列に写す（そろっていなくてもよい）。
fn read_plain<T: Plain>(b: &[u8]) -> Vec<T> {
    let n = b.len() / std::mem::size_of::<T>();
    let mut out: Vec<T> = Vec::with_capacity(n);
    // SAFETY: 出力は n 個分の容量があり、入力は n × size_of::<T>() バイト以上ある。T は Plain なので
    // どのビット列も有効な値である
    unsafe {
        std::ptr::copy_nonoverlapping(b.as_ptr(), out.as_mut_ptr() as *mut u8, n * std::mem::size_of::<T>());
        out.set_len(n);
    }
    out
}

fn u32_at(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}

fn u64_at(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}

/// ヘッダの中身。
struct Header {
    kind: Kind,
    rows: usize,
    dims: Vec<(u32, u32)>, // 詰め方の順に（名札, ビット幅）
}

impl Header {
    fn parse(b: &[u8]) -> Result<Header> {
        if b.len() < HEAD || &b[..8] != MAGIC {
            return Err("平らな形式の印がない".into());
        }
        let version = u32_at(b, 8);
        if version != VERSION {
            return Err(format!("平らな形式の版 {version} には対応していない（{VERSION} まで）"));
        }
        let flags = u32_at(b, 12);
        let rows = usize::try_from(u64_at(b, 16)).map_err(|_| "行数が大きすぎる")?;
        let n = u32_at(b, 24) as usize;
        if b.len() < header_len(n) {
            return Err("ヘッダが途中で切れている".into());
        }
        let dims = (0..n).map(|i| (u32_at(b, HEAD + 8 * i), u32_at(b, HEAD + 8 * i + 4))).collect();
        Ok(Header { kind: if flags & FLAG_BOOL != 0 { Kind::Bool } else { Kind::Num }, rows, dims })
    }

    fn write(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&(if self.kind == Kind::Bool { FLAG_BOOL } else { 0 }).to_le_bytes());
        out.extend_from_slice(&(self.rows as u64).to_le_bytes());
        out.extend_from_slice(&(self.dims.len() as u32).to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        for &(label, bits) in &self.dims {
            out.extend_from_slice(&label.to_le_bytes());
            out.extend_from_slice(&bits.to_le_bytes());
        }
    }
}

fn bits_of(pack: &Packing) -> Vec<u32> {
    pack.masks.iter().map(|m| m.count_ones()).collect()
}

/// 詰め方の順の名札（labels は宣言した軸の順）。
fn packed_labels(pack: &Packing, metric_dims: &[DimId], labels: &[u32]) -> Vec<u32> {
    pack.dims.iter().map(|d| labels[metric_dims.iter().position(|x| x == d).unwrap()]).collect()
}

impl Store {
    /// 本体を平らな形式のバイト列にする。labels は宣言した軸の順の、軸の名札（モデルの軸の ID）。
    /// 差分があれば本体と突き合わせて 1 本にする。
    pub fn to_flat(&self, labels: &[u32]) -> Result<Vec<u8>> {
        if labels.len() != self.metric_dims.len() {
            return Err("軸の名札の数が軸の数と合わない".into());
        }
        let dims: Vec<(u32, u32)> = packed_labels(&self.pack, &self.metric_dims, labels).into_iter().zip(bits_of(&self.pack)).collect();
        let (keys, vals): (Vec<u64>, Vec<f64>);
        let (k, v): (&[u64], &[f64]) = if self.delta.is_empty() {
            (&self.base.keys, &self.base.vals)
        } else {
            let mut cells = Vec::with_capacity(self.base.keys.len() + self.delta.len());
            self.merged(0, None, |k, v| cells.push((k, v)));
            (keys, vals) = cells.into_iter().unzip();
            (&keys, &vals)
        };
        let mut out = Vec::with_capacity(header_len(dims.len()) + 16 * k.len());
        Header { kind: self.kind, rows: k.len(), dims }.write(&mut out);
        out.extend_from_slice(bytes_of(k));
        out.extend_from_slice(bytes_of(v));
        Ok(out)
    }

    /// 平らな形式のバイト列（の持ち主 owner）から Store を作る。引数は Store::new と同じで、labels は宣言した
    /// 軸の順の名札。詰め方（軸の並びとビット幅）がバイト列のものと同じなら、キーと値は owner を参照して
    /// 写さない。違えば詰め直す。キーが昇順で、メンバー番号が軸の大きさ未満であることも確かめる。
    pub fn from_flat<O: ColumnOwner + AsRef<[u8]>>(
        owner: Arc<O>,
        metric_dims: &[DimId],
        labels: &[u32],
        index: Option<DimId>,
        kind: Kind,
        cat: &Catalog,
    ) -> Result<Store> {
        if cfg!(target_endian = "big") {
            return Err("平らな形式はリトルエンディアンの機械でだけ読める".into());
        }
        if labels.len() != metric_dims.len() {
            return Err("軸の名札の数が軸の数と合わない".into());
        }
        let mut store = Store::new(metric_dims, index, kind, cat)?;
        let bytes: &[u8] = (*owner).as_ref();
        let h = Header::parse(bytes)?;
        if h.kind != kind {
            return Err("値の種類が保存したものと違う".into());
        }
        let at = header_len(h.dims.len());
        let need = at.checked_add(h.rows.checked_mul(16).ok_or("行数が大きすぎる")?).ok_or("行数が大きすぎる")?;
        if bytes.len() < need {
            return Err(format!("バイト列が行数 {} に足りない（{} < {need} バイト）", h.rows, bytes.len()));
        }
        let want: Vec<(u32, u32)> = packed_labels(&store.pack, metric_dims, labels).into_iter().zip(bits_of(&store.pack)).collect();
        if h.dims.iter().map(|d| d.0).ne(store.pack.dims.iter().map(|d| labels[metric_dims.iter().position(|x| x == d).unwrap()])) {
            // 軸の並びが違う（分割軸が違うか、名札が合わない）。同じ軸の集まりなら詰め直す
            let mut sorted: Vec<u32> = h.dims.iter().map(|d| d.0).collect();
            sorted.sort_unstable();
            let mut mine = labels.to_vec();
            mine.sort_unstable();
            if sorted != mine {
                return Err(format!("保存した軸 {sorted:?} が Metric の軸 {mine:?} と合わない"));
            }
        }
        let same = h.dims == want;
        let keys: Column<u64>;
        let vals: Column<f64>;
        match (Column::<u64>::from_bytes(&owner, at, h.rows), Column::<f64>::from_bytes(&owner, at + 8 * h.rows, h.rows)) {
            (Ok(k), Ok(v)) => (keys, vals) = (k, v),
            _ => {
                // そろっていないバイト列（まれ）。列だけ写す
                keys = read_plain::<u64>(&bytes[at..at + 8 * h.rows]).into();
                vals = read_plain::<f64>(&bytes[at + 8 * h.rows..at + 16 * h.rows]).into();
            }
        }
        if same {
            check_keys(&keys, &store.pack, cat, &store.cfg)?;
            store.base = Arc::new(Base { keys, vals, postings: fresh_postings(store.pack.dims.len()) });
            return Ok(store);
        }
        // 保存した詰め方から今の詰め方へ写す。軸の並びが同じならキーの順序は保たれるが、念のため並べ直す
        let src = packing_of(&h, metric_dims, labels)?;
        check_keys(&keys, &src, cat, &store.cfg)?;
        let proj = Proj::new(&src, &store.pack);
        let mut cells: Vec<(u64, f64)> = keys.iter().zip(vals.iter()).map(|(&k, &v)| (proj.apply(&src, &store.pack, k), v)).collect();
        sort_cells(&store.cfg, &mut cells);
        store.set_sorted(cells);
        Ok(store)
    }
}

/// ヘッダの詰め方を Packing にする（名札を Catalog の番号に戻す）。
fn packing_of(h: &Header, metric_dims: &[DimId], labels: &[u32]) -> Result<Packing> {
    let dims: Vec<DimId> = h
        .dims
        .iter()
        .map(|&(label, _)| labels.iter().position(|&l| l == label).map(|i| metric_dims[i]).ok_or_else(|| format!("名札 {label} の軸がない")))
        .collect::<Result<_>>()?;
    let bits: Vec<u32> = h.dims.iter().map(|d| d.1).collect();
    if bits.iter().any(|&b| b == 0 || b > 64) || bits.iter().sum::<u32>() > 64 {
        return Err("保存したビット幅が正しくない".into());
    }
    let mut shifts = vec![0; dims.len()];
    let mut acc = 0;
    for i in (0..dims.len()).rev() {
        shifts[i] = acc;
        acc += bits[i];
    }
    let masks = bits.iter().map(|&b| if b == 64 { u64::MAX } else { (1u64 << b) - 1 }).collect();
    Ok(Packing { dims, shifts, masks })
}

/// キーが昇順（重複なし）で、各軸のメンバー番号が軸の大きさ未満か。
fn check_keys(keys: &[u64], pack: &Packing, cat: &Catalog, cfg: &Config) -> Result<()> {
    let sorted = if cfg.par(keys.len()) { keys.par_windows(2).all(|w| w[0] < w[1]) } else { keys.windows(2).all(|w| w[0] < w[1]) };
    if !sorted {
        return Err("保存したキーが昇順に並んでいない".into());
    }
    for (pos, &d) in pack.dims.iter().enumerate() {
        let size = cat.dims[d].size;
        if pack.masks[pos] < size as u64 {
            continue; // ビット幅に収まる番号はすべて軸の大きさ未満
        }
        let max = if cfg.par(keys.len()) {
            keys.par_iter().map(|&k| pack.get(k, pos)).max()
        } else {
            keys.iter().map(|&k| pack.get(k, pos)).max()
        };
        if let Some(m) = max.filter(|&m| m >= size) {
            return Err(format!("{} のメンバー番号 {m} が軸の大きさ {size} を超える", cat.dims[d].name));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn catalog(sizes: &[u32]) -> Catalog {
        Catalog {
            dims: sizes.iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect(),
            maps: Vec::new(),
            cfg: Config::default(),
        }
    }

    /// 行を (宣言した軸の順のメンバー番号, 値) にして並べる（詰め方によらず比べるため）。
    fn cells(s: &Store) -> Vec<(Vec<u32>, u64)> {
        let (cols, vals) = s.rows();
        let mut out: Vec<(Vec<u32>, u64)> = (0..vals.len()).map(|r| (cols.iter().map(|c| c[r]).collect(), vals[r].to_bits())).collect();
        out.sort();
        out
    }

    fn store(cat: &Catalog, index: Option<DimId>) -> Store {
        let cols: Vec<&[u32]> = vec![&[0, 1, 2, 2], &[3, 0, 1, 2]];
        Store::new(&[0, 1], index, Kind::Num, cat).unwrap().with_rows(&cols, &[1.0, 2.5, -3.0, 4.0]).unwrap()
    }

    #[test]
    fn round_trip_shares_the_bytes() {
        let cat = catalog(&[3, 4]);
        let s = store(&cat, Some(1));
        let buf = s.to_flat(&[10, 20]).unwrap();
        assert_eq!(buf.len(), 32 + 16 + 16 * 4);
        let owner = Arc::new(buf.into_boxed_slice());
        let t = Store::from_flat(owner.clone(), &[0, 1], &[10, 20], Some(1), Kind::Num, &cat).unwrap();
        assert_eq!(t.as_cube().cells, s.as_cube().cells);
        assert!(t.base.keys.same_owner(&t.base.vals));
        assert_eq!(t.memory().base_heap, owner.len());
        assert_eq!(Arc::strong_count(&owner), 3); // キーと値の列が持ち主を参照している
    }

    #[test]
    fn delta_is_merged_when_writing() {
        let cat = catalog(&[3, 4]);
        let mut s = store(&cat, None);
        s.write(&[1, 1], Some(9.0));
        s.write(&[0, 3], None);
        let buf = s.to_flat(&[1, 2]).unwrap();
        let t = Store::from_flat(Arc::new(buf.into_boxed_slice()), &[0, 1], &[1, 2], None, Kind::Num, &cat).unwrap();
        assert_eq!(t.as_cube().cells, s.as_cube().cells);
        assert_eq!(t.len(), 4);
    }

    #[test]
    fn different_packing_is_repacked() {
        let cat = catalog(&[3, 4]);
        let s = store(&cat, Some(1));
        let buf = Arc::new(s.to_flat(&[10, 20]).unwrap().into_boxed_slice());
        // 分割軸なしで読む（軸の並びが変わる）
        let t = Store::from_flat(buf.clone(), &[0, 1], &[10, 20], None, Kind::Num, &cat).unwrap();
        assert_eq!(cells(&t), cells(&s)); // 詰め方が違うのでキーでなく行で比べる
        assert!(!t.base.keys.same_owner(&t.base.vals));
        // メンバーが増えてビット幅が変わった Catalog で読む
        let grown = catalog(&[3, 40]);
        let t = Store::from_flat(buf.clone(), &[0, 1], &[10, 20], Some(1), Kind::Num, &grown).unwrap();
        assert_eq!(cells(&t), cells(&s));
        // 宣言した軸の順が違っても、名札で対応づける
        let t = Store::from_flat(buf, &[1, 0], &[20, 10], Some(1), Kind::Num, &cat).unwrap();
        let swapped: Vec<(Vec<u32>, u64)> = cells(&t).into_iter().map(|(k, v)| (vec![k[1], k[0]], v)).collect();
        let mut swapped = swapped;
        swapped.sort();
        assert_eq!(swapped, cells(&s));
    }

    #[test]
    fn rejects_broken_input() {
        let cat = catalog(&[3, 4]);
        let s = store(&cat, Some(1));
        let buf = s.to_flat(&[10, 20]).unwrap();
        let read = |b: Vec<u8>, labels: &[u32], kind: Kind, cat: &Catalog| {
            Store::from_flat(Arc::new(b.into_boxed_slice()), &[0, 1], labels, Some(1), kind, cat).map(|_| ())
        };
        assert!(read(buf[..20].to_vec(), &[10, 20], Kind::Num, &cat).unwrap_err().contains("ヘッダ") || read(buf[..20].to_vec(), &[10, 20], Kind::Num, &cat).is_err());
        assert!(read(buf[..buf.len() - 1].to_vec(), &[10, 20], Kind::Num, &cat).unwrap_err().contains("足りない"));
        assert!(read(buf.clone(), &[10, 21], Kind::Num, &cat).unwrap_err().contains("合わない"));
        assert!(read(buf.clone(), &[10, 20], Kind::Bool, &cat).unwrap_err().contains("値の種類"));
        let mut bad = buf.clone();
        bad[..8].copy_from_slice(b"XXXXXXXX");
        assert!(read(bad, &[10, 20], Kind::Num, &cat).unwrap_err().contains("印"));
        // 軸が小さくなった Catalog では、メンバー番号がはみ出す
        assert!(read(buf.clone(), &[10, 20], Kind::Num, &catalog(&[3, 3])).unwrap_err().contains("超える"));
        // キーの順序を壊す
        let mut unsorted = buf.clone();
        unsorted[48..56].copy_from_slice(&u64::MAX.to_le_bytes());
        assert!(read(unsorted, &[10, 20], Kind::Num, &cat).unwrap_err().contains("昇順"));
    }

    #[test]
    fn unaligned_bytes_are_copied() {
        let cat = catalog(&[3, 4]);
        let s = store(&cat, Some(1));
        let mut buf = vec![0u8; 1];
        buf.extend(s.to_flat(&[10, 20]).unwrap());
        struct Shifted(Vec<u8>);
        impl ColumnOwner for Shifted {
            fn heap_bytes(&self) -> usize {
                self.0.len()
            }
        }
        impl AsRef<[u8]> for Shifted {
            fn as_ref(&self) -> &[u8] {
                &self.0[1..]
            }
        }
        let owner = Arc::new(Shifted(buf));
        let t = Store::from_flat(owner.clone(), &[0, 1], &[10, 20], Some(1), Kind::Num, &cat).unwrap();
        assert_eq!(t.as_cube().cells, s.as_cube().cells);
        assert_eq!(Arc::strong_count(&owner), 1); // 写したので持ち主を参照していない
    }

    #[test]
    fn empty_store() {
        let cat = catalog(&[3, 4]);
        let s = Store::new(&[0, 1], None, Kind::Bool, &cat).unwrap();
        let buf = s.to_flat(&[1, 2]).unwrap();
        assert_eq!(buf.len(), 32 + 16);
        let t = Store::from_flat(Arc::new(buf.into_boxed_slice()), &[0, 1], &[1, 2], None, Kind::Bool, &cat).unwrap();
        assert!(t.is_empty());
    }
}
