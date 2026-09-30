//! 軸の違う Cube の突き合わせと、軸の追加。

#[allow(unused_imports)]
use crate::*;

pub(crate) fn merge(a: &[DimId], b: &[DimId]) -> Vec<DimId> {
    let mut out = a.to_vec();
    out.extend(b.iter().copied().filter(|d| !a.contains(d)));
    out
}

/// 並んだ 2 つのセル列を突き合わせる（sort-merge）。outer なら片側だけのキーも f に None を渡して残す。
pub(crate) fn merge_sorted(
    a: &[(u64, f64)],
    b: &[(u64, f64)],
    outer: bool,
    f: impl Fn(Option<f64>, Option<f64>) -> Option<f64>,
) -> Vec<(u64, f64)> {
    let mut out = Vec::with_capacity(if outer { a.len() + b.len() } else { a.len().min(b.len()) });
    let mut push = |k: u64, x: Option<f64>| {
        if let Some(x) = x {
            out.push((k, x));
        }
    };
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        let (x, y) = (a[i].0, b[j].0);
        if x == y {
            push(x, f(Some(a[i].1), Some(b[j].1)));
            i += 1;
            j += 1;
        } else if x < y {
            if outer {
                push(x, f(Some(a[i].1), None));
            }
            i += 1;
        } else {
            if outer {
                push(y, f(None, Some(b[j].1)));
            }
            j += 1;
        }
    }
    if outer {
        for &(k, v) in &a[i..] {
            push(k, f(Some(v), None));
        }
        for &(k, v) in &b[j..] {
            push(k, f(None, Some(v)));
        }
    }
    out
}

pub(crate) fn sorted(cfg: &Config, mut cells: Vec<(u64, f64)>) -> Vec<(u64, f64)> {
    sort_cells(cfg, &mut cells);
    cells
}

/// INNER JOIN。両側に値があるセルだけ結果を持つ。f が None を返したセルは空。
pub(crate) fn intersect(a: &Cube, b: &Cube, kind: Kind, cat: &Catalog, f: impl Fn(f64, f64) -> Option<f64> + Sync) -> Result<Cube> {
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
        let (x, y) = (sorted(cfg, a.cells.clone()), sorted(cfg, b.repack(cfg, &a.pack).cells));
        let cells = merge_sorted(&x, &y, false, |p, q| f(p.unwrap(), q.unwrap()));
        return Ok(Cube { pack: a.pack.clone(), kind, cells });
    }

    let shared: Vec<DimId> = a.dims().iter().copied().filter(|d| b.dims().contains(d)).collect();
    let extra: Vec<DimId> = b.dims().iter().copied().filter(|d| !a.dims().contains(d)).collect();
    let out = Packing::new(&[a.dims(), &extra[..]].concat(), cat)?;
    let sk = Packing::new(&shared, cat)?;
    let (a_sk, b_sk) = (Proj::new(&a.pack, &sk), Proj::new(&b.pack, &sk));
    let (a_out, b_out) = (Proj::new(&a.pack, &out), Proj::new(&b.pack, &out));

    // b を共有する軸のキーで並べ、キーごとの区間を引けるようにする
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
    let mut index: FxHashMap<u64, (usize, usize)> = FxHashMap::default();
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
    let cells = flat_map_cells(cfg, &a.cells, |k, v| {
        let range = index.get(&a_sk.apply(&a.pack, &sk, k)).copied().unwrap_or((0, 0));
        let base = a_out.apply(&a.pack, &out, k);
        entries[range.0..range.1].iter().filter_map(move |&(_, bk, bv)| f(v, bv).map(|x| (base | bk, x)))
    });
    Ok(Cube { pack: out, kind, cells })
}

/// 足りない軸の全メンバー（restrict の範囲）へ値を複製する。
pub(crate) fn expand(c: &Cube, dims: &[DimId], cat: &Catalog, r: &Restrict, b: &Budget) -> Result<Cube> {
    let cfg = &cat.cfg;
    let out = Packing::new(dims, cat)?;
    if out == c.pack {
        return Ok(c.clone());
    }
    let proj = Proj::new(&c.pack, &out);
    let missing: Vec<(usize, Vec<u32>)> = dims
        .iter()
        .enumerate()
        .filter(|(_, d)| !c.dims().contains(d))
        .map(|(i, &d)| (i, members(cat, d, r)))
        .collect();
    let n: usize = missing.iter().map(|(_, ms)| ms.len()).product();
    b.need(n.saturating_mul(c.cells.len().max(1)).saturating_add(n), "EXPAND（軸の全メンバーへの展開）")?;
    let combos = product(&out, &missing);
    let combos = &combos;
    let cells = flat_map_cells(cfg, &c.cells, |k, v| {
        let base = proj.apply(&c.pack, &out, k);
        combos.iter().map(move |&extra| (base | extra, v))
    });
    Ok(Cube { pack: out, kind: c.kind, cells })
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

/// FULL OUTER JOIN（両側とも同じ詰め方）。片側だけのセルは f に None を渡す。
pub(crate) fn union(cfg: &Config, a: Cube, b: Cube, kind: Kind, f: impl Fn(Option<f64>, Option<f64>) -> Option<f64>) -> Cube {
    let (x, y) = (sorted(cfg, a.cells), sorted(cfg, b.cells));
    Cube { pack: a.pack, kind, cells: merge_sorted(&x, &y, true, f) }
}

/// c に現れるメンバーだけに各軸を絞った範囲。c が大きければ None（絞っても得をしない）。
///
/// INNER JOIN の結果は c に値があるセルにしか残らないので、もう片側をこの範囲で評価しても
/// 結果は変わらない。もう片側が持たない軸の絞り込みは、その軸を外す演算が捨てるので害がない。
pub(crate) fn semi(r: &Restrict, c: &Cube, cat: &Catalog) -> Option<Restrict> {
    if c.dims().is_empty() || c.cells.len() > cat.cfg.semi_max {
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
    let n: usize = spans.iter().map(|(_, ms)| ms.len()).product();
    b.need(n.saturating_mul(2), "ISBLANK / IFBLANK（軸の全組み合わせ）")?;
    Ok(product(&c.pack, &spans))
}
