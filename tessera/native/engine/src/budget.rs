//! 評価のメモリの予算。1 つの式の評価（木を 1 本たどる間）が同時に持つ途中結果のバイト数に上限を設ける。
//!
//! 途中結果は、各ノードの評価が終わったときに数える（子の結果は親の評価が終わると捨てられるので、
//! 親の結果に置き換える）。全組み合わせを作る演算（EXPAND、ISBLANK、IFBLANK）は、作る前に要る量を確かめる。
//! 超えるなら、確保する前に Err にする。

use crate::*;
use std::cell::Cell;

/// 途中結果の 1 セルのバイト数（キーと値）。
pub const CELL_BYTES: usize = 16;

/// 1 つの評価の予算。並列に評価する式は、それぞれ別の Budget を持つ（計画が予算を分ける）。
pub struct Budget {
    limit: usize,
    used: Cell<usize>,
}

impl Budget {
    pub fn new(limit: usize) -> Budget {
        Budget { limit, used: Cell::new(0) }
    }

    /// 今持っている途中結果のバイト数。
    pub fn used(&self) -> usize {
        self.used.get()
    }

    /// cells セルをこれから作ってよいか。今の途中結果と足して上限を超えるなら Err。
    pub fn need(&self, cells: usize, what: &str) -> Result<()> {
        let bytes = cells.saturating_mul(CELL_BYTES);
        if self.used.get().saturating_add(bytes) > self.limit {
            return Err(self.over(bytes, what));
        }
        Ok(())
    }

    /// ノードの評価が終わった。mark 以降に数えた子の結果を捨て、ノードの結果 out を数える。
    pub(crate) fn settle(&self, mark: usize, out: &Cube) -> Result<()> {
        let bytes = out.cells.capacity().saturating_mul(CELL_BYTES);
        self.used.set(mark.saturating_add(bytes));
        if self.used.get() > self.limit {
            return Err(self.over(bytes, "途中結果"));
        }
        Ok(())
    }

    fn over(&self, bytes: usize, what: &str) -> String {
        format!(
            "メモリの予算を超える: {what}に {} が要り、途中結果がすでに {} ある（上限 {}）。式を絞るか、予算（max_bytes）を増やす",
            human(bytes),
            human(self.used.get()),
            human(self.limit)
        )
    }
}

fn human(bytes: usize) -> String {
    const UNITS: [&str; 5] = ["バイト", "KiB", "MiB", "GiB", "TiB"];
    let mut x = bytes as f64;
    let mut u = 0;
    while x >= 1024.0 && u + 1 < UNITS.len() {
        x /= 1024.0;
        u += 1;
    }
    if u == 0 {
        format!("{bytes} バイト")
    } else {
        format!("{x:.1} {}", UNITS[u])
    }
}
