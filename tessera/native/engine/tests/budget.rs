//! Memory budget tests. An operation that can make a large result checks the budget before it makes the result.

use nanashi_engine::{eval, eval_columns, eval_with, held, Budget, Catalog, Config, DimInfo, Kind, Mapping, Node, Op, Restrict, Sel, Src, Store};
use std::sync::Arc;

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
    // Fused, the evaluation holds only the result and the small lists of the chunks (4 KiB is enough for them)
    let fused = catalog(stored + 100_000 * 16 + 4096);
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
fn a_streamed_result_is_budgeted_by_its_columns() {
    let cat = catalog(usize::MAX);
    let src = [column(&cat, &[0], 3000)];
    let held = held(&src);
    let node = Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(Node::Const(2.0, Kind::Num)), vec![]);
    let store = Store::new(&[0], None, Kind::Num, &cat).unwrap();
    // The scan checks 16 B for each driver row before it starts. The columns are smaller, and only they stay counted.
    let b = Budget::new(held + 3000 * 16, held);
    let c = eval_columns(&node, &cat, &src, &b, &store, false).unwrap().expect("streams");
    assert!(c.bytes() < 3000 * 16, "{}", c.bytes());
    let room = (3000 * 16 - c.bytes()) / 16;
    assert!(b.need(room, "x").is_ok());
    assert!(b.need(room + 1, "x").is_err());
    assert_eq!(store.with_columns(c).0.as_cube().cells.len(), 3000);
    // The check before the scan (16 B for each driver row) is the stricter one. If it fails, the scan does not
    // start, and the evaluation of one operation at a time reports the budget error
    let tight = Budget::new(held + 3000 * 9, held);
    assert!(eval_columns(&node, &cat, &src, &tight, &store, false).unwrap().is_none());
    assert!(eval_with(&node, &cat, &src, &Restrict::all(1), &tight).unwrap_err().contains("メモリの予算を超える"));
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
