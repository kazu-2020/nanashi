//! セルの列（キー順のキーと値の組）を扱う小さな道具。件数が多ければ並列に処理する。

#[allow(unused_imports)]
use crate::*;

pub(crate) fn sort_cells(cfg: &Config, v: &mut [(u64, f64)]) {
    if cfg.par(v.len()) {
        v.par_sort_unstable_by_key(|c| c.0);
    } else {
        v.sort_unstable_by_key(|c| c.0);
    }
}

/// 各セルを f で写し、None を捨てる（件数が多ければ並列に）。
pub(crate) fn map_cells<F>(cfg: &Config, cells: &[(u64, f64)], f: F) -> Vec<(u64, f64)>
where
    F: Fn(u64, f64) -> Option<(u64, f64)> + Sync + Send,
{
    if cfg.par(cells.len()) {
        cells.par_iter().filter_map(|&(k, v)| f(k, v)).collect()
    } else {
        cells.iter().filter_map(|&(k, v)| f(k, v)).collect()
    }
}

/// 各セルを複数のセルへ写す（件数が多ければ並列に）。
pub(crate) fn flat_map_cells<F, I>(cfg: &Config, cells: &[(u64, f64)], f: F) -> Vec<(u64, f64)>
where
    F: Fn(u64, f64) -> I + Sync + Send,
    I: Iterator<Item = (u64, f64)>,
{
    if cfg.par(cells.len()) {
        cells.par_iter().flat_map_iter(|&(k, v)| f(k, v)).collect()
    } else {
        cells.iter().flat_map(|&(k, v)| f(k, v)).collect()
    }
}
