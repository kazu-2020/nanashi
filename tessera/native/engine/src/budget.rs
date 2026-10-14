//! The memory budget of one operation. The bytes that the operation holds alive (stored data, sources, writes
//! that wait) plus its intermediate results must not be more than the limit.
//!
//! The operations that make all combinations (EXPAND, ISBLANK, IFBLANK) check the bytes before they make them.
//! If the bytes are over the limit, they return Err before the allocation.

use crate::*;
use std::cell::Cell;

/// The bytes of one cell of an intermediate result (the key and the value).
pub const CELL_BYTES: usize = 16;

/// The budget of one evaluation. Each formula that the engine evaluates in parallel has its own Budget (the plan
/// splits the limit).
pub struct Budget {
    limit: usize,
    held: usize,
    used: Cell<usize>,
}

impl Budget {
    pub fn new(limit: usize) -> Budget {
        Budget::with_held(limit, 0)
    }

    pub fn with_held(limit: usize, held: usize) -> Budget {
        Budget { limit, held, used: Cell::new(0) }
    }

    /// The bytes of the intermediate results that the evaluation holds now.
    pub fn used(&self) -> usize {
        self.used.get()
    }

    /// Err if `cells` new cells do not fit next to the held bytes and the intermediates.
    pub fn need(&self, cells: usize, what: &str) -> Result<()> {
        let bytes = cells.saturating_mul(CELL_BYTES);
        if self.held.saturating_add(self.used.get()).saturating_add(bytes) > self.limit {
            return Err(self.over(bytes, what));
        }
        Ok(())
    }

    /// The evaluation of a node ended. Drop the child results counted after mark, and count the node result out.
    pub(crate) fn settle(&self, mark: usize, out: &Cube) -> Result<()> {
        let bytes = out.cells.capacity().saturating_mul(CELL_BYTES);
        self.used.set(mark.saturating_add(bytes));
        if self.held.saturating_add(self.used.get()) > self.limit {
            return Err(self.over(bytes, "途中結果"));
        }
        Ok(())
    }

    fn over(&self, bytes: usize, what: &str) -> String {
        format!(
            "メモリの予算を超える: {what}に {} が要り、格納データが {}、途中結果がすでに {} ある（上限 {}）。式を絞るか、予算（max_bytes）を増やす",
            human(bytes),
            human(self.held),
            human(self.used.get()),
            human(self.limit)
        )
    }
}

/// The bytes of the sources. A source that two entries share (the same Arc) counts once.
pub fn held<'a>(src: impl IntoIterator<Item = &'a Src>) -> usize {
    let mut seen = FxHashSet::default();
    src.into_iter()
        .filter(|s| {
            seen.insert(match s {
                Src::Store(a) => Arc::as_ptr(a) as usize,
                Src::Cube(a) => Arc::as_ptr(a) as usize,
            })
        })
        .map(|s| match s {
            Src::Store(a) => a.memory().bytes(),
            Src::Cube(a) => a.cells.capacity().saturating_mul(CELL_BYTES),
        })
        .sum()
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
