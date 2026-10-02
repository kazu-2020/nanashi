//! メモリの予算。全組み合わせを作る演算は、作る前に予算を確かめる。

use nanashi_engine::{eval, eval_with, Budget, Catalog, Config, DimInfo, Kind, Node, Restrict, Src, Store};
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
    // 演算ごとに評価すると、全組み合わせのキーと結果の 2 つ分が要るので、1 つ分の予算では足りない
    let small = catalog_with(Config { max_bytes: 100_000 * 16, fuse: false, ..Config::default() });
    let err = eval(&node, &small, &source(&small), &Restrict::all(3)).unwrap_err();
    assert!(err.contains("IFBLANK"), "{err}");
    // 融合すれば結果の分しか持たないので、同じ予算で計算できる
    let fused = catalog(100_000 * 16);
    assert_eq!(eval(&node, &fused, &source(&fused), &Restrict::all(3)).unwrap().cells.len(), 100_000);
    // 結果も収まらなければ、確保する前に失敗する
    let tiny = catalog(100_000 * 8);
    assert!(eval(&node, &tiny, &source(&tiny), &Restrict::all(3)).unwrap_err().contains("メモリの予算を超える"));
}

#[test]
fn intermediate_results_are_counted_while_they_live() {
    let cat = catalog(usize::MAX);
    let b = Budget::new(usize::MAX);
    let node = Node::Ref(0);
    let out = eval_with(&node, &cat, &source(&cat), &Restrict::all(3), &b).unwrap();
    assert_eq!(b.used(), out.cells.capacity() * 16);
    // 1 セル分しか予算がなければ、3 セルの読み出しは失敗する
    let tight = Budget::new(16);
    assert!(eval_with(&node, &cat, &source(&cat), &Restrict::all(3), &tight).is_err());
}
