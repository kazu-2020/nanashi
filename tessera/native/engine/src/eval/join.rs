//! 軸の違う Cube の突き合わせと、軸の追加。

#[allow(unused_imports)]
use crate::*;
use std::borrow::Cow;

pub(crate) fn merge(a: &[DimId], b: &[DimId]) -> Vec<DimId> {
    let mut out = a.to_vec();
    out.extend(b.iter().copied().filter(|d| !a.contains(d)));
    out
}

/// Cells in ascending key order. The cells of a Cube, or the base of stored data without a delta (no copy).
pub(crate) trait Cells {
    fn n(&self) -> usize;
    fn iter(&self) -> impl Iterator<Item = (u64, f64)> + '_;
}

impl Cells for [(u64, f64)] {
    #[inline]
    fn n(&self) -> usize {
        self.len()
    }

    #[inline]
    fn iter(&self) -> impl Iterator<Item = (u64, f64)> + '_ {
        <[(u64, f64)]>::iter(self).copied()
    }
}

impl Cells for Store {
    #[inline]
    fn n(&self) -> usize {
        self.base_rows().expect("差分のある格納データは番号で読めない")
    }

    #[inline]
    fn iter(&self) -> impl Iterator<Item = (u64, f64)> + '_ {
        self.base_range(0..self.n())
    }
}

/// Merges 2 sorted cell columns (sort-merge). With `outer`, a key on one side only stays, and f gets None
/// for the other side.
pub(crate) fn merge_sorted<A: Cells + ?Sized, B: Cells + ?Sized>(
    a: &A,
    b: &B,
    outer: bool,
    f: impl Fn(Option<f64>, Option<f64>) -> Option<f64>,
) -> Vec<(u64, f64)> {
    let (an, bn) = (a.n(), b.n());
    let mut out = Vec::with_capacity(if outer { an + bn } else { an.min(bn) });
    let mut push = |k: u64, x: Option<f64>| {
        if let Some(x) = x {
            out.push((k, x));
        }
    };
    let (mut a, mut b) = (a.iter().peekable(), b.iter().peekable());
    while let (Some(&(x, xv)), Some(&(y, yv))) = (a.peek(), b.peek()) {
        if x == y {
            push(x, f(Some(xv), Some(yv)));
            a.next();
            b.next();
        } else if x < y {
            if outer {
                push(x, f(Some(xv), None));
            }
            a.next();
        } else {
            if outer {
                push(y, f(None, Some(yv)));
            }
            b.next();
        }
    }
    if outer {
        for (k, v) in a {
            push(k, f(Some(v), None));
        }
        for (k, v) in b {
            push(k, f(None, Some(v)));
        }
    }
    out
}

pub(crate) fn sorted(cfg: &Config, mut cells: Vec<(u64, f64)>) -> Vec<(u64, f64)> {
    sort_cells(cfg, &mut cells);
    cells
}

/// セルをキーの昇順に。すでに並んでいれば（格納データから読んだセルなど）写さずに借りる。
pub(crate) fn in_order<'a>(cfg: &Config, cells: &'a [(u64, f64)]) -> Cow<'a, [(u64, f64)]> {
    if cells.windows(2).all(|w| w[0].0 < w[1].0) {
        Cow::Borrowed(cells)
    } else {
        Cow::Owned(sorted(cfg, cells.to_vec()))
    }
}

/// INNER JOIN. Only the cells with a value on both sides are in the result. A cell is empty if `f` gives None.
/// The result counts as used in `bud`. The buffers of the join count only while the join makes the result.
pub(crate) fn intersect(
    a: &Cube,
    b: &Cube,
    kind: Kind,
    cat: &Catalog,
    bud: &Budget,
    f: impl Fn(f64, f64) -> Option<f64> + Sync,
) -> Result<Cube> {
    let start = bud.mark();
    let out = join(a, b, kind, cat, bud, f)?;
    bud.settle(start, &out)?;
    Ok(out)
}

