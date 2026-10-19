//! Memory budget tests. An operation that can make a large result checks the budget before it makes the result.

use nanashi_engine::plan::{derive_delta, recalc, Formula, Metric, Plan, Reg, Step};
use nanashi_engine::{eval, Agg, eval_columns, eval_with, held, Budget, Catalog, Config, DimInfo, Kind, Mapping, Node, Op, Restrict, Sel, Src, Store, Tier, Whole, CELL_BYTES};
use std::sync::Arc;

const CACHE: usize = 1 << 16;

/// The file tier of this test process, with a window of 2 blocks.
fn tier() -> Option<&'static Tier> {
    Some(Tier::install(&std::env::temp_dir().join(format!("nanashi-budget-{}", std::process::id())), CACHE, 2).unwrap())
}

fn catalog(max_bytes: usize) -> Catalog {
    catalog_with(Config { max_bytes, ..Config::default() })
}

fn catalog_with(cfg: Config) -> Catalog {
    let dims = (0..3).map(|i| DimInfo { size: 100_000, ordered: false, name: format!("d{i}") }).collect();
    Catalog { dims, maps: Vec::new(), cfg }
}

fn source(cat: &Catalog) -> Vec<Src> {
    let s = Store::new(&[0], None, Kind::Num, cat).unwrap();
    let s = s.with_rows(&[&[1, 2, 3]], &[1.0, 2.0, 3.0]).unwrap();
    vec![Src::Store(Arc::new(s))]
}

#[test]
fn expand_beyond_the_budget_fails_before_allocating() {
    // 3 セル × 10^5 × 10^5 = 3 × 10^10 セル（480 GB）。確かめずに作ればメモリが尽きる
    let cat = catalog(1 << 30);
    let node = Node::Expand(Box::new(Node::Ref(0)), vec![1, 2]);
    let err = eval(&node, &cat, &source(&cat), &Restrict::all(3)).unwrap_err();
    assert!(err.contains("メモリの予算を超える") && err.contains("EXPAND"), "{err}");
}

#[test]
fn ifblank_beyond_the_budget_fails_before_allocating() {
    let cat = catalog(1 << 30);
    let node = Node::IfBlank(Box::new(Node::Expand(Box::new(Node::Ref(0)), vec![])), 0.0, false, vec![]);
    // 10^5 の軸 1 本なら収まる
    assert_eq!(eval(&node, &cat, &source(&cat), &Restrict::all(3)).unwrap().cells.len(), 100_000);
    let stored = held(&source(&cat));
    // 3 values (24 B) and the packed keys (40 B: one 8 B word, 16 spare bytes, one 16 B block header)
    assert_eq!(stored, 64);
    // 演算ごとに評価すると、全組み合わせのキーと結果の 2 つ分が要るので、1 つ分の予算では足りない
    let small = catalog_with(Config { max_bytes: stored + 100_000 * 16, fuse: false, ..Config::default() });
    let err = eval(&node, &small, &source(&small), &Restrict::all(3)).unwrap_err();
    assert!(err.contains("IFBLANK"), "{err}");
    // Fused, the evaluation holds the result, the list of the 10^5 members (4 B each) and the small lists of the chunks
    let fused = catalog(stored + 100_000 * 16 + 4096);
    assert!(eval(&node, &fused, &source(&fused), &Restrict::all(3)).unwrap_err().contains("IFBLANK"));
    let fused = catalog(stored + 100_000 * (16 + 4) + 4096);
    assert_eq!(eval(&node, &fused, &source(&fused), &Restrict::all(3)).unwrap().cells.len(), 100_000);
    // 結果も収まらなければ、確保する前に失敗する
    let tiny = catalog(stored + 100_000 * 8);
    assert!(eval(&node, &tiny, &source(&tiny), &Restrict::all(3)).unwrap_err().contains("メモリの予算を超える"));
}

/// Makes a Store on `dims` with `n` rows. Row `i` has member `i` of the last dimension and member 0 of each other dimension.
fn column(cat: &Catalog, dims: &[usize], n: u32) -> Src {
    let last: Vec<u32> = (0..n).collect();
    let zeros = vec![0u32; n as usize];
    let cols: Vec<&[u32]> = (0..dims.len()).map(|i| if i + 1 == dims.len() { &last[..] } else { &zeros[..] }).collect();
    let s = Store::new(dims, None, Kind::Num, cat).unwrap().with_rows(&cols, &vec![1.0; n as usize]).unwrap();
    Src::Store(Arc::new(s))
}

