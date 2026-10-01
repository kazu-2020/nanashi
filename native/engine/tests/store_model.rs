//! Store の性質テスト。乱数の操作列を Store と BTreeMap（正解）の両方に適用し、毎回同じ中身かを確かめる。
//! 調整値を小さくして、差分のまとめ直し、索引、並列の経路も通す。複製（版）は書き換えても元に影響しない。

use nanashi_engine::{Catalog, Config, Cube, DimInfo, Kind, Packing, Restrict, Sel, Store};
use std::collections::BTreeMap;

/// 再現できる乱数（xorshift64*）。
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }

    fn chance(&mut self, p: f64) -> bool {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64 <= p
    }
}

type Model = BTreeMap<Vec<u32>, f64>;

const SIZES: [u32; 3] = [7, 5, 11];

fn catalog(cfg: Config) -> Catalog {
    let dims = SIZES.iter().enumerate().map(|(i, &size)| DimInfo { size, ordered: false, name: format!("d{i}") }).collect();
    Catalog { dims, maps: Vec::new(), cfg }
}

fn key(rng: &mut Rng) -> Vec<u32> {
    SIZES.iter().map(|&s| rng.below(s as u64) as u32).collect()
}

fn value(rng: &mut Rng) -> Option<f64> {
    (!rng.chance(0.2)).then(|| rng.below(100) as f64)
}

fn region(rng: &mut Rng, cat: &Catalog) -> Restrict {
    let mut r = Restrict::all(SIZES.len());
    for (d, &size) in SIZES.iter().enumerate() {
        if rng.chance(0.4) {
            let ms: Vec<u32> = (0..size).filter(|_| rng.chance(0.4)).collect();
            r = r.with(d, Sel::new(ms, cat.dims[d].size));
        }
    }
    r
}

fn inside(r: &Restrict, k: &[u32]) -> bool {
    k.iter().enumerate().all(|(d, &m)| r.get(d).is_none_or(|s| s.has(m)))
}

fn cells(s: &Store) -> Model {
    let (cols, values) = s.rows();
    (0..values.len()).map(|i| (cols.iter().map(|c| c[i]).collect(), values[i])).collect()
}

fn check(s: &Store, m: &Model, rng: &mut Rng, cat: &Catalog, step: usize) {
    assert_eq!(cells(s), *m, "中身が違う（{step} 回目）");
    assert_eq!(s.len(), m.len(), "len が違う（{step} 回目）");
    for _ in 0..5 {
        let k = key(rng);
        assert_eq!(s.get(&k), m.get(&k).copied(), "get {k:?}（{step} 回目）");
    }
    let r = region(rng, cat);
    let want: Model = m.iter().filter(|(k, _)| inside(&r, k)).map(|(k, v)| (k.clone(), *v)).collect();
    assert_eq!(cells(&s.slice(&r)), want, "slice（{step} 回目）");
    assert_eq!(s.read(&r).cells.len(), want.len(), "read（{step} 回目）");
    let (offset, limit) = (rng.below(4) as usize, rng.below(6) as usize);
    let (cols, values, total) = s.rows_in(&r, &[], offset, Some(limit));
    assert_eq!(total, want.len());
    let page: Vec<(Vec<u32>, f64)> = want.iter().skip(offset).take(limit).map(|(k, v)| (k.clone(), *v)).collect();
    let got: Vec<(Vec<u32>, f64)> = (0..values.len()).map(|i| (cols.iter().map(|c| c[i]).collect(), values[i])).collect();
    assert_eq!(got, page, "rows_in は宣言した軸の順に並ぶ（{step} 回目）");
}