fn join(a: &Cube, b: &Cube, kind: Kind, cat: &Catalog, bud: &Budget, f: impl Fn(f64, f64) -> Option<f64> + Sync) -> Result<Cube> {
    let cfg = &cat.cfg;
    // 片側が定数（軸なし）なら、結合せずに 1 回の走査で済む
    if b.dims().is_empty() {
        let Some(&(_, bv)) = b.cells.first() else { return Ok(Cube { pack: a.pack.clone(), kind, cells: Vec::new() }) };
        let cells = map_cells(cfg, &a.cells, |k, v| f(v, bv).map(|x| (k, x)));
        return Ok(Cube { pack: a.pack.clone(), kind, cells });
    }
    if a.dims().is_empty() {
        let Some(&(_, av)) = a.cells.first() else { return Ok(Cube { pack: b.pack.clone(), kind, cells: Vec::new() }) };
        let cells = map_cells(cfg, &b.cells, |k, v| f(av, v).map(|x| (k, x)));
        return Ok(Cube { pack: b.pack.clone(), kind, cells });
    }
    // 同じ軸どうしなら、並べて突き合わせる
    if same_set(a.dims(), b.dims()) {
        let x = in_order(cfg, &a.cells);
        let y = if b.pack == a.pack { in_order(cfg, &b.cells) } else { Cow::Owned(sorted(cfg, b.repack(cfg, &a.pack).cells)) };
        let cells = merge_sorted(&*x, &*y, false, |p, q| f(p.unwrap(), q.unwrap()));
        return Ok(Cube { pack: a.pack.clone(), kind, cells });
    }

    let shared: Vec<DimId> = a.dims().iter().copied().filter(|d| b.dims().contains(d)).collect();
    let extra: Vec<DimId> = b.dims().iter().copied().filter(|d| !a.dims().contains(d)).collect();
    let out = Packing::new(&[a.dims(), &extra[..]].concat(), cat)?;
    let sk = Packing::new(&shared, cat)?;
    let (a_sk, b_sk) = (Proj::new(&a.pack, &sk), Proj::new(&b.pack, &sk));
    let (a_out, b_out) = (Proj::new(&a.pack, &out), Proj::new(&b.pack, &out));

    const WHAT: &str = "軸の違う式の結合";
    // Sort b by the key of the shared dimensions, and index the run of each key. An entry has 24 bytes.
    bud.take(b.cells.len().saturating_mul(3).div_ceil(2), WHAT)?;
    let entry = |&(k, v): &(u64, f64)| (b_sk.apply(&b.pack, &sk, k), b_out.apply(&b.pack, &out, k), v);
    let entries: Vec<(u64, u64, f64)> = if cfg.par(b.cells.len()) {
        let mut e: Vec<_> = b.cells.par_iter().map(entry).collect();
        e.par_sort_unstable_by_key(|e| e.0);
        e
    } else {
        let mut e: Vec<_> = b.cells.iter().map(entry).collect();
        e.sort_unstable_by_key(|e| e.0);
        e
    };
    let keys = entries.chunk_by(|x, y| x.0 == y.0).count();
    // A hash table has a power of 2 of slots, at least 8/7 of the keys. A slot has 24 bytes and 1 control byte.
    bud.take(((keys.saturating_mul(8) / 7).max(4).next_power_of_two().saturating_mul(25)).div_ceil(CELL_BYTES), WHAT)?;
    let mut index: FxHashMap<u64, (usize, usize)> = FxHashMap::with_capacity_and_hasher(keys, Default::default());
    let mut i = 0;
    while i < entries.len() {
        let mut j = i;
        while j < entries.len() && entries[j].0 == entries[i].0 {
            j += 1;
        }
        index.insert(entries[i].0, (i, j));
        i = j;
    }

    let (index, entries, f) = (&index, &entries, &f);
    let range = |k: u64| index.get(&a_sk.apply(&a.pack, &sk, k)).copied().unwrap_or((0, 0));
    // If each key of b has one entry, |a| is an upper bound of the pairs. Count them exactly only when it does not fit.
    if !(index.len() == entries.len() && bud.need(a.cells.len(), WHAT).is_ok()) {
        let pairs = |&(k, _): &(u64, f64)| {
            let (i, j) = range(k);
            j - i
        };
        let n = if cfg.par(a.cells.len()) {
            a.cells.par_iter().map(pairs).reduce(|| 0, usize::saturating_add)
        } else {
            a.cells.iter().map(pairs).fold(0, usize::saturating_add)
        };
        bud.need(n, WHAT)?;
    }
    let cells = flat_map_cells(cfg, &a.cells, |k, v| {
        let (i, j) = range(k);
        let base = a_out.apply(&a.pack, &out, k);
        entries[i..j].iter().filter_map(move |&(_, bk, bv)| f(v, bv).map(|x| (base | bk, x)))
    });
    Ok(Cube { pack: out, kind, cells })
}