#[test]
fn join_beyond_the_budget_fails_before_allocating() {
    let cat = catalog(usize::MAX);
    let src = [column(&cat, &[0], 3000), column(&cat, &[1], 3000)];
    let cat = catalog(held(&src) + (1 << 20));
    let node = Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![]);
    let err = eval(&node, &cat, &src, &Restrict::all(3)).unwrap_err();
    assert!(err.contains("メモリの予算を超える") && err.contains("結合"), "{err}");
}

/// Members 0 to 2999 of d0 map to member 0 of d1. The source has 3000 cells on (d1 = 0, d2).
fn by_lookup() -> (Catalog, [Src; 1], Node) {
    let mut cat = catalog(usize::MAX);
    let mut fwd = vec![-1i64; 100_000];
    fwd[..3000].fill(0);
    let mut inv = vec![Vec::new(); 100_000];
    inv[0] = (0..3000).collect();
    cat.maps = vec![Arc::new(Mapping { fwd, inv })];
    let src = [column(&cat, &[1, 2], 3000)];
    cat.cfg.max_bytes = held(&src) + (1 << 20);
    (cat, src, Node::ByLookup { child: Box::new(Node::Ref(0)), src: 0, dst: 1, map: 0 })
}

#[test]
fn by_lookup_beyond_the_budget_fails_before_allocating() {
    let (cat, src, node) = by_lookup();
    let err = eval(&node, &cat, &src, &Restrict::all(3)).unwrap_err();
    assert!(err.contains("メモリの予算を超える") && err.contains("引き下ろし"), "{err}");
}

#[test]
fn by_lookup_on_one_member_counts_only_that_member() {
    let (cat, src, node) = by_lookup();
    let one = Restrict::all(3).with(0, Sel::new(vec![5], 100_000));
    let c = eval(&node, &cat, &src, &one).unwrap();
    assert_eq!(c.cells.len(), 3000);
    assert!(c.cells.iter().all(|&(_, v)| v == 1.0));
}

#[test]
fn a_by_metric_join_beyond_the_budget_fails_before_copying() {
    // x[BY SUM: d0.V]: x has 3000 cells on (d0 = 0, d1), and V maps member 0 of d0 to member 7 of d2.
    // The 10^10 groups on (d1, d2) are too many for the table, so the engine joins x with V
    let cat = catalog(usize::MAX);
    let v = Store::new(&[0], None, Kind::Num, &cat).unwrap().with_rows(&[&[0]], &[7.0]).unwrap();
    let src = [column(&cat, &[0, 1], 3000), Src::Store(Arc::new(v))];
    let edges = Node::AsAxis { child: Box::new(Node::Ref(1)), dim: 2 };
    let node = Node::Remove { child: Box::new(Node::On(Box::new(Node::Ref(0)), Box::new(edges))), dim: 0, agg: Agg::Sum };
    let c = eval(&node, &cat, &src, &Restrict::all(3)).unwrap();
    assert_eq!(c.cells.len(), 3000);
    // The copy of x (3000 cells) does not fit in 1000 cells
    let small = catalog(held(&src) + 1000 * CELL_BYTES);
    let err = eval(&node, &small, &src, &Restrict::all(3)).unwrap_err();
    assert!(err.contains("メモリの予算を超える") && err.contains("集計"), "{err}");
}

#[test]
fn a_by_metric_join_reads_only_the_mappings_of_a_small_source() {
    // x has 10 cells on d0 = 0. V maps 50,000 members of d0, but the join reads only V[0]
    let cat = catalog(usize::MAX);
    let n = 50_000u32;
    let v = Store::new(&[0], None, Kind::Num, &cat).unwrap().with_rows(&[&(0..n).collect::<Vec<_>>()], &vec![7.0; n as usize]).unwrap();
    let src = [column(&cat, &[0, 1], 10), Src::Store(Arc::new(v))];
    let edges = Node::AsAxis { child: Box::new(Node::Ref(1)), dim: 2 };
    let node = Node::Remove { child: Box::new(Node::On(Box::new(Node::Ref(0)), Box::new(edges))), dim: 0, agg: Agg::Sum };
    // All of V and its table (100,000 cells) do not fit in 1000 cells
    let small = catalog(held(&src) + 1000 * CELL_BYTES);
    let c = eval(&node, &small, &src, &Restrict::all(3)).unwrap();
    assert_eq!(c.cells.len(), 10);
    assert!(c.cells.iter().all(|&(_, v)| v == 1.0));
}

