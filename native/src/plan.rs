//! 差分再計算の段取り。意味は Python の Model.recalc の差分の経路（evaluate.affected、
//! Model._scan_regions、Model._scan、Model._apply_delta）と同じで、テストで突き合わせる。
//!
//! 入力の変更範囲を計算計画の順に伝え、各 Metric の影響範囲だけを評価して書き戻す。書き戻すときに
//! 新旧の値を比べ、実際に値が変わったセルだけを下流への影響範囲にする。影響範囲はメンバー名ではなく
//! 番号の集合で持つので、1 回の変更で何百もの Metric を計算し直しても、Python との往復は 1 回で済む。

use crate::core::{eval, Catalog, Cube, DimId, Kind, Node, Op, Restrict, Result, Sel, Src, Store, PAR_MIN};
use rayon::prelude::*;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

// ------------------------------------------------------------------ 影響範囲

/// 影響範囲。軸ごとのメンバー番号の集合の直積で、載っていない軸は全メンバー。
/// 空の Reg は Metric 全体。「影響なし」は Option の None で表す。
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Reg {
    sels: Vec<(DimId, Vec<u32>)>, // 軸の番号の昇順。メンバーは昇順で重複なし
}

fn sorted(mut ms: Vec<u32>) -> Vec<u32> {
    ms.sort_unstable();
    ms.dedup();
    ms
}