/// Copies the values to all members (in the restrict) of the dimensions that `c` does not have.
pub(crate) fn expand(c: Cube, dims: &[DimId], cat: &Catalog, r: &Restrict, b: &Budget) -> Result<Cube> {
    let cfg = &cat.cfg;
    let out = Packing::new(dims, cat)?;
    if out == c.pack {
        return Ok(c);
    }
    let proj = Proj::new(&c.pack, &out);
    let missing: Vec<(usize, Vec<u32>)> = dims
        .iter()
        .enumerate()
        .filter(|(_, d)| !c.dims().contains(d))
        .map(|(i, &d)| (i, members(cat, d, r)))
        .collect();
    let n = combos(&missing);
    b.need(n.saturating_mul(c.cells.len().max(1)).saturating_add(n), "EXPAND（軸の全メンバーへの展開）")?;
    let combos = product(&out, &missing);
    let combos = &combos;
    let cells = flat_map_cells(cfg, &c.cells, |k, v| {
        let base = proj.apply(&c.pack, &out, k);
        combos.iter().map(move |&extra| (base | extra, v))
    });
    Ok(Cube { pack: out, kind: c.kind, cells })
}

/// Gives the number of combinations of the members in `spans`. Stops at `usize::MAX` and does not wrap.
pub(crate) fn combos(spans: &[(usize, Vec<u32>)]) -> usize {
    spans.iter().fold(1, |n, (_, ms)| n.saturating_mul(ms.len()))
}

/// 与えた軸（位置, メンバー一覧）の全組み合わせを、pack の詰め方のキーの部分として列挙する。
pub(crate) fn product(pack: &Packing, spans: &[(usize, Vec<u32>)]) -> Vec<u64> {
    let mut out = vec![0u64];
    for (pos, ms) in spans {
        let mut next = Vec::with_capacity(out.len() * ms.len());
        for &base in &out {
            for &m in ms {
                next.push(base | pack.put(*pos, m));
            }
        }
        out = next;
    }
    out
}

/// FULL OUTER JOIN. Both sides have the same packing. If a cell is on one side only, f gets None for the other side.
pub(crate) fn union(cfg: &Config, bud: &Budget, a: Cube, b: Cube, kind: Kind, f: impl Fn(Option<f64>, Option<f64>) -> Option<f64>) -> Result<Cube> {
    bud.need(a.cells.len().saturating_add(b.cells.len()), "和の結合")?;
    let (x, y) = (sorted(cfg, a.cells), sorted(cfg, b.cells));
    Ok(Cube { pack: a.pack, kind, cells: merge_sorted(&x[..], &y[..], true, f) })
}

/// If the left side of an INNER JOIN has this number of cells or fewer, evaluate the right side only for the members of the left side.
const SEMI_MAX: usize = 4096;

/// c に現れるメンバーだけに各軸を絞った範囲。c が大きければ None（絞っても得をしない）。
///
/// INNER JOIN の結果は c に値があるセルにしか残らないので、もう片側をこの範囲で評価しても
/// 結果は変わらない。もう片側が持たない軸の絞り込みは、その軸を外す演算が捨てるので害がない。
pub(crate) fn semi(r: &Restrict, c: &Cube, cat: &Catalog) -> Option<Restrict> {
    if c.dims().is_empty() || c.cells.len() > SEMI_MAX {
        return None;
    }
    let mut out = r.clone();
    for (i, &d) in c.dims().iter().enumerate() {
        let ms = c.cells.iter().map(|&(k, _)| c.pack.get(k, i)).collect();
        out = out.with(d, Sel::new(ms, cat.dims[d].size));
    }
    Some(out)
}

pub(crate) fn replace_dim(dims: &[DimId], old: DimId, new: DimId) -> Vec<DimId> {
    dims.iter().map(|&d| if d == old { new } else { d }).collect()
}

pub(crate) fn dense(c: &Cube, cat: &Catalog, r: &Restrict, b: &Budget) -> Result<Vec<u64>> {
    let spans: Vec<(usize, Vec<u32>)> = c.dims().iter().enumerate().map(|(i, &d)| (i, members(cat, d, r))).collect();
    let n = combos(&spans);
    b.need(n.saturating_mul(2), "ISBLANK / IFBLANK（軸の全組み合わせ）")?;
    Ok(product(&c.pack, &spans))
}

#[cfg(test)]
mod tests {
    use super::combos;

    #[test]
    fn combos_saturates_instead_of_wrapping_to_zero() {
        let members = 1usize << 16;
        let spans: Vec<(usize, Vec<u32>)> = (0..4).map(|i| (i, (0..members as u32).collect())).collect();
        assert_eq!(combos(&spans), usize::MAX);
        assert_eq!(combos(&spans[..3]), members.pow(3));
        assert_eq!(combos(&[]), 1);
    }
}