#[test]
fn a_streamed_result_is_budgeted_by_its_columns() {
    for tier in [None, tier()] {
        streamed_result_is_budgeted_by_its_columns(tier);
    }
}

fn streamed_result_is_budgeted_by_its_columns(tier: Option<&'static Tier>) {
    let cat = catalog_with(Config { seg_rows: 1000, tier, ..Config::default() });
    let rows = 3000usize;
    let src = [column(&cat, &[0], rows as u32)];
    let held = held(&src);
    if tier.is_some() {
        assert_eq!(held, 3 * 16, "the fences of 3 segments");
    }
    let node = Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(Node::Const(2.0, Kind::Num)), vec![]);
    let store = Store::new(&[0], None, Kind::Num, &cat).unwrap();
    // The columns need room for the part buffers: here one part of all rows, 16 B for each cell. The list of
    // the ranges of the scan needs less than 1 KiB. Without that room, an array of cells fits
    let b = Budget::new(held + store.sink_bytes(rows, false), held);
    assert!(matches!(eval_columns(&node, &cat, &src, &b, &store, false).unwrap(), Some(Whole::Cube(c)) if c.cells.len() == rows));
    let b = Budget::new(held + rows * CELL_BYTES, held);
    assert!(eval_columns(&node, &cat, &src, &b, &store, false).is_err_and(|e| e.contains("メモリの予算を超える")));
    let limit = store.sink_bytes(rows, false) + rows * CELL_BYTES + 1024;
    let b = Budget::new(held + limit, held);
    let Some(Whole::Columns(c)) = eval_columns(&node, &cat, &src, &b, &store, false).unwrap() else { panic!("streams") };
    // Only the columns stay counted, and they are smaller than the cells
    assert!(c.bytes() < rows * CELL_BYTES, "{}", c.bytes());
    if tier.is_some() {
        assert_eq!(c.bytes(), 3 * 16, "a file base keeps only its fences in RAM");
    }
    let room = (limit - c.bytes()) / CELL_BYTES;
    assert!(b.need(room, "x").is_ok());
    assert!(b.need(room + 1, "x").is_err());
    assert_eq!(store.with_columns(c).0.as_cube().cells.len(), rows);
    // The check before the scan (CELL_BYTES for each driver row) is the stricter one. If it fails, the scan does
    // not start, and the evaluation of one operation at a time reports the budget error
    let tight = Budget::new(held + rows * 9, held);
    assert!(eval_columns(&node, &cat, &src, &tight, &store, false).unwrap().is_none());
    assert!(eval_with(&node, &cat, &src, &Restrict::all(1), &tight).unwrap_err().contains("メモリの予算を超える"));
}

#[test]
fn a_streamed_file_result_needs_no_room_for_all_its_cells() {
    // A file base keeps only its fences in RAM, and the scan holds at most 2 batches of parts of about 100 cells
    let cat = catalog_with(Config { seg_rows: 1000, chunk: 100, tier: tier(), ..Config::default() });
    let rows = 300_000usize;
    let (d0, d1): (Vec<u32>, Vec<u32>) = (0..rows as u32).map(|i| (i / 1000, i % 1000)).unzip();
    let s = Store::new(&[0, 1], None, Kind::Num, &cat).unwrap().with_rows(&[&d0, &d1], &vec![1.0; rows]).unwrap();
    let src = [Src::Store(Arc::new(s))];
    let held = held(&src);
    let node = Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(Node::Const(2.0, Kind::Num)), vec![]);
    let store = Store::new(&[0, 1], None, Kind::Num, &cat).unwrap();
    let b = Budget::new(held + rows * CELL_BYTES / 2, held);
    let Some(Whole::Columns(c)) = eval_columns(&node, &cat, &src, &b, &store, false).unwrap() else { panic!("streams") };
    assert_eq!(store.with_columns(c).0.as_cube().cells.len(), rows);
}