/// 昇順の 2 つの集合の和。
fn merged(a: &[u32], b: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => {
                out.push(a[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.push(b[j]);
                j += 1;
            }
            std::cmp::Ordering::Equal => {
                out.push(a[i]);
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    out
}

impl Reg {
    pub fn new(sels: Vec<(DimId, Vec<u32>)>) -> Reg {
        let mut sels: Vec<(DimId, Vec<u32>)> = sels.into_iter().map(|(d, ms)| (d, sorted(ms))).collect();
        sels.sort_by_key(|s| s.0);
        Reg { sels }
    }

    pub fn into_parts(self) -> Vec<(DimId, Vec<u32>)> {
        self.sels
    }

    fn get(&self, d: DimId) -> Option<&[u32]> {
        self.sels.iter().find(|s| s.0 == d).map(|s| s.1.as_slice())
    }

    fn has(&self, d: DimId, m: u32) -> bool {
        self.get(d).is_none_or(|ms| ms.binary_search(&m).is_ok())
    }

    fn is_all(&self) -> bool {
        self.sels.is_empty()
    }

    fn without(&self, ds: &[DimId]) -> Reg {
        Reg { sels: self.sels.iter().filter(|s| !ds.contains(&s.0)).cloned().collect() }
    }

    /// 軸 d のメンバーを ms にした範囲。
    fn set(&self, d: DimId, ms: Vec<u32>) -> Reg {
        let mut out = self.without(&[d]);
        out.sels.push((d, sorted(ms)));
        out.sels.sort_by_key(|s| s.0);
        out
    }

    /// どれかの軸のメンバーが空なら、どのセルも含まないので None。
    fn nonempty(self) -> Option<Reg> {
        (!self.sels.iter().any(|s| s.1.is_empty())).then_some(self)
    }

    pub fn restrict(&self, cat: &Catalog) -> Restrict {
        let mut r = Restrict::all(cat.dims.len());
        for (d, ms) in &self.sels {
            r = r.with(*d, Sel::new(ms.clone(), cat.dims[*d].size));
        }
        r
    }
}

/// 2 つの影響範囲を囲む範囲（両方に載っている軸だけ、メンバーの和を取る）。
fn union(a: Option<Reg>, b: Option<Reg>) -> Option<Reg> {
    match (a, b) {
        (None, x) | (x, None) => x,
        (Some(a), Some(b)) => Some(Reg {
            sels: a.sels.iter().filter_map(|(d, ms)| b.get(*d).map(|ns| (*d, merged(ms, ns)))).collect(),
        }),
    }
}

/// 影響範囲の計算に使う環境。regions は Metric の番号ごとの変更範囲、added は軸ごとの追加したメンバー。
struct Env<'a> {
    cat: &'a Catalog,
    regions: &'a [Option<Reg>],
    added: &'a [(DimId, Vec<u32>)],
}

impl Env<'_> {
    /// dims のうちメンバーを追加した軸について、新しいメンバーの範囲を足す。
    fn grow(&self, mut r: Option<Reg>, dims: &[DimId]) -> Option<Reg> {
        for d in dims {
            if let Some((_, ms)) = self.added.iter().find(|(x, _)| x == d) {
                r = union(r, Some(Reg { sels: vec![(*d, ms.clone())] }));
            }
        }
        r
    }

    /// 式の結果のうち、値が変わりうる範囲。refs は式の Ref の番号 -> Metric の番号。
    fn affected(&self, node: &Node, refs: &[usize]) -> Option<Reg> {
        let af = |n: &Node| self.affected(n, refs);
        match node {
            Node::Ref(i) => self.regions[refs[*i]].clone(),
            Node::Const(..) | Node::MemberConst(..) => None,
            Node::By { .. } => unreachable!("型を決めた式だけを使う"),
            Node::DimRef(d) => self.grow(None, &[*d]),
            Node::Bin(_, l, r, grow) => self.grow(union(af(l), af(r)), grow),
            Node::Filter(l, r) | Node::On(l, r) | Node::Coalesce(l, r) => union(af(l), af(r)),
            Node::If(c, t, e, grow) => {
                let mut r = union(af(c), af(t));
                if let Some(e) = e {
                    r = union(r, af(e));
                }
                self.grow(r, grow)
            }
            Node::Not(c) | Node::AsAxis { child: c, .. } => af(c),
            Node::IsBlank(c, grow) | Node::IfBlank(c, _, _, grow) => self.grow(af(c), grow),
            Node::Expand(c, dims) => self.grow(af(c), dims),
            Node::Remove { child, dim, .. } => af(child).map(|r| r.without(&[*dim])),
            Node::Select { child, dim, member, .. } => {
                let r = af(child)?;
                // 変更が選んだメンバーに届かなければ影響なし
                r.has(*dim, *member).then(|| r.without(&[*dim]))
            }
            Node::Shift { child, dim, n } => {
                let r = af(child).and_then(|r| match r.get(*dim) {
                    Some(ms) => {
                        let size = self.cat.dims[*dim].size as i64;
                        let moved = ms
                            .iter()
                            .filter_map(|&m| {
                                let t = m as i64 + n;
                                (0..size).contains(&t).then_some(t as u32)
                            })
                            .collect();
                        r.set(*dim, moved).nonempty()
                    }
                    None => Some(r),
                });
                self.grow(r, &[*dim]) // 末尾に足した時点には、ずらした値が入りうる
            }
            Node::ByAgg { child, src, dst, map, .. } => {
                // 集約: 変わった社員の部署が変わる
                let r = af(child)?;
                let mp = &self.cat.maps[*map];
                let mut out = r.without(&[*src, *dst]);
                if let Some(ms) = r.get(*src) {
                    let ts = ms.iter().filter_map(|&m| mp.fwd.get(m as usize).and_then(|&t| (t >= 0).then_some(t as u32)));
                    out = out.set(*dst, ts.collect());
                }
                out.nonempty()
            }
            Node::ByLookup { child, src, dst, map } => {
                // 引き下ろし: 変わった部署に属する社員が変わる。新しい社員にも部署の値が配られる
                let r = af(child).and_then(|r| {
                    let mp = &self.cat.maps[*map];
                    let mut out = r.without(&[*src, *dst]);
                    if let Some(ts) = r.get(*dst) {
                        let ms = ts.iter().flat_map(|&t| mp.inv.get(t as usize).into_iter().flatten().copied());
                        out = out.set(*src, ms.collect());
                    }
                    out.nonempty()
                });
                self.grow(r, &[*src])
            }
        }
    }
}

// ------------------------------------------------------------------ 計算計画

/// Metric の式。refs は式の Ref の番号 -> 読み出し元の番号（Metric、または差分集計の作業データ）。
pub struct Formula {
    pub node: Arc<Node>,
    pub refs: Vec<usize>,
}

/// 差分集計の計画。inputs は [集計元, 対応表...]。d_count と d_value の refs は作業データの番号で、
/// inputs の i 番目の変更後が 2i、変更前が 2i + 1。
pub struct Delta {
    pub inputs: Vec<usize>,
    pub d_count: Formula,
    pub d_value: Option<Formula>, // None なら d_count が値の差分（COUNT の集計）
}

pub struct Metric {
    pub formula: Option<Formula>, // None なら入力
    pub count: Option<Formula>,   // 差分集計する SUM の、各グループの件数の式
    pub delta: Option<Delta>,
    pub source: bool, // 差分集計の集計元か対応表（変更前の値を取っておく）
}

pub enum Step {
    One(usize),
    Scan(DimId, Vec<usize>), // 時間軸に沿って 1 時点ずつ計算する Metric の組
}

/// 計算計画。levels は依存の段ごとの計算の段階で、同じ段の段階は互いに依存しない。
pub struct Plan {
    pub metrics: Vec<Metric>,
    pub levels: Vec<Vec<Step>>,
}

/// 段の中で計算し直す 1 つの Metric。
struct Task {
    m: usize,
    region: Reg,
    delta: bool,   // 差分集計で更新する
    weight: usize, // 計算し直す行数の見積もり（並列にするかの判断に使う）
}

/// 計算し終えて、書き戻しを待つ値。範囲が全体なら、新しい格納データまで作っておく
/// （置き換える前の格納データを読むだけで作れるので、同じ段の Metric と並列に作れる）。
enum Write {
    Whole(Store, Option<Vec<Vec<u32>>>), // 新しい格納データと、値が変わったセルの範囲
    Part(Cube),                          // 範囲の中の新しい値
}

struct Done {
    m: usize,
    region: Reg,
    delta: bool,
    value: Write,
    count: Option<Write>, // 差分集計する SUM の、各グループの件数（Whole の範囲は使わない）
    old: Option<Src>,     // 書き戻す前の値（下流の差分集計の集計元なら）
}

/// 値が変わった範囲がセル全体のこの割合以上なら、全体が変わったものとして下流へ伝える
/// （この行数以上の Metric だけ。小さな Metric では範囲を細かく伝えても安い）。
pub const WIDEN_SHARE: f64 = 0.5;
pub static WIDEN_MIN_ROWS: AtomicUsize = AtomicUsize::new(4096);

/// 再計算した Metric の記録: (Metric の番号, 差分集計なら true, 範囲)。
pub type Log = Vec<(usize, bool, Reg)>;

// 差分集計の後半で使う式（読み出し元は、変更前の値、値の差分、新しい件数の順）
fn new_count() -> Node {
    Node::Bin(Op::Add, Box::new(Node::Ref(0)), Box::new(Node::Ref(1)), vec![])
}

fn alive(i: usize) -> Box<Node> {
    Box::new(Node::Bin(Op::Gt, Box::new(Node::Ref(i)), Box::new(Node::Const(0.0, Kind::Num)), vec![]))
}

fn new_value() -> Node {
    Node::Filter(Box::new(new_count()), alive(2))
}

fn kept_count() -> Node {
    Node::Filter(Box::new(Node::Ref(0)), alive(0))
}

struct Run<'a> {
    cat: &'a Catalog,
    plan: &'a Plan,
    stores: &'a mut [Arc<Store>],
    counts: &'a mut [Option<Arc<Store>>],
    added: &'a [(DimId, Vec<u32>)],
    forced: Vec<Option<Reg>>, // 定義を変えたので、影響範囲に関係なく計算し直す範囲
    regions: Vec<Option<Reg>>,
    olds: Vec<Option<Src>>,
    log: Log,
}

