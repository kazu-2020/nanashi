//! 格納データどうしの INNER JOIN。差分がなく詰め方も同じなら本体を写さずに突き合わせるが、
//! 結果は差分があるときや詰め方が違うとき（Cube に読んでから突き合わせる）と同じ。

use nanashi_engine::{eval, Catalog, Config, DimInfo, Kind, Node, Op, Restrict, Sel, Src, Store};
use std::collections::BTreeMap;
use std::sync::Arc;

const SIZES: [u32; 2] = [30, 20];

type Scalar = fn(f64, f64) -> Option<f64>;

fn catalog() -> Catalog {
    let dims = SIZES.iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
    Catalog { dims, maps: Vec::new(), cfg: Config::default() }
}

/// (d0, d1) の格子の一部に値を置いた Store。step ごとに 1 つ置き、値は位置から決める。
fn cells(step: u32, scale: f64) -> BTreeMap<(u32, u32), f64> {
    (0..SIZES[0] * SIZES[1]).step_by(step as usize).map(|i| ((i / SIZES[1], i % SIZES[1]), (i % 7) as f64 * scale)).collect()
}

fn store(order: &[usize], cells: &BTreeMap<(u32, u32), f64>, delta: bool, cat: &Catalog) -> Src {
    let s = Store::new(order, None, Kind::Num, cat).unwrap();
    let pick = |(a, b): (u32, u32)| if order[0] == 0 { [a, b] } else { [b, a] };
    let keys: Vec<[u32; 2]> = cells.keys().map(|&k| pick(k)).collect();
    let cols: Vec<Vec<u32>> = (0..2).map(|d| keys.iter().map(|k| k[d]).collect()).collect();
    let values: Vec<f64> = cells.values().copied().collect();
    let mut s = s.with_rows(&[&cols[0], &cols[1]], &values).unwrap();
    if delta {
        // 差分の木に入れて、同じ中身に戻す
        let (&k, &v) = cells.iter().next().unwrap();
        s.write(&pick(k), Some(v + 1.0));
        s.write(&pick(k), Some(v));
    }
    Src::Store(Arc::new(s))
}

fn result(cat: &Catalog, op: Op, a: Src, b: Src, r: &Restrict) -> BTreeMap<(u32, u32), f64> {
    let node = Node::Bin(op, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![]);
    let c = eval(&node, cat, &[a, b], r).unwrap();
    let p0 = c.pack.dims.iter().position(|&d| d == 0).unwrap();
    c.cells.iter().map(|&(k, v)| {
        let (x, y) = (c.pack.get(k, p0), c.pack.get(k, 1 - p0));
        ((x, y), v)
    }).collect()
}

#[test]
fn store_joins_match_the_definition() {
    let cat = catalog();
    let (a, b) = (cells(2, 1.0), cells(3, 2.0));
    let ops: [(Op, Scalar); 3] = [
        (Op::Mul, |x, y| Some(x * y)),
        (Op::Div, |x, y| (y != 0.0).then(|| x / y)),
        (Op::Gt, |x, y| Some(if x > y { 1.0 } else { 0.0 })),
    ];
    let region = Restrict::all(2).with(0, Sel::new(vec![1, 4, 9], SIZES[0]));
    for (op, f) in ops {
        for r in [Restrict::all(2), region.clone()] {
            let want: BTreeMap<(u32, u32), f64> = a
                .iter()
                .filter(|(k, _)| r.get(0).is_none_or(|s| s.has(k.0)))
                .filter_map(|(k, &x)| b.get(k).and_then(|&y| f(x, y)).map(|v| (*k, v)))
                .collect();
            for (order_b, delta) in [([0, 1], false), ([0, 1], true), ([1, 0], false)] {
                let got = result(&cat, op, store(&[0, 1], &a, false, &cat), store(&order_b, &b, delta, &cat), &r);
                assert_eq!(got, want, "{op:?} 範囲 {:?} 右の詰め方 {order_b:?} 差分 {delta}", r.get(0).map(|s| &s.members));
            }
        }
    }
}