#[test]
fn a_sorted_scan_whose_ranges_do_not_fit_is_not_fused() {
    // 100 drivers and a range at each key of the largest: 10,000 ranges of 2 x 100 row positions (16 MB)
    let cat = catalog(usize::MAX);
    let src: Vec<Src> = (0..100).map(|i| column(&cat, &[0], if i == 0 { 10_000 } else { 1 })).collect();
    let cat = catalog_with(Config { max_bytes: held(&src) + (1 << 20), chunk: 1, ..Config::default() });
    let node = (1..100).fold(Node::Ref(0), |acc, i| Node::Bin(Op::Add, Box::new(acc), Box::new(Node::Ref(i)), vec![]));
    // One operation at a time, each result has at most 10,000 cells (160 KB)
    let c = eval(&node, &cat, &src, &Restrict::all(3)).unwrap();
    assert_eq!(c.cells.len(), 10_000);
    assert_eq!(c.cells[0].1, 100.0);
    assert!(c.cells[1..].iter().all(|&(_, v)| v == 1.0));
}

/// A full recalculation of `Month >= HireMonth AND ISBLANK(LeaveMonth)[EXPAND: Month]` (Employed in
/// examples/fpa.py) into a store on (Employee, Month). No source has both dimensions, and the store does not
/// keep the dimensions in the order of their ids (Month is d0). `budget` gets the result rows and the RAM bytes
/// of the inputs. Gives the cells of the result.
fn employed(tier: Option<&'static Tier>, budget: impl FnOnce(usize, usize) -> usize) -> Result<Vec<(u64, f64)>, String> {
    let (months, employees) = (400u32, 1000u32);
    let dims = vec![DimInfo { size: months, ordered: true, name: "Month".into() }, DimInfo { size: employees, ordered: false, name: "Employee".into() }];
    // Small parts, one at a time, so the part buffers are small against the result
    let cfg = Config { tier, chunk: 1000, par_min: usize::MAX, ..Config::default() };
    let start = Catalog { dims: dims.clone(), maps: Vec::new(), cfg };
    let all: Vec<u32> = (0..employees).collect();
    let hire: Vec<f64> = all.iter().map(|e| (e % months) as f64).collect();
    let hire = Store::new(&[1], None, Kind::Num, &start)?.with_rows(&[&all], &hire)?;
    let left: Vec<u32> = (0..employees).step_by(3).collect();
    let leave = Store::new(&[1], None, Kind::Num, &start)?.with_rows(&[&left], &vec![1.0; left.len()])?;
    let out = Store::new(&[1, 0], None, Kind::Bool, &start)?;
    let mut stores = vec![Arc::new(hire), Arc::new(leave), Arc::new(out)];
    let src: Vec<Src> = stores[..2].iter().map(|s| Src::Store(s.clone())).collect();
    let rows = (months * employees) as usize;
    let cat = Catalog { dims, maps: Vec::new(), cfg: Config { max_bytes: budget(rows, held(&src) + cfg.cache_bound()), ..cfg } };
    let ge = Node::Bin(Op::Ge, Box::new(Node::DimRef(0)), Box::new(Node::Ref(0)), vec![]);
    let blank = Node::Expand(Box::new(Node::IsBlank(Box::new(Node::Ref(1)), vec![])), vec![0]);
    let node = Node::Bin(Op::And, Box::new(ge), Box::new(blank), vec![]);
    let input = || Metric { formula: None, delta: None, source: false };
    let formula = Metric { formula: Some(Formula { node: Arc::new(node), refs: vec![0, 1] }), delta: None, source: false };
    let plan = Plan { metrics: vec![input(), input(), formula], levels: vec![vec![Step::One(2)]] };
    recalc(&cat, &plan, &mut stores, &mut vec![None; 3], &mut vec![None; 3], vec![], &[], vec![], vec![(2, Reg::new(vec![]))], true)?;
    Ok(stores[2].as_cube().cells)
}