/// 差分再計算。changed は入力の変更範囲、olds は差分集計の集計元になる入力の変更前の値、
/// forced は定義を変えたので必ず計算し直す計算 Metric の範囲。stores と counts（Metric の番号順）は
/// その場で書き換える。
#[allow(clippy::too_many_arguments)]
pub fn recalc(
    cat: &Catalog,
    plan: &Plan,
    stores: &mut [Arc<Store>],
    counts: &mut [Option<Arc<Store>>],
    changed: Vec<(usize, Reg)>,
    added: &[(DimId, Vec<u32>)],
    olds: Vec<(usize, Src)>,
    forced: Vec<(usize, Reg)>,
) -> Result<Log> {
    let n = plan.metrics.len();
    let added: Vec<(DimId, Vec<u32>)> = added.iter().map(|(d, ms)| (*d, sorted(ms.clone()))).collect();
    let mut run = Run {
        cat,
        plan,
        stores,
        counts,
        added: &added,
        forced: vec![None; n],
        regions: vec![None; n],
        olds: vec![None; n],
        log: Vec::new(),
    };
    for (m, r) in forced {
        run.forced[m] = Some(r);
    }
    for (m, r) in changed {
        run.regions[m] = Some(r);
    }
    for (m, s) in olds {
        run.olds[m] = Some(s);
    }
    for level in &plan.levels {
        run.level(level)?;
    }
    Ok(run.log)
}

