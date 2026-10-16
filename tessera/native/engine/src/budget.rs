//! The memory budget of one operation. The held bytes (the sources and the writes that wait) plus the intermediate
//! results must not be more than the limit.

use crate::*;
use std::cell::Cell;

/// The bytes of one cell of an intermediate result (the key and the value).
pub const CELL_BYTES: usize = 16;

/// The budget of one operation. Each formula that the engine evaluates in parallel gets its own Budget.
pub struct Budget {
    limit: usize,
    held: usize,
    used: Cell<usize>,
}

pub(crate) struct Mark(usize);

impl Budget {
    pub fn new(limit: usize, held: usize) -> Budget {
        Budget { limit, held, used: Cell::new(0) }
    }

    pub(crate) fn mark(&self) -> Mark {
        Mark(self.used.get())
    }

    /// Err if `cells` new cells do not fit with the held bytes and the intermediate results.
    pub fn need(&self, cells: usize, what: &str) -> Result<()> {
        // A byte count that overflows usize never fits, also when the limit is usize::MAX (no limit).
        let total = cells.checked_mul(CELL_BYTES).and_then(|b| self.held.checked_add(self.used.get())?.checked_add(b));
        match total {
            Some(t) if t <= self.limit => Ok(()),
            _ => Err(self.over(cells.saturating_mul(CELL_BYTES), what)),
        }
    }

    /// The number of new cells that `need` permits now. Parallel code uses it because a Budget is not Sync.
    pub(crate) fn room(&self) -> usize {
        self.limit.saturating_sub(self.held.saturating_add(self.used.get())) / CELL_BYTES
    }

    /// The evaluation of a node ended. Set the used bytes back to start (this drops the child results counted
    /// after it), and count the node result out.
    pub(crate) fn settle(&self, start: Mark, out: &Cube) -> Result<()> {
        let bytes = out.cells.capacity().saturating_mul(CELL_BYTES);
        self.used.set(start.0.saturating_add(bytes));
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
        .filter_map(|s| {
            let (ptr, bytes) = match s {
                Src::Store(a) => (Arc::as_ptr(a) as usize, a.memory().bytes()),
                Src::Cube(a) => (Arc::as_ptr(a) as usize, a.cells.capacity().saturating_mul(CELL_BYTES)),
            };
            seen.insert(ptr).then_some(bytes)
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