#[test]
fn a_fused_result_in_the_order_of_its_store_is_budgeted_by_its_columns() {
    // Every (Employee, Month) gets a cell: 1 from the hire month of an employee who did not leave, else 0
    let ones: usize = (0..1000).filter(|e| e % 3 != 0).map(|e| 400 - e % 400).sum();
    for tier in [None, tier()] {
        let all = employed(tier, |_, _| usize::MAX).unwrap();
        assert_eq!(all.len(), 400_000);
        assert_eq!(all.iter().filter(|c| c.1 == 1.0).count(), ones);
        // The result array and its write (2 cells for each row) do not fit. The columns of the store (at most
        // 1 cell for each row) and the part buffers do. The scan must give the dimension order of the store
        let tight = employed(tier, |rows, held| held + rows * CELL_BYTES * 3 / 2).unwrap();
        assert_eq!(tight, all);
    }
}

#[test]
fn intermediate_results_are_counted_while_they_live() {
    let cat = catalog(usize::MAX);
    let b = Budget::new(usize::MAX, 0);
    let node = Node::Ref(0);
    let out = eval_with(&node, &cat, &source(&cat), &Restrict::all(3), &b).unwrap();
    let exact = Budget::new(out.cells.capacity() * 16, 0);
    eval_with(&node, &cat, &source(&cat), &Restrict::all(3), &exact).unwrap();
    assert!(exact.need(1, "x").is_err());
    // 1 セル分しか予算がなければ、3 セルの読み出しは失敗する
    let tight = Budget::new(16, 0);
    assert!(eval_with(&node, &cat, &source(&cat), &Restrict::all(3), &tight).is_err());
}

#[test]
fn stored_data_counts_against_the_budget() {
    let cat = catalog(usize::MAX);
    let src = source(&cat);
    let node = Node::Ref(0);
    let stored = held(&src);
    let tight = eval(&node, &catalog(stored + 3 * 16 - 1), &src, &Restrict::all(3)).unwrap_err();
    assert!(tight.contains("格納データが 64 バイト"), "{tight}");
    assert!(eval(&node, &catalog(stored + 3 * 16), &src, &Restrict::all(3)).is_ok());
}

#[test]
fn held_counts_a_shared_source_once() {
    let cat = catalog(usize::MAX);
    let src = source(&cat);
    let twice = [src[0].clone(), src[0].clone()];
    assert_eq!(held(&twice), held(&src[..1]));
    let other = source(&cat);
    assert_eq!(held([&src[0], &other[0]]), 2 * held(&src[..1]));
}

#[test]
fn need_refuses_a_byte_count_that_overflows_with_no_limit() {
    let b = Budget::new(usize::MAX, 0);
    assert!(b.need(usize::MAX / 16, "x").is_ok());
    assert!(b.need(usize::MAX / 16 + 1, "x").unwrap_err().contains("メモリの予算を超える"));
    assert!(b.need(usize::MAX, "x").is_err());
}

#[test]
fn join_against_few_cells_fits_a_tight_budget() {
    let cat = catalog(usize::MAX);
    for rows in [0, 1] {
        let src = [column(&cat, &[0, 1], 100_000), column(&cat, &[1, 2], rows)];
        // Fusion off, so that intersect makes the join. Reading a needs |a| cells. The bound |a| for the pairs
        // does not fit a second time, but the exact count does.
        let tight = catalog_with(Config { max_bytes: held(&src) + 100_000 * 16 * 3 / 2, fuse: false, ..Config::default() });
        let node = Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![]);
        assert_eq!(eval(&node, &tight, &src, &Restrict::all(3)).unwrap().cells.len(), rows as usize);
    }
}

#[test]
fn held_counts_only_the_ram_of_the_sources() {
    let file = catalog_with(Config { tier: tier(), ..Config::default() });
    let ram = catalog(usize::MAX);
    let (a, r) = (source(&file), source(&ram));
    assert_eq!(held([&a[0], &r[0]]), 16 + 64, "the fences of 1 block, and a RAM store");
    assert_eq!(file.cfg.cache_bound(), CACHE);
    assert_eq!(ram.cfg.cache_bound(), 0);
}