/// r の範囲を、乱数のセルからなる Cube で置き換える（Store::replace と同じことを正解にもする）。
fn replace(rng: &mut Rng, cat: &Catalog, s: &mut Store, m: &mut Model, diff: bool) {
    let r = region(rng, cat);
    let order = [2, 0, 1]; // Cube の軸の順は Store と違ってよい
    let pack = Packing::new(&order, cat).unwrap();
    let mut new = BTreeMap::new();
    for _ in 0..rng.below(30) {
        let k = key(rng);
        if inside(&r, &k) {
            new.insert(k, rng.below(100) as f64);
        }
    }
    let cube = Cube {
        pack: pack.clone(),
        kind: Kind::Num,
        cells: new.iter().map(|(k, &v)| (order.iter().enumerate().fold(0, |acc, (i, &d)| acc | pack.put(i, k[d])), v)).collect(),
    };
    let before = m.clone();
    m.retain(|k, _| !inside(&r, k));
    m.extend(new);
    if diff {
        let changed = s.replace_diff(&r, &cube).unwrap();
        let moved: Vec<&Vec<u32>> = before.keys().chain(m.keys()).filter(|k| before.get(*k) != m.get(*k)).collect();
        match changed {
            None => assert!(moved.is_empty(), "変わったセルがあるのに None"),
            Some(sets) => {
                for k in moved {
                    assert!(k.iter().zip(&sets).all(|(x, set)| set.contains(x)), "変わったセル {k:?} が範囲 {sets:?} の外");
                }
            }
        }
    } else {
        s.replace(&r, &cube).unwrap();
    }
}

fn run(seed: u64, cfg: Config, index: Option<usize>) {
    let cat = catalog(cfg);
    let mut rng = Rng(seed);
    let mut s = Store::new(&[0, 1, 2], index, Kind::Num, &cat).unwrap();
    let mut m = Model::new();
    let mut forks: Vec<(Store, Model)> = Vec::new();
    for step in 0..400 {
        match rng.below(10) {
            0..=3 => {
                let (k, v) = (key(&mut rng), value(&mut rng));
                s.write(&k, v);
                match v {
                    Some(v) => m.insert(k, v),
                    None => m.remove(&k),
                };
            }
            4 | 5 => {
                let n = rng.below(40) as usize;
                let keys: Vec<Vec<u32>> = (0..n).map(|_| key(&mut rng)).collect();
                let values: Vec<Option<f64>> = (0..n).map(|_| value(&mut rng)).collect();
                let cols: Vec<Vec<u32>> = (0..SIZES.len()).map(|d| keys.iter().map(|k| k[d]).collect()).collect();
                let slices: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect();
                s.write_many(&slices, &values);
                for (k, v) in keys.into_iter().zip(values) {
                    match v {
                        Some(v) => m.insert(k, v),
                        None => m.remove(&k),
                    };
                }
            }
            6 => replace(&mut rng, &cat, &mut s, &mut m, false),
            7 => replace(&mut rng, &cat, &mut s, &mut m, true),
            8 => {
                // 軸 d のメンバーを消し、後ろの番号を詰める（軸の大きさは変えないので、最後の番号は空になる）
                let d = rng.below(SIZES.len() as u64) as usize;
                let victim = rng.below(SIZES[d] as u64) as u32;
                s.remove_member(d, victim, false);
                m = m
                    .into_iter()
                    .filter(|(k, _)| k[d] != victim)
                    .map(|(mut k, v)| {
                        if k[d] > victim {
                            k[d] -= 1;
                        }
                        (k, v)
                    })
                    .collect();
            }
            _ => forks.push((s.clone(), m.clone())), // 版を取っておく（あとで書き換わっていないか確かめる）
        }
        check(&s, &m, &mut rng, &cat, step);
    }
    for (i, (f, fm)) in forks.iter().enumerate() {
        assert_eq!(cells(f), *fm, "{i} 番目に取った版が、後の書き込みで変わった");
    }
}

#[test]
fn store_matches_btreemap() {
    let small = Config { compact_min: 3, par_min: 0, postings_min_rows: 0, ..Config::default() };
    for seed in 1..=20 {
        run(seed, Config::default(), Some(0));
        run(seed, small, Some(2));
        run(seed, small, None);
    }
}
