//! メモリの予算。全組み合わせを作る演算は、作る前に予算を確かめる。

use nanashi_engine::{eval, eval_with, held, Budget, Catalog, Config, DimInfo, Kind, Mapping, Node, Op, Restrict, Src, Store};
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
    // 融合すれば結果の分しか持たないので、同じ予算で計算できる
    let fused = catalog(stored + 100_000 * 16);
    assert_eq!(eval(&node, &fused, &source(&fused), &Restrict::all(3)).unwrap().cells.len(), 100_000);
    // 結果も収まらなければ、確保する前に失敗する
    let tiny = catalog(stored + 100_000 * 8);
    assert!(eval(&node, &tiny, &source(&tiny), &Restrict::all(3)).unwrap_err().contains("メモリの予算を超える"));
}

/// A Store on `dims` with `n` rows: the row i is at member i of the last dimension and at member 0 of the others.
fn column(cat: &Catalog, dims: &[usize], n: u32) -> Src {
    let last: Vec<u32> = (0..n).collect();
    let zeros = vec![0u32; n as usize];
    let cols: Vec<&[u32]> = dims.iter().enumerate().map(|(i, _)| if i + 1 == dims.len() { &last[..] } else { &zeros[..] }).collect();
    let s = Store::new(dims, None, Kind::Num, cat).unwrap().with_rows(&cols, &vec![1.0; n as usize]).unwrap();
    Src::Store(Arc::new(s))
}

#[test]
fn join_beyond_the_budget_fails_before_allocating() {
    // 3000 cells on d0 x 3000 cells on d1 = 9 x 10^6 cells (144 MB). The error names the join, not the result
    let cat = catalog(usize::MAX);
    let src = [column(&cat, &[0], 3000), column(&cat, &[1], 3000)];
    let cat = catalog(held(&src) + (1 << 20));
    let node = Node::Bin(Op::Mul, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![]);
    let err = eval(&node, &cat, &src, &Restrict::all(3)).unwrap_err();
    assert!(err.contains("メモリの予算を超える") && err.contains("結合"), "{err}");
}

#[test]
fn by_lookup_beyond_the_budget_fails_before_allocating() {
    // The 3000 cells of (d1 = 0, d2) go down to the 3000 members of d0 that map to d1 = 0
    let mut cat = catalog(usize::MAX);
    let mut fwd = vec![-1i64; 100_000];
    fwd[..3000].fill(0);
    let mut inv = vec![Vec::new(); 100_000];
    inv[0] = (0..3000).collect();
    cat.maps = vec![Arc::new(Mapping { fwd, inv })];
    let src = [column(&cat, &[1, 2], 3000)];
    cat.cfg.max_bytes = held(&src) + (1 << 20);
    let node = Node::ByLookup { child: Box::new(Node::Ref(0)), src: 0, dst: 1, map: 0 };
    let err = eval(&node, &cat, &src, &Restrict::all(3)).unwrap_err();
    assert!(err.contains("メモリの予算を超える") && err.contains("引き下ろし"), "{err}");
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