/// Adds 1 cell to a file source of 10000 rows and updates an incremental COUNT of it. `budget` gets the RAM bytes
/// of the stores.
fn incremental_count(budget: impl FnOnce(usize) -> usize) -> Result<Option<f64>, String> {
    let rows = 10_000u32;
    let dims = vec![DimInfo { size: 2, ordered: false, name: "d0".into() }, DimInfo { size: rows + 1, ordered: false, name: "d1".into() }];
    let start = Catalog { dims: dims.clone(), maps: Vec::new(), cfg: Config { tier: tier(), ..Config::default() } };
    let Src::Store(old) = column(&start, &[0, 1], rows) else { unreachable!() };
    let mut new = (*old).clone();
    new.write(&[0, rows], Some(1.0));
    let count = Store::new(&[0], None, Kind::Num, &start).unwrap().with_rows(&[&[0]], &[rows as f64]).unwrap();
    let mut stores = vec![Arc::new(new), Arc::new(count)];
    let ram = stores.iter().chain([&old]).map(|s| s.memory().bytes()).sum();
    let cat = Catalog { dims, maps: Vec::new(), cfg: Config { max_bytes: budget(ram), ..start.cfg } };
    let formula = Formula { node: Arc::new(Node::Remove { child: Box::new(Node::Ref(0)), dim: 1, agg: Agg::Count }), refs: vec![0] };
    let delta = Some(derive_delta(&formula).expect("an incremental COUNT"));
    let metrics = vec![Metric { formula: None, delta: None, source: true }, Metric { formula: Some(formula), delta, source: false }];
    let plan = Plan { metrics, levels: vec![vec![Step::One(1)]] };
    let changed = vec![(0, Reg::new(vec![(0, vec![0]), (1, vec![rows])]))];
    recalc(&cat, &plan, &mut stores, &mut vec![None; 2], &mut vec![None; 2], changed, &[], vec![(0, Src::Store(old))], vec![], false)?;
    Ok(stores[1].get(&[0]))
}

#[test]
fn an_incremental_aggregation_counts_the_cache_bound_once() {
    let headroom = CACHE / 2;
    assert_eq!(incremental_count(|ram| ram + CACHE + headroom), Ok(Some(10_001.0)));
    assert!(incremental_count(|ram| ram + headroom).unwrap_err().contains("メモリの予算を超える"), "the cache bound counts");
}

/// Writes 1 cell of a copy of a column of `rows` rows. The copy compacts at each write (a delta limit of 1 row).
fn write_one_cell(tier: Option<&'static Tier>, rows: u32, headroom: usize) -> Result<usize, String> {
    let dims = vec![DimInfo { size: rows, ordered: false, name: "d0".into() }];
    let cfg = Config { compact_min: 1, delta_max: 1, tier, ..Config::default() };
    let start = Catalog { dims: dims.clone(), maps: Vec::new(), cfg };
    let Src::Store(input) = column(&start, &[0], rows) else { unreachable!() };
    let mut stores = vec![input.clone(), input];
    let src: Vec<Src> = stores.iter().map(|s| Src::Store(s.clone())).collect();
    let cat = Catalog { dims, maps: Vec::new(), cfg: Config { max_bytes: held(&src) + cfg.cache_bound() + headroom, ..cfg } };
    let input = Metric { formula: None, delta: None, source: false };
    let copy = Metric { formula: Some(Formula { node: Arc::new(Node::Ref(0)), refs: vec![0] }), delta: None, source: false };
    let plan = Plan { metrics: vec![input, copy], levels: vec![vec![Step::One(1)]] };
    let changed = vec![(0, Reg::new(vec![(0, vec![5])]))];
    recalc(&cat, &plan, &mut stores, &mut vec![None; 2], &mut vec![None; 2], changed, &[], vec![], vec![], false)?;
    Ok(stores[1].rows_hint())
}

#[test]
fn a_compaction_into_files_counts_only_the_ram_of_the_new_base() {
    // In RAM the new base needs 16 B for each row (3.2 MB). In file mode it needs the fences, the writer buffer
    // (1 MiB), the block in build and one read window
    let rows = 200_000;
    let err = write_one_cell(None, rows, 2 << 20).unwrap_err();
    assert!(err.contains("メモリの予算を超える") && err.contains("結果の書き込み"), "{err}");
    assert_eq!(write_one_cell(tier(), rows, 2 << 20), Ok(rows as usize));
    assert!(write_one_cell(tier(), rows, 1 << 20).unwrap_err().contains("結果の書き込み"), "the writer buffer counts");
}