impl<'a> Run<'a> {
    /// 依存の段を 1 つ計算し直す。同じ段の Metric は互いを読まないので、範囲を決めてから並列に計算し、
    /// 順に書き戻す。scan は時点ごとに自分の格納データへ書き込むので、そのあとで順に計算する。
    fn level(&mut self, level: &[Step]) -> Result<()> {
        let mut tasks = Vec::new();
        let mut scans = Vec::new();
        for step in level {
            match step {
                Step::One(m) => tasks.extend(self.prepare(*m)),
                Step::Scan(dim, names) => scans.push((*dim, names)),
            }
        }
        // 小さな仕事ばかりなら、並列にする受け渡しの費用のほうが大きいので順に計算する
        let weight: usize = tasks.iter().map(|t| t.weight).sum();
        let run: &Self = self;
        let done: Vec<Result<Done>> = if tasks.len() > 1 && weight >= PAR_MIN.load(Ordering::Relaxed) {
            tasks.par_iter().map(|t| run.compute(t)).collect()
        } else {
            tasks.iter().map(|t| run.compute(t)).collect()
        };
        for d in done {
            self.apply(d?)?;
        }
        for (dim, names) in scans {
            self.scan_step(dim, names)?;
        }
        Ok(())
    }

    fn env(&self) -> Env<'_> {
        Env { cat: self.cat, regions: &self.regions, added: self.added }
    }

    fn formula(&self, m: usize) -> &'a Formula {
        let plan: &'a Plan = self.plan;
        plan.metrics[m].formula.as_ref().expect("計算 Metric")
    }

    fn eval(&self, f: &Formula, work: Option<&[Src]>, r: &Restrict) -> Result<Cube> {
        let src: Vec<Src> = match work {
            Some(w) => f.refs.iter().map(|&i| w[i].clone()).collect(),
            None => f.refs.iter().map(|&i| Src::Store(self.stores[i].clone())).collect(),
        };
        eval(&f.node, self.cat, &src, r)
    }

    fn slice(&self, m: usize, r: &Reg) -> Src {
        Src::Store(Arc::new(self.stores[m].slice(&r.restrict(self.cat))))
    }

    /// 段の 1 つの Metric について、計算し直す範囲と方法を決める。計算し直さなくてよければ None。
    fn prepare(&self, m: usize) -> Option<Task> {
        let plan: &'a Plan = self.plan;
        let metric = &plan.metrics[m];
        let f = metric.formula.as_ref()?;
        let forced = self.forced[m].clone();
        let redefined = forced.is_some();
        let region = union(self.env().affected(&f.node, &f.refs), forced)?;
        let delta = match &metric.delta {
            Some(d) => !redefined && self.delta_applicable(d),
            None => false,
        };
        let size = |d: DimId| self.cat.dims[d].size.max(1) as f64;
        let share: f64 = region.sels.iter().map(|(d, ms)| ms.len() as f64 / size(*d)).product();
        let weight = (self.stores[m].rows_hint() as f64 * share) as usize;
        Some(Task { m, region, delta, weight })
    }

    /// 計算する（格納データは読むだけなので、同じ段の Metric を並列に計算できる）。
    fn compute(&self, t: &Task) -> Result<Done> {
        let plan: &'a Plan = self.plan;
        let metric = &plan.metrics[t.m];
        // 集計元なら、書き戻す前の値を下流の差分集計のために取っておく。全体を計算し直すなら、
        // 置き換える前の格納データをそのまま使う（複製しない）
        let old = metric.source.then(|| {
            if t.region.is_all() {
                Src::Store(self.stores[t.m].clone())
            } else {
                self.slice(t.m, &t.region)
            }
        });
        let (value, count) = match (&metric.delta, t.delta) {
            (Some(d), true) => self.compute_delta(t.m, d, &t.region)?,
            _ => {
                let r = t.region.restrict(self.cat);
                let value = self.eval(self.formula(t.m), None, &r)?;
                let count = match &metric.count {
                    Some(c) => Some(self.eval(c, None, &r)?),
                    None => None,
                };
                (value, count)
            }
        };
        let (value, count) = if t.region.is_all() {
            let (store, sets) = self.stores[t.m].replaced_all(&value)?;
            let count = match count {
                Some(c) => {
                    let mut s = self.counts[t.m].as_ref().expect("件数の格納").emptied();
                    s.replace(&Restrict::all(self.cat.dims.len()), &c)?;
                    Some(Write::Whole(s, None))
                }
                None => None,
            };
            (Write::Whole(store, sets), count)
        } else {
            (Write::Part(value), count.map(Write::Part))
        };
        Ok(Done { m: t.m, region: t.region.clone(), delta: t.delta, value, count, old })
    }

    /// 計算した値を書き戻し、値が実際に変わったセルの範囲を下流への影響範囲にする。
    fn apply(&mut self, done: Done) -> Result<()> {
        let Done { m, region, delta, value, count, old } = done;
        let r = region.restrict(self.cat);
        let sets = match value {
            Write::Whole(store, sets) => {
                self.stores[m] = Arc::new(store);
                sets
            }
            Write::Part(value) => Arc::make_mut(&mut self.stores[m]).replace_diff(&r, &value)?,
        };
        match count {
            Some(Write::Whole(store, _)) => self.counts[m] = Some(Arc::new(store)),
            Some(Write::Part(c)) => Arc::make_mut(self.counts[m].as_mut().expect("件数の格納")).replace(&r, &c)?,
            None => {}
        }
        if old.is_some() {
            self.olds[m] = old;
        }
        self.log.push((m, delta, region));
        if let Some(sets) = sets {
            self.regions[m] = Some(self.changed_region(m, sets));
        }
        Ok(())
    }

    /// 値が変わったセル（Metric の軸ごとのメンバー番号）を囲む範囲。全メンバーにわたる軸は範囲から外す
    /// （Python の Model._widened と同じ）。さらに WIDEN_MIN_ROWS 行以上の Metric では、範囲がセル全体の
    /// WIDEN_SHARE 以上を占めるなら全体にする。全体として扱えば、下流の書き戻しは並べ直すだけで済み、
    /// 差分集計より計算し直しを選べる（範囲を広げても結果は変わらない）。
    fn changed_region(&self, m: usize, sets: Vec<Vec<u32>>) -> Reg {
        let store = &self.stores[m];
        let dims = &store.metric_dims;
        let size = |d: DimId| self.cat.dims[d].size as usize;
        let share: f64 = dims.iter().zip(&sets).map(|(&d, ms)| ms.len() as f64 / size(d).max(1) as f64).product();
        if share >= WIDEN_SHARE && store.rows_hint() >= WIDEN_MIN_ROWS.load(Ordering::Relaxed) {
            return Reg::default();
        }
        Reg::new(dims.iter().copied().zip(sets).filter(|(d, ms)| ms.len() < size(*d)).collect())
    }

    /// 集計元と対応表の変更範囲を合わせた範囲。どれも変わっていなければ None。
    fn delta_range(&self, d: &Delta) -> Option<Reg> {
        d.inputs.iter().fold(None, |acc, &n| union(acc, self.regions[n].clone()))
    }

    fn delta_applicable(&self, d: &Delta) -> bool {
        let changed: Vec<usize> = d.inputs.iter().copied().filter(|&n| self.regions[n].is_some()).collect();
        // 範囲が Metric 全体に広がるなら、差分より計算し直すほうが速い
        !changed.is_empty()
            && changed.iter().all(|&n| self.olds[n].is_some())
            && self.delta_range(d).is_some_and(|r| !r.is_all())
    }

    /// 集計元（と対応表）の変更前後の差分を集計し、region 内の既存の値と件数に足し込んだ値と件数。
    fn compute_delta(&self, m: usize, d: &Delta, region: &Reg) -> Result<(Cube, Option<Cube>)> {
        let cat = self.cat;
        let all = Restrict::all(cat.dims.len());
        let range = self.delta_range(d).expect("変更範囲").restrict(cat);
        let mut work = Vec::with_capacity(2 * d.inputs.len());
        for &n in &d.inputs {
            let new = self.stores[n].slice(&range);
            let old = match &self.regions[n] {
                // 変更前の値は、実際に変わった範囲の分だけ戻す
                Some(changed) => {
                    let rn = changed.restrict(cat);
                    let before = self.olds[n].as_ref().expect("変更前の値").read(&rn);
                    let mut old = new.clone();
                    old.replace(&rn, &before)?;
                    Arc::new(old)
                }
                None => Arc::new(new.clone()),
            };
            work.push(Src::Store(Arc::new(new)));
            work.push(Src::Store(old));
        }
        let plan: &'a Plan = self.plan;
        let metric = &plan.metrics[m];
        let r = region.restrict(cat);
        let old_value = Src::Store(Arc::new(self.stores[m].slice(&r)));
        let old_count = match (&metric.count, &self.counts[m]) {
            (Some(_), Some(c)) => Src::Store(Arc::new(c.slice(&r))),
            _ => old_value.clone(),
        };
        let d_count = self.eval(&d.d_count, Some(&work), &all)?;
        let d_value = match &d.d_value {
            Some(f) => self.eval(f, Some(&work), &all)?,
            None => d_count.clone(),
        };
        let count = Arc::new(eval(&new_count(), cat, &[old_count, Src::Cube(Arc::new(d_count))], &all)?);
        let value = eval(&new_value(), cat, &[old_value, Src::Cube(Arc::new(d_value)), Src::Cube(count.clone())], &all)?;
        // 件数が 0 になったグループは消す
        let kept = match metric.count {
            Some(_) => Some(eval(&kept_count(), cat, &[Src::Cube(count)], &all)?),
            None => None,
        };
        Ok((value, kept))
    }

    /// scan の Metric の影響範囲を、増えなくなるまで伝えてから、時間軸に沿って 1 時点ずつ計算する。
    fn scan_step(&mut self, dim: DimId, names: &[usize]) -> Result<()> {
        for &n in names {
            self.regions[n] = union(self.regions[n].take(), self.forced[n].clone());
        }
        loop {
            let mut grown = false;
            for &n in names {
                let f = self.formula(n);
                let r = union(self.regions[n].clone(), self.env().affected(&f.node, &f.refs));
                if r != self.regions[n] {
                    self.regions[n] = r;
                    grown = true;
                }
            }
            if !grown {
                break;
            }
        }
        let active: Vec<(usize, Reg)> = names.iter().filter_map(|&n| self.regions[n].clone().map(|r| (n, r))).collect();
        for (n, r) in &active {
            if self.plan.metrics[*n].source {
                let old = if r.is_all() { Src::Store(self.stores[*n].clone()) } else { self.slice(*n, r) };
                self.olds[*n] = Some(old);
            }
            if r.is_all() {
                // 全時点を書き直すので、空から書き込む（既存のセルを時点ごとに消して書き直すより速い）
                self.stores[*n] = Arc::new(self.stores[*n].emptied());
            }
        }
        // 格納データにその場で 1 時点ずつ書き込む。次の時点の前月参照は、書き込んだばかりの時点を読む
        for t in 0..self.cat.dims[dim].size {
            for (n, r) in &active {
                if !r.has(dim, t) {
                    continue;
                }
                let sub = r.set(dim, vec![t]).restrict(self.cat);
                let value = self.eval(self.formula(*n), None, &sub)?;
                Arc::make_mut(&mut self.stores[*n]).replace(&sub, &value)?;
            }
        }
        for (n, r) in active {
            self.log.push((n, false, r));
        }
        Ok(())
    }
}
