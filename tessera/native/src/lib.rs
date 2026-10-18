//! Python から使う入口。Metric の格納データ（Store）と評価の途中結果（Cube）は Rust 側に置き、
//! Python には中身を持たないハンドルだけを渡す。

use nanashi_engine::check::{self, Env, TKind, Ty};
use nanashi_engine::plan::{self, Env as RangeEnv, Formula, Metric, Plan, Reg, Step};
use nanashi_engine::{eval, graph, ipc, pq, select, sql, Agg, Arg, Catalog, Cube, DimId, DimInfo, Diag, Kind, Mapping, Node, Op, ReadAgg, Restrict, Sel, Src, Store, Budget};
use bytes::Bytes;
use numpy::PyReadonlyArray1;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::{PyBackedBytes, PyBackedStr};
use pyo3::types::{PyBool, PyBytes, PyDict, PyIterator, PyList, PyString, PyTuple};
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::sync::atomic::{AtomicIsize, AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};

fn err(msg: String) -> PyErr {
    PyValueError::new_err(msg)
}

pyo3::create_exception!(
    nanashi_core,
    Diagnostic,
    PyValueError,
    "式の誤り。args は (コード, 値の dict)。文言は Python の sparse_engine.messages が作る。"
);

fn py_arg<'py>(py: Python<'py>, a: &Arg) -> PyResult<Bound<'py, PyAny>> {
    let all = |xs: &[Arg]| xs.iter().map(|x| py_arg(py, x)).collect::<PyResult<Vec<_>>>();
    Ok(match a {
        Arg::Str(s) => s.into_pyobject(py)?.into_any(),
        Arg::Int(n) => n.into_pyobject(py)?.into_any(),
        Arg::Tuple(xs) => PyTuple::new(py, all(xs)?)?.into_any(),
        Arg::List(xs) => PyList::new(py, all(xs)?)?.into_any(),
        Arg::Msg(d) => py_diag(py, d)?.into_any(),
    })
}

/// (コード, 値の dict)。
fn py_diag<'py>(py: Python<'py>, d: &Diag) -> PyResult<Bound<'py, PyTuple>> {
    let args = PyDict::new(py);
    for (k, v) in &d.args {
        args.set_item(k, py_arg(py, v)?)?;
    }
    (d.code, args).into_pyobject(py)
}

fn diag_err(py: Python<'_>, d: Diag) -> PyErr {
    match py_diag(py, &d) {
        Ok(t) => Diagnostic::new_err(t.unbind()),
        Err(e) => e,
    }
}

fn read_agg(agg: &str) -> PyResult<ReadAgg> {
    ReadAgg::from_name(agg).ok_or_else(|| err(format!("集計 {agg} は使えない（sum、avg、min、max、count）")))
}

fn kind_of(is_bool: bool) -> Kind {
    if is_bool {
        Kind::Bool
    } else {
        Kind::Num
    }
}

fn kind_from(kind: &str, d: i64) -> TKind {
    match kind {
        "boolean" => TKind::Bool,
        "member" => TKind::Member(d as usize),
        _ => TKind::Num,
    }
}

fn kind_to(kind: &TKind) -> (String, i64) {
    match kind {
        TKind::Num => ("number".into(), -1),
        TKind::Bool => ("boolean".into(), -1),
        TKind::Member(d) => ("member".into(), *d as i64),
    }
}

fn mapping(dst_size: u32, fwd: Vec<i64>) -> PyResult<Mapping> {
    let mut inv = vec![Vec::new(); dst_size as usize];
    for (s, &t) in fwd.iter().enumerate() {
        if t >= dst_size as i64 || t < -1 {
            return Err(err(format!("対応先のメンバー番号 {t} が軸の大きさ {dst_size} の範囲にない")));
        }
        if t >= 0 {
            inv[t as usize].push(s as u32);
        }
    }
    Ok(Mapping { fwd, inv })
}

/// ハンドルの格納データ（参照を 1 つ増やす）。別のスレッドが書き換えている途中なら RuntimeError。
fn read(h: &Bound<'_, StoreHandle>) -> PyResult<Arc<Store>> {
    Ok(h.try_borrow().map_err(busy)?.store.clone())
}

/// ハンドルの格納データを差し替える。別のスレッドが使っている途中なら RuntimeError。
fn put(h: &Bound<'_, StoreHandle>, s: Arc<Store>) -> PyResult<()> {
    h.try_borrow_mut().map_err(busy)?.store = s;
    Ok(())
}

#[pyclass(frozen)]
struct Expr {
    node: Arc<Node>,
    refs: usize, // 読み出し元の数（Ref の番号はこれより小さい）
}

#[pyclass(frozen)]
struct CubeHandle {
    cube: Arc<Cube>,
}

#[pyclass]
struct StoreHandle {
    store: Arc<Store>,
}

#[pyclass]
struct Core {
    cat: Arc<Catalog>,
}

/// 差分再計算の計算計画（Rust の段取り用）。
#[pyclass(frozen)]
struct PlanHandle {
    plan: Arc<Plan>,
}

/// Python の (式, 読み出し元の番号) から Formula を作る。None なら None。
fn formula(obj: &Bound<'_, PyAny>) -> PyResult<Option<Formula>> {
    if obj.is_none() {
        return Ok(None);
    }
    let t = obj.cast::<PyTuple>()?;
    let e = t.get_item(0)?;
    let e = e.cast::<Expr>()?.get();
    let refs: Vec<usize> = t.get_item(1)?.extract()?;
    if refs.len() != e.refs {
        return Err(err(format!("読み出す Metric の数 {} が式の読み出し元の数 {} と合わない", refs.len(), e.refs)));
    }
    Ok(Some(Formula { node: e.node.clone(), refs }))
}

/// 計算計画の式が読む Metric の番号が、どれも n より小さいか。
fn check_refs<'a>(formulas: impl Iterator<Item = &'a Option<Formula>>, n: usize) -> PyResult<()> {
    match formulas.flatten().flat_map(|f| &f.refs).find(|&&i| i >= n) {
        Some(i) => Err(err(format!("式が読む Metric の番号 {i} がない（Metric は {n} 個）"))),
        None => Ok(()),
    }
}

type Region = Vec<(DimId, Vec<u32>)>;

/// rows_in の結果（軸ごとのメンバー番号の列、値の列、真偽値か、範囲の全行数）。
type Page = (Vec<Vec<u32>>, Vec<f64>, bool, usize);

impl Expr {
    fn sources(&self, n: usize) -> PyResult<()> {
        if n != self.refs {
            return Err(err(format!("読み出し元の数 {n} が式の読み出し元の数 {} と合わない", self.refs)));
        }
        Ok(())
    }
}

impl Core {
    fn restrict(&self, region: &Region) -> PyResult<Restrict> {
        let mut r = Restrict::all(self.cat.dims.len());
        for (d, ms) in region {
            self.members(*d, ms)?;
            r = r.with(*d, Sel::new(ms.clone(), self.cat.dims[*d].size));
        }
        Ok(r)
    }

    fn dim(&self, d: DimId) -> PyResult<()> {
        if d >= self.cat.dims.len() {
            return Err(err(format!("軸の番号 {d} がない（軸は {} 個）", self.cat.dims.len())));
        }
        Ok(())
    }

    fn dims(&self, ds: &[DimId]) -> PyResult<()> {
        ds.iter().try_for_each(|&d| self.dim(d))
    }

    /// 軸 d のメンバー番号の列 ms が、どれも軸の大きさより小さいか。
    fn members(&self, d: DimId, ms: &[u32]) -> PyResult<()> {
        self.dim(d)?;
        let size = self.cat.dims[d].size;
        match ms.iter().find(|&&m| m >= size) {
            Some(m) => Err(err(format!("軸 {} のメンバー番号 {m} が軸の大きさ {size} を超える", self.cat.dims[d].name))),
            None => Ok(()),
        }
    }

    /// 宣言した軸 dims の順の、軸ごとのメンバー番号の列 cols が、長さ n でそろい、範囲に収まるか。
    fn columns(&self, dims: &[DimId], cols: &[&[u32]], n: usize) -> PyResult<()> {
        if cols.len() != dims.len() || cols.iter().any(|c| c.len() != n) {
            return Err(err(format!("列の数（{}）か長さが、軸の数（{}）と値の数（{n}）に合わない", cols.len(), dims.len())));
        }
        dims.iter().zip(cols).try_for_each(|(&d, c)| self.members(d, c))
    }

    /// 格納データの 1 セルのキー（宣言した軸の順のメンバー番号）が、軸の数と大きさに合うか。
    fn key(&self, s: &Store, key: &[u32]) -> PyResult<()> {
        if key.len() != s.metric_dims.len() {
            return Err(err(format!("キーの長さ {} が軸の数 {} と合わない", key.len(), s.metric_dims.len())));
        }
        s.metric_dims.iter().zip(key).try_for_each(|(&d, &m)| self.members(d, &[m]))
    }

    /// The rank table t of dimension d (number -> rank) is a permutation of the members.
    fn ranks(&self, d: DimId, t: Option<&[u32]>) -> PyResult<()> {
        let size = self.cat.dims[d].size as usize;
        if let Some(t) = t {
            let mut seen = vec![false; size];
            if t.len() != size || !t.iter().all(|&x| (x as usize) < size && !std::mem::replace(&mut seen[x as usize], true)) {
                return Err(err(format!("軸 {} の並び順の表が、メンバー数 {size} の並べ替えになっていない", self.cat.dims[d].name)));
            }
        }
        Ok(())
    }

    fn added(&self, added: &[(DimId, Vec<u32>)]) -> PyResult<()> {
        added.iter().try_for_each(|(d, ms)| self.members(*d, ms))
    }

    /// 計算計画の Metric の番号 m の範囲。
    fn reg(&self, plan: &Plan, m: usize, r: Region) -> PyResult<(usize, Reg)> {
        if m >= plan.metrics.len() {
            return Err(err(format!("Metric の番号 {m} が計算計画にない")));
        }
        r.iter().try_for_each(|(d, ms)| self.members(*d, ms))?;
        // A list of all the members of an axis is the whole axis. Then a wide edit writes a whole result
        let size = |d: DimId| self.cat.dims[d].size as usize;
        let r = r
            .into_iter()
            .filter_map(|(d, mut ms)| {
                ms.sort_unstable();
                ms.dedup();
                (ms.len() < size(d)).then_some((d, ms))
            })
            .collect();
        Ok((m, Reg::new(r)))
    }

    fn source(obj: &Bound<'_, PyAny>) -> PyResult<Src> {
        if let Ok(s) = obj.cast::<StoreHandle>() {
            return Ok(Src::Store(s.try_borrow().map_err(busy)?.store.clone()));
        }
        if let Ok(c) = obj.cast::<CubeHandle>() {
            return Ok(Src::Cube(c.get().cube.clone()));
        }
        Err(err("読み出し元は StoreHandle か CubeHandle でなければならない".into()))
    }

    fn as_cube(obj: &Bound<'_, PyAny>) -> PyResult<Arc<Cube>> {
        match Core::source(obj)? {
            Src::Store(s) => Ok(Arc::new(s.as_cube())),
            Src::Cube(c) => Ok(c),
        }
    }

    /// Python から受け取った式の軸、対応表、読み出し元の番号が、どれも範囲にあるか。
    fn check_node(&self, n: &Node, refs: usize) -> PyResult<()> {
        let map = |m: usize| if m < self.cat.maps.len() { Ok(()) } else { Err(err(format!("対応表の番号 {m} がない"))) };
        match n {
            Node::Ref(i) | Node::ByMetric { metric: i, .. } if *i >= refs => {
                return Err(err(format!("読み出し元の番号 {i} がない（読み出し元は {refs} 個）")))
            }
            Node::Ref(_) | Node::Const(..) => {}
            Node::DimRef(d) => self.dim(*d)?,
            Node::MemberConst(d, m) => self.members(*d, &[*m])?,
            Node::Bin(_, a, b, ds) | Node::If(a, b, None, ds) => {
                self.dims(ds)?;
                self.check_node(a, refs)?;
                self.check_node(b, refs)?;
            }
            Node::If(a, b, Some(c), ds) => {
                self.dims(ds)?;
                self.check_node(a, refs)?;
                self.check_node(b, refs)?;
                self.check_node(c, refs)?;
            }
            Node::Filter(a, b) | Node::On(a, b) | Node::Coalesce(a, b) => {
                self.check_node(a, refs)?;
                self.check_node(b, refs)?;
            }
            Node::Not(c) => self.check_node(c, refs)?,
            Node::Expand(c, ds) | Node::IsBlank(c, ds) | Node::IfBlank(c, _, _, ds) => {
                self.dims(ds)?;
                self.check_node(c, refs)?;
            }
            Node::By { child, src, dst, map: m, .. } | Node::ByAgg { child, src, dst, map: m, .. } | Node::ByLookup { child, src, dst, map: m } => {
                self.dims(&[*src, *dst])?;
                map(*m)?;
                self.check_node(child, refs)?;
            }
            Node::ByMetric { child, src, .. } => {
                self.dim(*src)?;
                self.check_node(child, refs)?;
            }
            Node::Remove { child, dim, .. } | Node::Shift { child, dim, .. } | Node::AsAxis { child, dim } => {
                self.dim(*dim)?;
                self.check_node(child, refs)?;
            }
            Node::Select { child, dim, member, .. } => {
                self.members(*dim, &[*member])?;
                self.check_node(child, refs)?;
            }
        }
        Ok(())
    }

    fn node(&self, t: &Bound<'_, PyAny>) -> PyResult<Node> {
        let t = t.cast::<PyTuple>()?;
        let tag: String = t.get_item(0)?.extract()?;
        let child = |i: usize| -> PyResult<Box<Node>> { Ok(Box::new(self.node(&t.get_item(i)?)?)) };
        let agg = |s: String| -> PyResult<Agg> {
            Ok(match s.as_str() {
                "sum" => Agg::Sum,
                "avg" => Agg::Avg,
                "min" => Agg::Min,
                "max" => Agg::Max,
                "count" => Agg::Count,
                "first" => Agg::First,
                "low" => Agg::Low,
                _ => return Err(err(format!("未知の集計関数 {s}"))),
            })
        };
        Ok(match tag.as_str() {
            "ref" => Node::Ref(t.get_item(1)?.extract()?),
            "dimref" => Node::DimRef(t.get_item(1)?.extract()?),
            "const" => Node::Const(t.get_item(1)?.extract()?, kind_of(t.get_item(2)?.extract()?)),
            "member" => Node::MemberConst(t.get_item(1)?.extract()?, t.get_item(2)?.extract()?),
            "bin" => {
                let op: String = t.get_item(1)?.extract()?;
                let op = match op.as_str() {
                    "+" => Op::Add,
                    "-" => Op::Sub,
                    "*" => Op::Mul,
                    "/" => Op::Div,
                    "=" => Op::Eq,
                    "<>" => Op::Ne,
                    "<" => Op::Lt,
                    "<=" => Op::Le,
                    ">" => Op::Gt,
                    ">=" => Op::Ge,
                    "and" => Op::And,
                    "or" => Op::Or,
                    _ => return Err(err(format!("未知の演算子 {op}"))),
                };
                Node::Bin(op, child(2)?, child(3)?, Vec::new())
            }
            "not" => Node::Not(child(1)?),
            "if" => {
                let e = t.get_item(3)?;
                let e = if e.is_none() { None } else { Some(Box::new(self.node(&e)?)) };
                Node::If(child(1)?, child(2)?, e, Vec::new())
            }
            "filter" => Node::Filter(child(1)?, child(2)?),
            "on" => Node::On(child(1)?, child(2)?),
            "coalesce" => Node::Coalesce(child(1)?, child(2)?),
            "expand" => Node::Expand(child(1)?, t.get_item(2)?.extract()?),
            "isblank" => Node::IsBlank(child(1)?, Vec::new()),
            "ifblank" => Node::IfBlank(child(1)?, t.get_item(2)?.extract()?, t.get_item(3)?.extract()?, Vec::new()),
            "by" => {
                let a = t.get_item(5)?;
                let agg = if a.is_none() { None } else { Some(agg(a.extract()?)?) };
                Node::By {
                    child: child(1)?,
                    src: t.get_item(2)?.extract()?,
                    dst: t.get_item(3)?.extract()?,
                    map: t.get_item(4)?.extract()?,
                    agg,
                    dim: t.get_item(6)?.extract()?,
                    prop: t.get_item(7)?.extract()?,
                }
            }
            "remove" => Node::Remove { child: child(1)?, dim: t.get_item(2)?.extract()?, agg: agg(t.get_item(3)?.extract()?)? },
            "shift" => Node::Shift { child: child(1)?, dim: t.get_item(2)?.extract()?, n: t.get_item(3)?.extract()? },
            "asaxis" => Node::AsAxis { child: child(1)?, dim: t.get_item(2)?.extract()? },
            "bymetric" => {
                let a = t.get_item(4)?;
                let agg = if a.is_none() { None } else { Some(agg(a.extract()?)?) };
                Node::ByMetric {
                    child: child(1)?,
                    src: t.get_item(2)?.extract()?,
                    metric: t.get_item(3)?.extract()?,
                    agg,
                    dim: t.get_item(5)?.extract()?,
                    prop: t.get_item(6)?.extract()?,
                }
            }
            "select" => Node::Select {
                child: child(1)?,
                dim: t.get_item(2)?.extract()?,
                member: t.get_item(3)?.extract()?,
                name: t.get_item(4)?.extract()?,
            },
            _ => return Err(err(format!("未知のノード {tag}"))),
        })
    }
}

#[pymethods]
impl Core {
    /// config は速さのための調整値（nanashi_engine::Config のフィールド名。結果は変えない）。
    #[new]
    #[pyo3(signature = (**config))]
    fn new(config: Option<&Bound<'_, PyDict>>) -> PyResult<Core> {
        let mut core = Core { cat: Arc::new(Catalog::default()) };
        core.configure(config)?;
        Ok(core)
    }

    /// Change the tuning values. max_bytes is the memory budget of one operation: the stored data that it holds plus
    /// its intermediate results (bytes, None for no limit). If an operation is over it, the engine gives a ValueError
    /// before or during the calculation. Evaluation and the recalculation schedule use a new value at once. The
    /// values for the stored data (compaction, index and parallel thresholds) apply to the stored data made after the
    /// change. fail_at is for tests: (Metric number, panic or not), or None.
    #[pyo3(signature = (**config))]
    fn configure(&mut self, config: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        let Some(config) = config else { return Ok(()) };
        let cfg = &mut Arc::make_mut(&mut self.cat).cfg;
        for (k, v) in config.iter() {
            let k: String = k.extract()?;
            match k.as_str() {
                "compact_min" => cfg.compact_min = v.extract()?,
                "seg_rows" => cfg.seg_rows = v.extract()?,
                "par_min" => cfg.par_min = v.extract()?,
                "postings_min_rows" => cfg.postings_min_rows = v.extract()?,
                "stream_always" => cfg.stream_always = v.extract()?,
                "fuse" => cfg.fuse = v.extract()?,
                "dense_always" => cfg.dense_always = v.extract()?,
                "scatter_bytes" => cfg.scatter_bytes = v.extract()?,
                "chunk" => cfg.chunk = v.extract()?,
                "widen_min_rows" => cfg.widen_min_rows = v.extract()?,
                "max_bytes" => cfg.max_bytes = v.extract::<Option<usize>>()?.unwrap_or(usize::MAX),
                "fail_at" => cfg.fail_at = v.extract()?,
                _ => return Err(err(format!("未知の調整値 {k}"))),
            }
        }
        Ok(())
    }

    /// 同じ軸と対応表を持つ Core（モデルの複製用）。以後の変更は互いに影響しない（書き込み時に複製）。
    fn fork(&self) -> Core {
        Core { cat: self.cat.clone() }
    }

    /// 同じ中身を指す別のハンドル。どちらかに書き込むと、そのときに中身が複製される。
    fn share(&self, store: &Bound<'_, StoreHandle>) -> PyResult<StoreHandle> {
        Ok(StoreHandle { store: read(store)? })
    }

    fn add_dim(&mut self, size: u32, ordered: bool, name: String) -> usize {
        let cat = Arc::make_mut(&mut self.cat);
        cat.dims.push(DimInfo { size, ordered, name });
        cat.dims.len() - 1
    }

    /// 軸のメンバー数を変える（メンバーの追加と削除）。
    fn resize_dim(&mut self, dim: DimId, size: u32) -> PyResult<()> {
        self.dim(dim)?;
        Arc::make_mut(&mut self.cat).dims[dim].size = size;
        Ok(())
    }

    /// Remove member m of dim from the stored data and close up the numbers after it (also the value numbers if
    /// values).
    fn remove_member(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, dim: DimId, m: u32, values: bool) -> PyResult<()> {
        self.dim(dim)?; // The dimension is already smaller, so m can be equal to its size (then m was the last member)
        let last = m >= self.cat.dims[dim].size;
        let mut handle = store.try_borrow_mut().map_err(busy)?;
        let target = Arc::make_mut(&mut handle.store);
        py.detach(|| target.remove_member(dim, m, values, last));
        Ok(())
    }

    /// 値が value のセルを消す。
    fn drop_value(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, value: f64) -> PyResult<()> {
        let mut handle = store.try_borrow_mut().map_err(busy)?;
        let target = Arc::make_mut(&mut handle.store);
        py.detach(|| target.drop_value(value));
        Ok(())
    }

    /// 値が value のセルを囲む範囲（軸ごとのメンバー番号）。なければ None。
    fn region_of_value(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, value: f64) -> PyResult<Option<Vec<Vec<u32>>>> {
        let s = read(store)?;
        Ok(py.detach(move || s.region_of_value(value)))
    }

    /// src のメンバー番号 -> dst のメンバー番号（なければ -1）の対応を登録する。
    fn add_mapping(&mut self, dst_size: u32, fwd: Vec<i64>) -> PyResult<usize> {
        let m = mapping(dst_size, fwd)?;
        let cat = Arc::make_mut(&mut self.cat);
        cat.maps.push(Arc::new(m));
        Ok(cat.maps.len() - 1)
    }

    /// 登録済みの対応を置き換える（メンバーの追加やプロパティの設定のあと）。番号は変わらない。
    fn set_mapping(&mut self, id: usize, dst_size: u32, fwd: Vec<i64>) -> PyResult<()> {
        if id >= self.cat.maps.len() {
            return Err(err(format!("対応表の番号 {id} がない")));
        }
        let m = mapping(dst_size, fwd)?;
        Arc::make_mut(&mut self.cat).maps[id] = Arc::new(m);
        Ok(())
    }

    /// メンバーが増えてキーのビット幅に収まらなくなった格納データを詰め直す。収まるなら None。
    fn fit(&self, store: &Bound<'_, StoreHandle>) -> PyResult<Option<StoreHandle>> {
        let s = &read(store)?;
        if s.pack.fits(&self.cat) {
            return Ok(None);
        }
        Ok(Some(StoreHandle { store: Arc::new(s.repacked(&self.cat).map_err(err)?) }))
    }

    /// 式を変換して型を検査する。names は Ref の番号ごとの名前、types はその型
    /// （軸の番号の列, "number" | "boolean" | "member", メンバー型なら軸の番号）。
    /// 返すのは (変換した式, 軸の番号の列, 種類, メンバー型の軸の番号, 警告)。型の誤りは Diagnostic、
    /// 警告は (コード, 値の dict) の列（文言は Python の sparse_engine.messages が作る）。
    #[allow(clippy::type_complexity)]
    fn compile<'py>(
        &self,
        py: Python<'py>,
        tree: &Bound<'py, PyAny>,
        names: Vec<String>,
        types: Vec<(Vec<DimId>, String, i64)>,
    ) -> PyResult<(Expr, Vec<DimId>, String, i64, Vec<Bound<'py, PyTuple>>)> {
        let mut node = self.node(tree)?;
        self.check_node(&node, types.len())?;
        types.iter().try_for_each(|(dims, _, d)| {
            self.dims(dims)?;
            if *d >= 0 {
                self.dim(*d as usize)?;
            }
            Ok::<(), PyErr>(())
        })?;
        let refs = types.len();
        let types: Vec<Ty> = types
            .into_iter()
            .map(|(dims, kind, d)| Ty { dims, kind: kind_from(&kind, d) })
            .collect();
        let _ = &names; // 名前は Python 側の文言にだけ使う
        let env = Env { cat: &self.cat, types: &types };
        let mut warnings = Vec::new();
        let ty = check::infer(&mut node, &env, &mut warnings).map_err(|d| diag_err(py, d))?;
        let warnings = warnings.iter().map(|w| py_diag(py, w)).collect::<PyResult<_>>()?;
        let (kind, d) = kind_to(&ty.kind);
        Ok((Expr { node: Arc::new(node), refs }, ty.dims, kind, d, warnings))
    }

    /// 型を決めた式の結果のセル数の見積もり（上限）。refs は Ref の番号ごとの (軸, セル数)。
    fn estimate(&self, expr: &Expr, refs: Vec<(Vec<DimId>, f64)>) -> PyResult<f64> {
        expr.sources(refs.len())?;
        refs.iter().try_for_each(|(ds, _)| self.dims(ds))?;
        Ok(check::estimate(&expr.node, &self.cat, &refs).map_err(err)?.1)
    }

    #[pyo3(signature = (dims, index, is_bool))]
    fn empty(&self, dims: Vec<DimId>, index: Option<DimId>, is_bool: bool) -> PyResult<StoreHandle> {
        self.dims(&dims)?;
        let store = Store::new(&dims, index, kind_of(is_bool), &self.cat).map_err(err)?;
        Ok(StoreHandle { store: Arc::new(store) })
    }

    #[allow(clippy::wrong_self_convention)] // Python から呼ぶ名前を保つ
    fn from_rows(&self, dims: Vec<DimId>, index: Option<DimId>, is_bool: bool, cols: Vec<Vec<u32>>, values: Vec<f64>) -> PyResult<StoreHandle> {
        self.dims(&dims)?;
        let store = Store::new(&dims, index, kind_of(is_bool), &self.cat).map_err(err)?;
        let cols: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect();
        self.columns(&dims, &cols, values.len())?;
        Ok(StoreHandle { store: Arc::new(store.with_rows(&cols, &values).map_err(err)?) })
    }

    #[allow(clippy::wrong_self_convention)] // Python から呼ぶ名前を保つ
    fn from_arrays(
        &self,
        dims: Vec<DimId>,
        index: Option<DimId>,
        is_bool: bool,
        cols: Vec<PyReadonlyArray1<u32>>,
        values: PyReadonlyArray1<f64>,
    ) -> PyResult<StoreHandle> {
        self.dims(&dims)?;
        let store = Store::new(&dims, index, kind_of(is_bool), &self.cat).map_err(err)?;
        let slices: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect::<Result<_, _>>()?;
        let values = values.as_slice()?;
        self.columns(&dims, &slices, values.len())?;
        Ok(StoreHandle { store: Arc::new(store.with_rows(&slices, values).map_err(err)?) })
    }

    fn write(&self, store: &Bound<'_, StoreHandle>, key: Vec<u32>, value: Option<f64>) -> PyResult<()> {
        let mut handle = store.try_borrow_mut().map_err(busy)?;
        self.key(&handle.store, &key)?;
        Arc::make_mut(&mut handle.store).write(&key, value);
        Ok(())
    }

    /// まとめて書き込む。cols は宣言した軸の順の、軸ごとのメンバー番号の列、values の None は消す。
    /// 同じセルが複数あれば後のものが勝つ。GIL を外して行う。
    fn write_many(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, cols: Vec<Vec<u32>>, values: Vec<Option<f64>>) -> PyResult<()> {
        let mut s = read(store)?;
        let slices: Vec<&[u32]> = cols.iter().map(|x| x.as_slice()).collect();
        self.columns(&s.metric_dims, &slices, values.len())?;
        let s = py.detach(move || {
            let slices: Vec<&[u32]> = cols.iter().map(|x| x.as_slice()).collect();
            Arc::make_mut(&mut s).write_many(&slices, &values);
            s
        });
        put(store, s)
    }

    /// 1 セルの値（宣言した軸の順のメンバー番号）。空なら None。格納データ全体を読まない。
    fn get(&self, store: &Bound<'_, StoreHandle>, key: Vec<u32>) -> PyResult<Option<f64>> {
        let s = read(store)?;
        self.key(&s, &key)?;
        Ok(s.get(&key))
    }

    fn evaluate(&self, py: Python<'_>, expr: &Expr, sources: Vec<Bound<'_, PyAny>>, region: Region) -> PyResult<CubeHandle> {
        expr.sources(sources.len())?;
        let src: Vec<Src> = sources.iter().map(Core::source).collect::<PyResult<_>>()?;
        let (node, cat, r) = (expr.node.clone(), self.cat.clone(), self.restrict(&region)?);
        let cube = py.detach(move || eval(&node, &cat, &src, &r)).map_err(err)?;
        Ok(CubeHandle { cube: Arc::new(cube) })
    }

    /// 互いに独立な式をまとめて評価する（GIL を外して並列に）。
    fn evaluate_many(&self, py: Python<'_>, items: Vec<(PyRef<'_, Expr>, Vec<Bound<'_, PyAny>>, Region)>) -> PyResult<Vec<CubeHandle>> {
        let jobs: Vec<(Arc<Node>, Vec<Src>, Restrict)> = items
            .iter()
            .map(|(e, s, reg)| {
                e.sources(s.len())?;
                Ok((e.node.clone(), s.iter().map(Core::source).collect::<PyResult<_>>()?, self.restrict(reg)?))
            })
            .collect::<PyResult<_>>()?;
        let cat = self.cat.clone();
        // メモリの予算に限りがあれば、式ごとに予算をすべて使えるよう順に評価する
        let cubes: Vec<nanashi_engine::Result<Cube>> = py.detach(move || {
            if cat.cfg.max_bytes == usize::MAX {
                jobs.par_iter().map(|(n, s, r)| eval(n, &cat, s, r)).collect()
            } else {
                jobs.iter().map(|(n, s, r)| eval(n, &cat, s, r)).collect()
            }
        });
        cubes.into_iter().map(|c| Ok(CubeHandle { cube: Arc::new(c.map_err(err)?) })).collect()
    }

    /// 格納データから region の範囲を切り出した、新しい格納データ（GIL を外して読む）。
    fn filter(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, region: Region) -> PyResult<StoreHandle> {
        let (s, r) = (read(store)?, self.restrict(&region)?);
        Ok(StoreHandle { store: Arc::new(py.detach(move || s.slice(&r))) })
    }

    /// Aggregate the region of the store to the dimensions in keep (sum, avg, min, max or count), with the GIL
    /// released. ids has the member ids of each kept dimension, in metric_dims order. Returns one tuple of
    /// member ids for each kept dimension and the values as f64 bytes in native byte order (for array.array).
    #[allow(clippy::type_complexity)]
    fn summarize<'py>(
        &self,
        py: Python<'py>,
        store: &Bound<'_, StoreHandle>,
        keep: Vec<DimId>,
        region: Region,
        agg: &str,
        ids: Vec<Bound<'py, PyList>>,
    ) -> PyResult<(Vec<Bound<'py, PyTuple>>, Bound<'py, PyBytes>)> {
        let agg = read_agg(agg)?;
        self.dims(&keep)?;
        let (s, r, cat) = (read(store)?, self.restrict(&region)?, self.cat.clone());
        let g = py.detach(move || s.summarize(&keep, &r, agg, &cat, &Budget::new(cat.cfg.max_bytes, s.memory().bytes()))).map_err(err)?;
        if ids.len() != g.cols.len() {
            return Err(err(format!("ids の数 {} が残す軸の数 {} と違う", ids.len(), g.cols.len())));
        }
        let cols = g.cols.iter().zip(&ids).map(|(c, l)| {
            PyTuple::new(py, c.iter().map(|&i| l.get_item(i as usize)).collect::<PyResult<Vec<_>>>()?)
        }).collect::<PyResult<_>>()?;
        let vals = PyBytes::new_with(py, g.vals.len() * 8, |b| {
            b.as_chunks_mut::<8>().0.iter_mut().zip(&g.vals).for_each(|(o, x)| *o = x.to_ne_bytes());
            Ok(())
        })?;
        Ok((cols, vals))
    }

    /// Run a resolved SQL plan (select::run) without the GIL and return the JSON or Arrow body.
    /// A column or a sort key is ("dim", index into keep) or ("agg", index into stores).
    /// Err: Diagnostic (sql_too_large, sql_budget), or ValueError if a number does not agree with the catalog.
    /// RuntimeError if a Metric does not have each kept dimension (Python checks it first).
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (stores, aggs, keep, region, columns, order, limit, names, ranks, seq, fmt, max_cells))]
    fn select<'py>(
        &self,
        py: Python<'py>,
        stores: Vec<Bound<'py, StoreHandle>>,
        aggs: Vec<String>,
        keep: Vec<DimId>,
        region: Region,
        columns: Vec<(String, String, usize)>,
        order: Vec<(String, usize, bool, bool)>,
        limit: Option<usize>,
        names: Vec<Vec<PyBackedStr>>,
        ranks: Vec<Option<Vec<u32>>>,
        seq: u64,
        fmt: &str,
        max_cells: usize,
    ) -> PyResult<Bound<'py, PyBytes>> {
        self.dims(&keep)?;
        if stores.len() != aggs.len() || names.len() != keep.len() || ranks.len() != keep.len() {
            return Err(err("stores と aggs、names と ranks と keep の数が合わない".into()));
        }
        for (j, &d) in keep.iter().enumerate() {
            if names[j].len() != self.cat.dims[d].size as usize {
                return Err(err(format!("軸 {} の名前の数 {} がメンバー数と違う", self.cat.dims[d].name, names[j].len())));
            }
            self.ranks(d, ranks[j].as_deref())?;
        }
        let aggs = stores.iter().zip(&aggs).map(|(s, a)| Ok((read(s)?, read_agg(a)?))).collect::<PyResult<Vec<_>>>()?;
        if aggs.is_empty() || aggs.iter().any(|(s, _)| !keep.iter().all(|d| s.metric_dims.contains(d))) {
            return Err(PyRuntimeError::new_err("集計がないか、Metric に GROUP BY の軸がない"));
        }
        let col = |kind: &str, i: usize| match kind {
            "dim" if i < keep.len() => Ok(select::Col::Dim(i)),
            "agg" if i < aggs.len() => Ok(select::Col::Agg(i)),
            _ => Err(err(format!("列 ({kind}, {i}) がない"))),
        };
        let columns = columns.into_iter().map(|(n, k, i)| Ok((n, col(&k, i)?))).collect::<PyResult<_>>()?;
        let order = order.into_iter().map(|(k, i, desc, nulls_first)| Ok(select::Key { col: col(&k, i)?, desc, nulls_first })).collect::<PyResult<_>>()?;
        let format = match fmt {
            "json" => select::Format::Json,
            "arrow" => select::Format::Arrow,
            _ => return Err(err(format!("形式 {fmt} は使えない（json、arrow）"))),
        };
        let sel = select::Select { keep, aggs, region: self.restrict(&region)?, columns, order, limit, names, ranks, max_cells, seq, format };
        let cat = self.cat.clone();
        // Borrow sel, so that the names drop with the thread state, after detach
        let body = py.detach(|| select::run(&sel, &cat)).map_err(|d| diag_err(py, d))?;
        Ok(PyBytes::new(py, &body))
    }

    fn replace(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, region: Region, new: &Bound<'_, PyAny>) -> PyResult<()> {
        let new = Core::as_cube(new)?;
        let r = self.restrict(&region)?;
        let mut handle = store.try_borrow_mut().map_err(busy)?;
        let target = Arc::make_mut(&mut handle.store);
        py.detach(|| target.replace(&r, &new)).map_err(err)
    }

    /// replace と同じだが、値が実際に変わったセルの範囲（軸ごとのメンバー番号）を返す。変化なしなら None。
    fn replace_diff(
        &self,
        py: Python<'_>,
        store: &Bound<'_, StoreHandle>,
        region: Region,
        new: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Vec<Vec<u32>>>> {
        let new = Core::as_cube(new)?;
        let r = self.restrict(&region)?;
        let mut handle = store.try_borrow_mut().map_err(busy)?;
        let target = Arc::make_mut(&mut handle.store);
        py.detach(|| target.replace_diff(&r, &new)).map_err(err)
    }

    /// 評価結果から新しい格納データを作る。
    fn store_from(&self, new: &Bound<'_, PyAny>, index: Option<DimId>) -> PyResult<StoreHandle> {
        let c = Core::as_cube(new)?;
        let mut store = Store::new(c.dims(), index, c.kind, &self.cat).map_err(err)?;
        store.replace(&Restrict::all(self.cat.dims.len()), &c).map_err(err)?;
        Ok(StoreHandle { store: Arc::new(store) })
    }

    fn repartition(&self, store: &Bound<'_, StoreHandle>, index: Option<DimId>) -> PyResult<StoreHandle> {
        let s = &read(store)?;
        let mut out = Store::new(&s.metric_dims, index, s.kind, &self.cat).map_err(err)?;
        out.replace(&Restrict::all(self.cat.dims.len()), &s.as_cube()).map_err(err)?;
        Ok(StoreHandle { store: Arc::new(out) })
    }

    fn rows(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>) -> PyResult<(Vec<Vec<u32>>, Vec<f64>, bool)> {
        let s = read(store)?;
        let is_bool = s.kind == Kind::Bool;
        let (cols, values) = py.detach(move || s.rows());
        Ok((cols, values, is_bool))
    }

    /// region の範囲の行を宣言した軸の順に各軸のメンバーの並び順で並べ、offset 件目から limit 件だけ返す
    /// （軸ごとのメンバー番号の列、値の列、真偽値か、範囲の全行数）。表示やページングに使う。ranks は
    /// 宣言した軸ごとの「番号 -> 並び順の位置」の表で、None の軸は番号の順に並ぶ。
    #[pyo3(signature = (store, region, ranks = Vec::new(), offset = 0, limit = None))]
    fn rows_in(
        &self,
        py: Python<'_>,
        store: &Bound<'_, StoreHandle>,
        region: Region,
        ranks: Vec<Option<Vec<u32>>>,
        offset: usize,
        limit: Option<usize>,
    ) -> PyResult<Page> {
        let (s, r) = (read(store)?, self.restrict(&region)?);
        if !ranks.is_empty() && ranks.len() != s.metric_dims.len() {
            return Err(err(format!("並び順の表の数 {} が軸の数 {} と合わない", ranks.len(), s.metric_dims.len())));
        }
        s.metric_dims.iter().zip(&ranks).try_for_each(|(&d, t)| self.ranks(d, t.as_deref()))?;
        let is_bool = s.kind == Kind::Bool;
        let (cols, values, total) = py.detach(move || s.rows_in(&r, &ranks, offset, limit));
        Ok((cols, values, is_bool, total))
    }

    /// 格納データを Parquet のバイト列にする（列は宣言した軸の順に names、値の列 v）。GIL を外して行う。
    #[pyo3(signature = (store, names, value, meta))]
    fn store_to_parquet<'py>(
        &self,
        py: Python<'py>,
        store: &Bound<'py, StoreHandle>,
        names: Vec<String>,
        value: &str,
        meta: Vec<(String, String)>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let s = read(store)?;
        let value = pq::Value::parse(value).map_err(err)?;
        let buf = py
            .detach(move || {
                let (cols, values) = s.rows();
                pq::write(&names, cols, values, value, &meta)
            })
            .map_err(err)?;
        Ok(PyBytes::new(py, &buf))
    }

    /// Makes stored data from bytes that store_to_parquet wrote. It releases the GIL during the work.
    /// For a member value, give value_dim, the dimension of the value.
    #[allow(clippy::wrong_self_convention)] // keep the name that Python calls
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (data, dims, index, value, names, value_dim=None))]
    fn store_from_parquet(
        &self,
        py: Python<'_>,
        data: PyBackedBytes,
        dims: Vec<DimId>,
        index: Option<DimId>,
        value: &str,
        names: Vec<String>,
        value_dim: Option<DimId>,
    ) -> PyResult<StoreHandle> {
        let value = pq::Value::parse(value).map_err(err)?;
        match (value == pq::Value::Member, value_dim) {
            (true, None) => return Err(err("メンバー型の値には値の軸 (value_dim) が必要".into())),
            (false, Some(d)) => return Err(err(format!("メンバー型でない値に値の軸 (value_dim = {d}) は使えない"))),
            _ => {}
        }
        self.dims(&dims)?;
        self.dims(value_dim.as_slice())?;
        let cat = self.cat.clone();
        let store = py
            .detach(move || {
                let sizes: Vec<u32> = dims.iter().chain(&value_dim).map(|d| cat.dims[*d].size).collect();
                let (cols, values) = pq::read(Bytes::from_owner(data), &names, value, &sizes)?;
                let cols: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect();
                Store::new(&dims, index, kind_of(value == pq::Value::Bool), &cat)?.with_rows(&cols, &values)
            })
            .map_err(err)?;
        Ok(StoreHandle { store: Arc::new(store) })
    }

    /// 2 つのハンドルが同じ格納データ（複製しただけで、どちらにも書き込んでいない）を指すか。
    fn same_store(&self, a: &Bound<'_, StoreHandle>, b: &Bound<'_, StoreHandle>) -> PyResult<bool> {
        let (a, b) = (read(a)?, read(b)?);
        Ok(a.same_as(&b))
    }

    /// The cells with different values in old (before) and new (after). If old is None, all cells of new (all
    /// were blank before). If the key packings differ, None (the caller compares in a different way).
    ///
    /// maps gives, for each declared dimension, the new number of each old number (None: the member was removed).
    /// A dimension whose numbers did not change has None. value_map gives the new number of each old member-type
    /// value; the caller gives a removed member a number too. Runs without the GIL.
    #[pyo3(signature = (old, new, maps = None, value_map = None))]
    fn diff_block(
        &self,
        py: Python<'_>,
        old: Option<&Bound<'_, StoreHandle>>,
        new: &Bound<'_, StoreHandle>,
        maps: Option<Vec<Option<Vec<Option<u32>>>>>,
        value_map: Option<Vec<u32>>,
    ) -> PyResult<Option<Diff>> {
        let a = old.map(read).transpose()?;
        let b = read(new)?;
        let gone = |t: Vec<Option<u32>>| -> Vec<u32> { t.into_iter().map(|m| m.unwrap_or(u32::MAX)).collect() };
        let maps = maps.unwrap_or_default();
        if !maps.is_empty() && maps.len() != b.metric_dims.len() {
            return Err(err(format!("番号の対応表の数 {} が軸の数 {} と合わない", maps.len(), b.metric_dims.len())));
        }
        // One map for each packing position (a repeated dimension uses its map at each of its positions)
        let mut slot_maps: Vec<Option<Vec<u32>>> = vec![None; b.pack.dims.len()];
        for (j, t) in maps.into_iter().enumerate() {
            if let Some(t) = t {
                slot_maps[b.pack.slot_of(&b.metric_dims, j).unwrap()] = Some(gone(t));
            }
        }
        Ok(py.detach(move || {
            let Some(a) = a else {
                let (cols, values) = b.rows();
                let old = vec![None; values.len()];
                return Some(Diff { cols, old, new: values.into_iter().map(Some).collect() });
            };
            let cells = a.diff_cells(&b, &slot_maps, value_map.as_deref())?;
            let mut cols = vec![Vec::with_capacity(cells.len()); b.metric_dims.len()];
            let (mut old, mut new) = (Vec::with_capacity(cells.len()), Vec::with_capacity(cells.len()));
            for (k, x, y) in cells {
                for (c, m) in cols.iter_mut().zip(b.decode(k)) {
                    c.push(m);
                }
                old.push(x);
                new.push(y);
            }
            Some(Diff { cols, old, new })
        }))
    }

    /// Write a block of changes to the stored data at one time (journal replay). dim_ids is the table of
    /// position -> member id of each dimension, in the declared order of the dimensions. value_ids is the same
    /// table of the dimension of a member-type value (None for the other kinds). A cell of an id that is not in
    /// the dimension now (a member that the transaction removed) is skipped. A block whose number of dimensions
    /// differs from the Metric is skipped.
    #[pyo3(signature = (store, block, dim_ids, value_ids))]
    fn apply_block(
        &self,
        py: Python<'_>,
        store: &Bound<'_, StoreHandle>,
        block: &CellBlock,
        dim_ids: Vec<Vec<String>>,
        value_ids: Option<Vec<String>>,
    ) -> PyResult<()> {
        let mut s = read(store)?;
        let c = &block.c;
        if c.cols.len() != s.metric_dims.len() || dim_ids.len() != c.cols.len() {
            return Ok(());
        }
        for (&d, table) in s.metric_dims.iter().zip(&dim_ids) {
            let dim = &self.cat.dims[d];
            if table.len() > dim.size as usize {
                return Err(err(format!("軸 {} の ID の表の長さ {} が軸の大きさ {} を超える", dim.name, table.len(), dim.size)));
            }
        }
        if (c.kind == pq::Value::Member) != value_ids.is_some() && c.new.iter().any(Option::is_some) {
            return Err(err("変更の塊の値の種類が Metric の値の種類と合わない".into()));
        }
        let s = py.detach(move || -> Result<Arc<Store>, String> {
            let maps: Vec<Vec<Option<u32>>> = c.ids.iter().zip(&dim_ids).map(|(used, table)| positions(used, table)).collect();
            let vmap = value_ids.as_deref().map(|table| positions(&c.value_ids, table));
            let mut cols: Vec<Vec<u32>> = vec![Vec::with_capacity(c.len()); maps.len()];
            let mut values = Vec::with_capacity(c.len());
            'row: for r in 0..c.len() {
                let mut key = Vec::with_capacity(maps.len());
                for (m, col) in maps.iter().zip(&c.cols) {
                    match m[col[r] as usize] {
                        Some(p) => key.push(p),
                        None => continue 'row,
                    }
                }
                let v = match (c.new[r], &vmap) {
                    (Some(v), Some(vm)) => Some(vm[v as usize].ok_or_else(|| format!("値のメンバーの ID {} が軸にない", c.value_id(v)))? as f64),
                    (v, _) => v,
                };
                for (col, p) in cols.iter_mut().zip(key) {
                    col.push(p);
                }
                values.push(v);
            }
            let slices: Vec<&[u32]> = cols.iter().map(|x| x.as_slice()).collect();
            Arc::make_mut(&mut s).write_many(&slices, &values);
            Ok(s)
        });
        put(store, s.map_err(err)?)
    }

    fn size(&self, store: &Bound<'_, StoreHandle>) -> PyResult<usize> {
        Ok(read(store)?.len())
    }

    /// 行数の上限（本体と差分の件数の和。差分の上書きや削除を数え直さないので、差分があっても O(1)）。
    fn size_hint(&self, store: &Bound<'_, StoreHandle>) -> PyResult<usize> {
        Ok(read(store)?.rows_hint())
    }

    /// 格納データが確保しているメモリ（本体の行数、本体、差分の件数、差分、索引。単位はバイト）。
    fn memory(&self, store: &Bound<'_, StoreHandle>) -> PyResult<(usize, usize, usize, usize, usize)> {
        let m = read(store)?.memory();
        Ok((m.rows, m.base, m.delta_rows, m.delta, m.index))
    }

    fn is_bool(&self, store: &Bound<'_, StoreHandle>) -> PyResult<bool> {
        Ok(read(store)?.kind == Kind::Bool)
    }

    fn index_dim(&self, store: &Bound<'_, StoreHandle>) -> PyResult<Option<DimId>> {
        Ok(read(store)?.index_dim())
    }

    fn metric_dims(&self, store: &Bound<'_, StoreHandle>) -> PyResult<Vec<DimId>> {
        Ok(read(store)?.metric_dims.clone())
    }

    fn cube_len(&self, cube: &Bound<'_, CubeHandle>) -> usize {
        cube.get().cube.cells.len()
    }

    /// 式が差分集計の対象なら (集計元の Ref の番号, 対応表の Ref の番号の列, 件数が要るか)。対象でなければ None。
    fn delta_plan(&self, expr: &Expr) -> Option<(usize, Vec<usize>, bool)> {
        plan::delta_pattern(&expr.node)
    }

    /// Make the plan of the incremental recalculation.
    ///
    /// metrics is (formula, incremental aggregation or not, source or not) for each Metric. A formula is (Expr, the
    /// numbers of the Metrics that it reads). For incremental aggregation, this makes the work formulas
    /// (plan::derive_delta). levels is a list of (scan dimension or None, Metric numbers) for each level.
    fn make_plan(&self, metrics: Vec<Bound<'_, PyTuple>>, levels: Vec<Vec<(Option<DimId>, Vec<usize>)>>) -> PyResult<PlanHandle> {
        let mut out = Vec::with_capacity(metrics.len());
        for t in &metrics {
            let f = formula(&t.get_item(0)?)?;
            let wants_delta: bool = t.get_item(1)?.extract()?;
            let delta = match (&f, wants_delta) {
                (Some(f), true) => Some(plan::derive_delta(f).ok_or_else(|| err("差分集計の対象でない式に差分集計を指定した".into()))?),
                _ => None,
            };
            out.push(Metric { formula: f, delta, source: t.get_item(2)?.extract()? });
        }
        let n = out.len();
        check_refs(out.iter().map(|m| &m.formula), n)?;
        let levels = levels
            .into_iter()
            .map(|level| {
                level
                    .into_iter()
                    .map(|(dim, names)| {
                        if names.is_empty() || names.iter().any(|&m| m >= n) {
                            return Err(err(format!("段の Metric の番号 {names:?} が計算計画にない")));
                        }
                        match dim {
                            Some(d) => self.dim(d).map(|_| Step::Scan(d, names)),
                            None => Ok(Step::One(names[0])),
                        }
                    })
                    .collect::<PyResult<_>>()
            })
            .collect::<PyResult<_>>()?;
        Ok(PlanHandle { plan: Arc::new(Plan { metrics: out, levels }) })
    }

    /// 計算計画を作る。formulas は Metric の番号順の (式, 読み出す Metric の番号) で、入力は None。
    /// names と dims はその名前と軸。返すのは、依存先が先の順の段階 (Metric の番号の列, scan の軸) と、
    /// 段ごとの段階の番号の列と、依存グラフの辺。循環の誤りは Diagnostic。
    #[allow(clippy::type_complexity)]
    fn plan(&self, py: Python<'_>, formulas: Vec<Bound<'_, PyAny>>, names: Vec<String>, dims: Vec<Vec<DimId>>) -> PyResult<(Vec<(Vec<usize>, Option<DimId>)>, Vec<Vec<usize>>, graph::Edges)> {
        let formulas: Vec<Option<Formula>> = formulas.iter().map(formula).collect::<PyResult<_>>()?;
        check_refs(formulas.iter(), formulas.len())?;
        if names.len() != formulas.len() || dims.len() != formulas.len() {
            return Err(err("名前と軸の数が式の数と合わない".into()));
        }
        dims.iter().try_for_each(|ds| self.dims(ds))?;
        graph::plan(&self.cat, &formulas, &names, &dims).map_err(|d| diag_err(py, d))
    }

    /// 入力の変更範囲と追加したメンバーを計画の順に伝え、影響を受ける全 Metric の範囲を返す。
    fn propagate(&self, py: Python<'_>, plan: &PlanHandle, changed: Vec<(usize, Region)>, added: Vec<(DimId, Vec<u32>)>) -> PyResult<Vec<(usize, Region)>> {
        let changed: Vec<(usize, Reg)> = changed.into_iter().map(|(m, r)| self.reg(&plan.plan, m, r)).collect::<PyResult<_>>()?;
        self.added(&added)?;
        let (cat, plan) = (self.cat.clone(), plan.plan.clone());
        let regions = py.detach(move || plan::propagate(&cat, &plan, changed, &added));
        Ok(regions.into_iter().enumerate().filter_map(|(m, r)| r.map(|r| (m, r.into_parts()))).collect())
    }

    /// 軸 dim のメンバー member を消すと値が変わる範囲（計算 Metric ごと）。stores は Metric の番号順の格納データ。
    fn removal_regions(&self, py: Python<'_>, plan: &PlanHandle, stores: Vec<Bound<'_, StoreHandle>>, dim: DimId, member: u32) -> PyResult<Vec<(usize, Region)>> {
        self.members(dim, &[member])?;
        if stores.len() != plan.plan.metrics.len() {
            return Err(err(format!("格納データの数 {} が計算計画の Metric の数 {} と合わない", stores.len(), plan.plan.metrics.len())));
        }
        let stores: Vec<Arc<Store>> = stores.iter().map(read).collect::<PyResult<_>>()?;
        let (cat, plan) = (self.cat.clone(), plan.plan.clone());
        let todo = py.detach(move || plan::removal_regions(&cat, &plan, &stores, dim, member));
        Ok(todo.into_iter().map(|(m, r)| (m, r.into_parts())).collect())
    }

    /// 1 つの式の影響範囲。regions は式の Ref の番号ごとの変更範囲（None は変わっていない）。
    #[pyo3(signature = (expr, regions, added, removed = None))]
    fn affected(&self, expr: &Expr, regions: Vec<Option<Region>>, added: Vec<(DimId, Vec<u32>)>, removed: Option<(DimId, u32)>) -> PyResult<Option<Region>> {
        for r in regions.iter().flatten() {
            r.iter().try_for_each(|(d, ms)| self.members(*d, ms))?;
        }
        self.added(&added)?;
        if let Some((d, m)) = removed {
            self.members(d, &[m])?;
        }
        let regions: Vec<Option<Reg>> = regions.into_iter().map(|r| r.map(Reg::new)).collect();
        let refs: Vec<usize> = (0..regions.len()).collect();
        let added: Vec<(DimId, Vec<u32>)> = added.into_iter().map(|(d, mut ms)| { ms.sort_unstable(); ms.dedup(); (d, ms) }).collect();
        let env = RangeEnv { cat: &self.cat, regions: &regions, added: &added, removed };
        Ok(env.affected(&expr.node, &refs).map(|r| r.into_parts()))
    }

    /// Do an incremental recalculation in one call, without the GIL. stores is the stored data in Metric number
    /// order. counts and lows are the counts and the residuals (exact sum minus value, only the cells that are not
    /// 0) of incremental aggregation, in the same order. All three change in place. changed is the changed range
    /// of the inputs, and added is the added members of each dimension. olds is the old values of the inputs
    /// that are a source of incremental aggregation. forced is the range of each formula Metric to calculate
    /// again. Returns each recalculated (Metric, incremental aggregation or not, range). If full is true, it is a
    /// full recalculation, and the levels always run in parallel.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (plan, stores, counts, lows, changed, added, olds, forced, full = false))]
    fn recalc_changes(
        &self,
        py: Python<'_>,
        plan: &PlanHandle,
        stores: Vec<Bound<'_, StoreHandle>>,
        counts: Vec<Option<Bound<'_, StoreHandle>>>,
        lows: Vec<Option<Bound<'_, StoreHandle>>>,
        changed: Vec<(usize, Region)>,
        added: Vec<(DimId, Vec<u32>)>,
        olds: Vec<(usize, Bound<'_, PyAny>)>,
        forced: Vec<(usize, Region)>,
        full: bool,
    ) -> PyResult<Vec<(usize, bool, Region)>> {
        let n = plan.plan.metrics.len();
        let olds: Vec<(usize, Src)> = olds
            .iter()
            .map(|(m, o)| if *m < n { Ok((*m, Core::source(o)?)) } else { Err(err(format!("Metric の番号 {m} が計算計画にない"))) })
            .collect::<PyResult<_>>()?;
        let changed: Vec<(usize, Reg)> = changed.into_iter().map(|(m, r)| self.reg(&plan.plan, m, r)).collect::<PyResult<_>>()?;
        let forced: Vec<(usize, Reg)> = forced.into_iter().map(|(m, r)| self.reg(&plan.plan, m, r)).collect::<PyResult<_>>()?;
        self.added(&added)?;
        if stores.len() != n || counts.len() != n || lows.len() != n {
            return Err(err(format!(
                "格納データの数 {}、件数の数 {}、残差の数 {} が計算計画の Metric の数 {n} と合わない",
                stores.len(),
                counts.len(),
                lows.len()
            )));
        }
        // Take the stored data out of the handles and put it back at the end (one more reference makes a copy at
        // a write). Always put it back, also after an error or a panic. Formula Metrics can then be half written,
        // so the caller does a full recalculation next. The inputs do not change, so they stay in the handles.
        // Keep the handles borrowed until the end, so other threads cannot touch them
        let empty = Arc::new(Store::new(&[], None, Kind::Num, &self.cat).map_err(err)?);
        let mut guard = Returned { stores: Vec::new(), counts: Vec::new(), own: Vec::new(), own_counts: Vec::new() };
        for (h, m) in stores.iter().zip(&plan.plan.metrics) {
            let mut h = h.try_borrow_mut().map_err(busy)?;
            guard.own.push(if m.formula.is_some() { std::mem::replace(&mut h.store, empty.clone()) } else { h.store.clone() });
            guard.stores.push(h);
        }
        // The counts and then the lows, in one list: own_counts[..n] are the counts, own_counts[n..] the lows
        for h in counts.iter().chain(&lows) {
            let mut h = h.as_ref().map(|h| h.try_borrow_mut().map_err(busy)).transpose()?;
            guard.own_counts.push(h.as_mut().map(|h| std::mem::replace(&mut h.store, empty.clone())));
            guard.counts.push(h);
        }
        let (cat, plan) = (self.cat.clone(), plan.plan.clone());
        let (own, (own_counts, own_lows)) = (&mut guard.own, guard.own_counts.split_at_mut(n));
        let result = py.detach(|| {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                plan::recalc(&cat, &plan, own, own_counts, own_lows, changed, &added, olds, forced, full)
            }))
        });
        drop(guard);
        let result = result.map_err(|p| PyRuntimeError::new_err(format!("再計算の内部エラー: {}", panic_message(&p))))?;
        let log = result.map_err(err)?;
        Ok(log.into_iter().map(|(m, delta, r)| (m, delta, r.into_parts())).collect())
    }
}

/// recalc_changes が取り出した格納データを、抜けるときに（panic でも）借りたままのハンドルへ戻す。
struct Returned<'py> {
    stores: Vec<PyRefMut<'py, StoreHandle>>,
    counts: Vec<Option<PyRefMut<'py, StoreHandle>>>,
    own: Vec<Arc<Store>>,
    own_counts: Vec<Option<Arc<Store>>>,
}

impl Drop for Returned<'_> {
    fn drop(&mut self) {
        for (h, s) in self.stores.iter_mut().zip(self.own.drain(..)) {
            h.store = s;
        }
        for (h, s) in self.counts.iter_mut().zip(self.own_counts.drain(..)) {
            if let (Some(h), Some(s)) = (h, s) {
                h.store = s;
            }
        }
    }
}

fn busy<E>(_: E) -> PyErr {
    PyRuntimeError::new_err("格納データを別のスレッドが使用中（同じモデルを複数のスレッドから書き換えない）")
}

fn panic_message(p: &Box<dyn std::any::Any + Send>) -> String {
    p.downcast_ref::<&str>().map(|s| s.to_string()).or_else(|| p.downcast_ref::<String>().cloned()).unwrap_or_else(|| "不明".into())
}

// ------------------------------------------------------------------ ヒープの計測

struct Counting;

const SLOTS: usize = 64;
// The change that a slot keeps before it goes to the total. heap_exact(True) sets 0, so the peak is exact.
static SLOT_FLUSH: AtomicUsize = AtomicUsize::new(64 << 10);

#[repr(align(128))] // 128, not 64: the adjacent-line prefetch moves cache lines in pairs
struct Slot(AtomicIsize);

static HEAP_SLOTS: [Slot; SLOTS] = [const { Slot(AtomicIsize::new(0)) }; SLOTS];
static HEAP_FLUSHED: AtomicIsize = AtomicIsize::new(0);
static HEAP_PEAK: AtomicIsize = AtomicIsize::new(0);
static NEXT_SLOT: AtomicUsize = AtomicUsize::new(0);

thread_local! {
    // No destructor: the allocator must not use a thread local with a destructor.
    static MY_SLOT: Cell<usize> = const { Cell::new(usize::MAX) };
}

fn heap_flush(v: isize) {
    let now = HEAP_FLUSHED.fetch_add(v, Ordering::Relaxed) + v;
    HEAP_PEAK.fetch_max(now, Ordering::Relaxed);
}

fn heap_add(n: isize) {
    // A large change skips the slot, so the value in transit stays below about 2 * SLOT_FLUSH.
    let flush = SLOT_FLUSH.load(Ordering::Relaxed);
    if n.unsigned_abs() > flush {
        return heap_flush(n);
    }
    let i = MY_SLOT.with(|c| {
        if c.get() == usize::MAX {
            c.set(NEXT_SLOT.fetch_add(1, Ordering::Relaxed) % SLOTS);
        }
        c.get()
    });
    let slot = &HEAP_SLOTS[i].0;
    let v = slot.fetch_add(n, Ordering::Relaxed) + n;
    if v.unsigned_abs() > flush {
        // swap, not fetch_sub: after more than SLOTS threads, two threads can share a slot.
        heap_flush(slot.swap(0, Ordering::Relaxed));
    }
}

fn heap_now() -> isize {
    HEAP_FLUSHED.load(Ordering::Relaxed) + HEAP_SLOTS.iter().map(|s| s.0.load(Ordering::Relaxed)).sum::<isize>()
}

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let p = unsafe { System.alloc(layout) };
        if !p.is_null() {
            heap_add(layout.size() as isize);
        }
        p
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let p = unsafe { System.alloc_zeroed(layout) };
        if !p.is_null() {
            heap_add(layout.size() as isize);
        }
        p
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) };
        heap_add(-(layout.size() as isize));
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let p = unsafe { System.realloc(ptr, layout, new_size) };
        if !p.is_null() {
            heap_add(new_size as isize - layout.size() as isize);
        }
        p
    }
}

#[global_allocator]
static ALLOC: Counting = Counting;

/// If `exact`, count each allocation in the total at once. Then the maximum of `heap` is exact, but allocation is
/// slower. Tests use it to compare a peak with a budget of a few KiB.
#[pyfunction]
fn heap_exact(exact: bool) {
    SLOT_FLUSH.store(if exact { 0 } else { 64 << 10 }, Ordering::Relaxed);
    for s in &HEAP_SLOTS {
        heap_flush(s.0.swap(0, Ordering::Relaxed));
    }
}

/// The memory that Rust allocated after the process started, and the maximum after reset_heap_peak (bytes).
/// The maximum can be wrong by about 128 KiB for each thread that allocated.
#[pyfunction]
fn heap() -> (usize, usize) {
    let now = heap_now();
    (now.max(0) as usize, HEAP_PEAK.load(Ordering::Relaxed).max(now).max(0) as usize)
}

/// Set the maximum of the heap to the current value. To measure the maximum during an operation, call this before it.
#[pyfunction]
fn reset_heap_peak() {
    HEAP_PEAK.store(heap_now(), Ordering::Relaxed);
}

/// 2 つの格納データの差（宣言した軸の順のメンバー番号の列と、変更前後の値）。Python のオブジェクトにはしない。
#[pyclass(frozen)]
struct Diff {
    cols: Vec<Vec<u32>>,
    old: Vec<Option<f64>>,
    new: Vec<Option<f64>>,
}

#[pymethods]
impl Diff {
    fn __len__(&self) -> usize {
        self.new.len()
    }

    /// (メンバー番号の列, 変更前, 変更後) の列。
    #[allow(clippy::type_complexity)]
    fn rows(&self) -> Vec<(Vec<u32>, Option<f64>, Option<f64>)> {
        (0..self.new.len()).map(|r| (self.cols.iter().map(|c| c[r]).collect(), self.old[r], self.new[r])).collect()
    }

    /// Make the block of changes, with member ids in place of the member numbers. dim_ids is the table of
    /// position -> member id of each dimension, value_ids the table of the dimension of a member-type value
    /// (None for the other kinds), value the kind of the value (number, boolean, member). The block keeps only
    /// the ids that it uses.
    #[pyo3(signature = (dim_ids, value_ids, value))]
    fn to_block(&self, py: Python<'_>, dim_ids: Vec<Vec<String>>, value_ids: Option<Vec<String>>, value: &str) -> PyResult<CellBlock> {
        let kind = pq::Value::parse(value).map_err(err)?;
        if dim_ids.len() != self.cols.len() {
            return Err(err("軸の表の数が軸の数と合わない".into()));
        }
        let c = py
            .detach(|| -> Result<pq::Changes, String> {
                let (mut ids, mut cols) = (Vec::with_capacity(dim_ids.len()), Vec::with_capacity(dim_ids.len()));
                for (col, table) in self.cols.iter().zip(&dim_ids) {
                    let (used, col) = compact(table, col.iter().copied())?;
                    ids.push(used);
                    cols.push(col);
                }
                let (value_ids, old, new) = match &value_ids {
                    None => (Vec::new(), self.old.clone(), self.new.clone()),
                    Some(table) => {
                        let present = self.old.iter().chain(&self.new).flatten().map(|&x| x as u32);
                        let (used, index) = compact(table, present)?;
                        let mut next = index.into_iter();
                        let mut conv = |xs: &[Option<f64>]| xs.iter().map(|v| v.map(|_| next.next().unwrap() as f64)).collect::<Vec<_>>();
                        let (old, new) = (conv(&self.old), conv(&self.new));
                        (used, old, new)
                    }
                };
                Ok(pq::Changes { cols, ids, old, new, value_ids, kind })
            })
            .map_err(err)?;
        Ok(CellBlock::new(c))
    }
}

/// Keep only the ids of table that positions use: (the used ids in the order of first use, the index in them of
/// each position). An error names a position that the table does not have.
fn compact(table: &[String], positions: impl Iterator<Item = u32>) -> Result<(Vec<String>, Vec<u32>), String> {
    let mut remap = vec![u32::MAX; table.len()];
    let (mut used, mut out) = (Vec::new(), Vec::new());
    for p in positions {
        let slot = remap.get_mut(p as usize).ok_or_else(|| format!("メンバー番号 {p} の ID がない"))?;
        if *slot == u32::MAX {
            *slot = used.len() as u32;
            used.push(table[p as usize].clone());
        }
        out.push(*slot);
    }
    Ok((used, out))
}

/// The position in table of each id of used (None if table does not have the id).
fn positions(used: &[String], table: &[String]) -> Vec<Option<u32>> {
    let index: FxHashMap<&str, u32> = table.iter().enumerate().map(|(p, s)| (s.as_str(), p as u32)).collect();
    used.iter().map(|s| index.get(s.as_str()).copied()).collect()
}

/// The text form of a PostgreSQL array of ids. Each id is quoted, so an id can hold any character.
fn array_literal(ids: &[&str]) -> String {
    let quoted: Vec<String> = ids.iter().map(|s| format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))).collect();
    format!("{{{}}}", quoted.join(","))
}

/// The changed cells of one input Metric as a block (the coordinates are member ids). It goes into the "rows" of
/// a record in place of a list of rows. Like a list of rows, it has a length and gives [the ids of the
/// coordinates, the old value, the new value] in sequence.
#[pyclass(frozen, sequence)]
struct CellBlock {
    c: pq::Changes,
    keys: OnceLock<FxHashSet<Vec<u32>>>, // the coordinates as indexes in the tables of this block (made to look for overlaps)
}

type Indexes<'a> = Vec<FxHashMap<&'a str, u32>>;

impl CellBlock {
    fn new(c: pq::Changes) -> CellBlock {
        CellBlock { c, keys: OnceLock::new() }
    }

    fn key(&self, r: usize) -> Vec<u32> {
        self.c.cols.iter().map(|x| x[r]).collect()
    }

    fn keys(&self) -> &FxHashSet<Vec<u32>> {
        self.keys.get_or_init(|| (0..self.c.len()).map(|r| self.key(r)).collect())
    }

    /// The index of each id in the tables of this block.
    fn indexes(&self) -> Indexes<'_> {
        self.c.ids.iter().map(|t| t.iter().enumerate().map(|(p, s)| (s.as_str(), p as u32)).collect()).collect()
    }

    /// The coordinates (member ids) as indexes in the tables. None if an id is not in this block: no row has the cell.
    fn index_of(indexes: &Indexes<'_>, key: &[String]) -> Option<Vec<u32>> {
        if key.len() != indexes.len() {
            return None;
        }
        key.iter().zip(indexes).map(|(k, ix)| ix.get(k.as_str()).copied()).collect()
    }

    fn value<'py>(&self, py: Python<'py>, v: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        Ok(match (v, self.c.kind) {
            (None, _) => py.None().into_bound(py),
            (Some(x), pq::Value::Num) => x.into_pyobject(py)?.into_any(),
            (Some(x), pq::Value::Bool) => PyBool::new(py, x != 0.0).to_owned().into_any(),
            (Some(x), pq::Value::Member) => PyString::new(py, self.c.value_id(x)).into_any(),
        })
    }
}

fn copy_float(out: &mut String, v: Option<f64>) {
    use std::fmt::Write;
    match v {
        None => out.push_str("\\N"),
        Some(x) if x.is_nan() => out.push_str("NaN"),
        Some(x) if x.is_infinite() => out.push_str(if x > 0.0 { "Infinity" } else { "-Infinity" }),
        Some(x) => write!(out, "{x:?}").unwrap(),
    }
}

fn copy_escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('\t', "\\t").replace('\n', "\\n").replace('\r', "\\r")
}

#[pymethods]
impl CellBlock {
    fn __len__(&self) -> usize {
        self.c.len()
    }

    /// The number of dimensions of the coordinates.
    #[getter]
    fn width(&self) -> usize {
        self.c.cols.len()
    }

    /// The rows [the ids of the coordinates, the old value, the new value] (a member-type value is an id, a
    /// boolean is bool).
    fn rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let rows = PyList::empty(py);
        for r in 0..self.c.len() {
            let row = PyList::new(py, [
                PyList::new(py, self.c.key(r))?.into_any(),
                self.value(py, self.c.old[r])?,
                self.value(py, self.c.new[r])?,
            ])?;
            rows.append(row)?;
        }
        Ok(rows)
    }

    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        self.rows(py)?.try_iter()
    }

    fn __eq__(&self, py: Python<'_>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        let rows = self.rows(py)?;
        if let Ok(b) = other.cast::<CellBlock>() {
            return rows.eq(b.get().rows(py)?);
        }
        rows.eq(other)
    }

    /// The [old value, new value] of the rows with the coordinates key (the ids). A history lookup does not make
    /// all rows Python objects.
    fn find<'py>(&self, py: Python<'py>, key: Vec<String>) -> PyResult<Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> {
        let rows: Vec<usize> = py.detach(|| {
            let Some(k) = Self::index_of(&self.indexes(), &key) else { return Vec::new() };
            (0..self.c.len()).filter(|&r| self.c.cols.iter().zip(&k).all(|(c, k)| c[r] == *k)).collect()
        });
        rows.into_iter().map(|r| Ok((self.value(py, self.c.old[r])?, self.value(py, self.c.new[r])?))).collect()
    }

    /// Tell if the block has one of the cells keys (lists of coordinate ids).
    fn contains_any(&self, py: Python<'_>, keys: Vec<Vec<String>>) -> bool {
        py.detach(|| {
            let (ix, set) = (self.indexes(), self.keys());
            keys.iter().any(|k| Self::index_of(&ix, k).is_some_and(|k| set.contains(&k)))
        })
    }

    /// Tell if the block has a cell that other also has.
    fn overlaps(&self, py: Python<'_>, other: &CellBlock) -> bool {
        py.detach(|| {
            let (small, large) = if self.c.len() <= other.c.len() { (self, other) } else { (other, self) };
            if small.c.cols.len() != large.c.cols.len() {
                return false;
            }
            let set = large.keys();
            let maps: Vec<Vec<Option<u32>>> = small.c.ids.iter().zip(&large.c.ids).map(|(used, table)| positions(used, table)).collect();
            (0..small.c.len()).any(|r| {
                let key: Option<Vec<u32>> = small.c.cols.iter().zip(&maps).map(|(c, m)| m[c[r] as usize]).collect();
                key.is_some_and(|k| set.contains(&k))
            })
        })
    }

    /// Write the block to Parquet bytes (the columns are the dimensions names, old and new).
    #[pyo3(signature = (names, meta))]
    fn to_parquet<'py>(&self, py: Python<'py>, names: Vec<String>, meta: Vec<(String, String)>) -> PyResult<Bound<'py, PyBytes>> {
        let buf = py.detach(|| pq::write_changes(&names, &self.c, &meta)).map_err(err)?;
        Ok(PyBytes::new(py, &buf))
    }

    /// Make the block from bytes that to_parquet wrote.
    #[staticmethod]
    fn from_parquet(py: Python<'_>, data: PyBackedBytes) -> PyResult<CellBlock> {
        let (_, c) = py.detach(move || pq::read_changes(Bytes::from_owner(data))).map_err(err)?;
        Ok(CellBlock::new(c))
    }

    /// Make the block from rows [the ids of the coordinates, the old value, the new value]. The kind of the value
    /// is member if the values are ids (strings), boolean if they are all bool, and number otherwise.
    #[staticmethod]
    fn from_rows(rows: &Bound<'_, PyAny>) -> PyResult<CellBlock> {
        let mut tables: Vec<pq::Interner> = Vec::new();
        let mut cols: Vec<Vec<u32>> = Vec::new();
        let mut values = pq::Interner::default();
        let (mut old, mut new) = (Vec::new(), Vec::new());
        let (mut floats, mut bools, mut strs) = (false, false, false);
        let mut value = |v: Bound<'_, PyAny>| -> PyResult<Option<f64>> {
            if v.is_none() {
                Ok(None)
            } else if let Ok(b) = v.cast::<PyBool>() {
                bools = true;
                Ok(Some(if b.is_true() { 1.0 } else { 0.0 }))
            } else if let Ok(s) = v.cast::<PyString>() {
                strs = true;
                Ok(Some(values.intern(s.to_str()?) as f64))
            } else {
                floats = true;
                Ok(Some(v.extract::<f64>()?))
            }
        };
        for (r, row) in rows.try_iter()?.enumerate() {
            let row = row?;
            let key: Vec<String> = row.get_item(0)?.extract()?;
            if r == 0 {
                tables = key.iter().map(|_| pq::Interner::default()).collect();
                cols = vec![Vec::new(); key.len()];
            } else if key.len() != cols.len() {
                return Err(err("行ごとに軸の数が違う".into()));
            }
            for ((col, t), k) in cols.iter_mut().zip(&mut tables).zip(&key) {
                col.push(t.intern(k));
            }
            old.push(value(row.get_item(1)?)?);
            new.push(value(row.get_item(2)?)?);
        }
        if strs && (floats || bools) {
            return Err(err("値の種類が混ざっている（メンバーの ID と、数か真偽値）".into()));
        }
        let kind = if strs {
            pq::Value::Member
        } else if bools && !floats {
            pq::Value::Bool
        } else {
            pq::Value::Num
        };
        let ids = tables.into_iter().map(|t| t.table).collect();
        Ok(CellBlock::new(pq::Changes { cols, ids, old, new, value_ids: values.table, kind }))
    }

    /// The rows for the PostgreSQL COPY (text format): model_id, seq, metric, the coordinates (an array of ids),
    /// old_value and new_value (numbers; null for a member type), old_member and new_member (the ids of a
    /// member-type value; null for the other kinds).
    fn copy_text<'py>(&self, py: Python<'py>, model_id: &str, seq: i64, metric: &str) -> Bound<'py, PyBytes> {
        let text = py.detach(|| {
            let prefix = format!("{}\t{seq}\t{}\t", copy_escape(model_id), copy_escape(metric));
            let mut out = String::with_capacity(self.c.len() * (prefix.len() + 40 * (self.c.cols.len() + 1)));
            let member = |out: &mut String, v: Option<f64>| match v {
                None => out.push_str("\\N"),
                Some(x) => out.push_str(&copy_escape(self.c.value_id(x))),
            };
            for r in 0..self.c.len() {
                out.push_str(&prefix);
                out.push_str(&copy_escape(&array_literal(&self.c.key(r))));
                out.push('\t');
                if self.c.kind == pq::Value::Member {
                    out.push_str("\\N\t\\N\t");
                    member(&mut out, self.c.old[r]);
                    out.push('\t');
                    member(&mut out, self.c.new[r]);
                } else {
                    copy_float(&mut out, self.c.old[r]);
                    out.push('\t');
                    copy_float(&mut out, self.c.new[r]);
                    out.push_str("\t\\N\t\\N");
                }
                out.push('\n');
            }
            out
        });
        PyBytes::new(py, text.as_bytes())
    }
}

/// 軸ごとのメンバー番号の列と値の列を Parquet のバイト列にする（格納データを持たないエンジン用）。
#[pyfunction]
fn write_parquet<'py>(
    py: Python<'py>,
    names: Vec<String>,
    cols: Vec<Vec<u32>>,
    values: Vec<f64>,
    value: &str,
    meta: Vec<(String, String)>,
) -> PyResult<Bound<'py, PyBytes>> {
    let value = pq::Value::parse(value).map_err(err)?;
    let buf = py.detach(move || pq::write(&names, cols, values, value, &meta)).map_err(err)?;
    Ok(PyBytes::new(py, &buf))
}

/// Reads bytes that write_parquet wrote into (columns, values). `sizes` has the member count of each dimension.
/// For a member value, the last item is the member count of the value dimension.
#[pyfunction]
fn read_parquet(
    py: Python<'_>,
    data: PyBackedBytes,
    names: Vec<String>,
    value: &str,
    sizes: Vec<u32>,
) -> PyResult<(Vec<Vec<u32>>, Vec<f64>)> {
    let value = pq::Value::parse(value).map_err(err)?;
    py.detach(move || pq::read(Bytes::from_owner(data), &names, value, &sizes)).map_err(err)
}

/// The Arrow IPC stream of a POST /query result (ipc::query_stream). Each part is (Metric id, rows, at):
/// rows are the cached rows of a summary ([member id..., value]), and at[j] is the row position of dimension j
/// of the query, or None if the Metric does not have dimension j.
#[pyfunction]
fn query_arrow<'py>(
    py: Python<'py>,
    seq: u64,
    dims: Vec<String>,
    parts: Vec<(String, Bound<'py, PyList>, Vec<Option<usize>>)>,
) -> PyResult<Bound<'py, PyBytes>> {
    let parts = parts
        .into_iter()
        .map(|(metric, rows, at)| {
            let rows = rows.iter().map(|r| r.cast_into::<PyList>().map_err(PyErr::from)).collect::<PyResult<Vec<_>>>()?;
            let column = |i: usize| rows.iter().map(|r| r.get_item(i)?.extract::<PyBackedStr>()).collect::<PyResult<Vec<_>>>();
            let coords = at.iter().map(|a| a.map(column).transpose()).collect::<PyResult<Vec<_>>>()?;
            let values = rows.iter().map(|r| r.get_item(r.len().saturating_sub(1))?.extract::<f64>()).collect::<PyResult<Vec<_>>>()?;
            Ok(ipc::Part { metric, coords, values })
        })
        .collect::<PyResult<Vec<_>>>()?;
    // Borrow parts, so that they drop after detach. A drop without the thread state puts each decref in a global pool.
    let buf = py.detach(|| ipc::query_stream(seq, &dims, &parts)).map_err(PyRuntimeError::new_err)?;
    Ok(PyBytes::new(py, &buf))
}

/// Parse the SQL text of POST /sql (sql::parse). Returns (select, filters, group_by, order_by, limit):
///   select:   [("dim", name, alias | None) | (agg, metric name, alias | None)], agg is "sum", "avg", ...
///   filters:  [(dimension name, [member names])]
///   group_by: [name | position]
///   order_by: [(name | position, desc, nulls_first)]
/// Err: Diagnostic with a code of sql::parse.
#[allow(clippy::type_complexity)]
#[pyfunction]
fn parse_sql<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyTuple>> {
    let q = sql::parse(text).map_err(|d| diag_err(py, d))?;
    let reference = |r: sql::Ref| -> PyResult<Bound<'py, PyAny>> {
        match r {
            sql::Ref::Name(n) => Ok(n.into_pyobject(py)?.into_any()),
            sql::Ref::Pos(p) => Ok(p.into_pyobject(py)?.into_any()),
        }
    };
    let select = q
        .select
        .into_iter()
        .map(|i| match i {
            sql::Item::Dim { name, alias } => ("dim", name, alias).into_pyobject(py),
            sql::Item::Agg { agg, metric, alias } => (agg.name(), metric, alias).into_pyobject(py),
        })
        .collect::<PyResult<Vec<_>>>()?;
    let group_by = q.group_by.into_iter().map(reference).collect::<PyResult<Vec<_>>>()?;
    let order_by = q.order_by.into_iter().map(|(r, desc, nulls)| (reference(r)?, desc, nulls).into_pyobject(py)).collect::<PyResult<Vec<_>>>()?;
    (select, q.filters, group_by, order_by, q.limit).into_pyobject(py)
}

/// Parquet のフッターのキーと値（本体は読まない）。
#[pyfunction]
fn parquet_metadata(py: Python<'_>, data: PyBackedBytes) -> PyResult<Vec<(String, String)>> {
    py.detach(move || pq::metadata(Bytes::from_owner(data))).map_err(err)
}

#[pymodule(gil_used = false)] // free-threaded の Python でも GIL を有効に戻さない（格納データは Arc と永続的な木で共有する）
fn nanashi_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(heap, m)?)?;
    m.add_function(wrap_pyfunction!(reset_heap_peak, m)?)?;
    m.add_function(wrap_pyfunction!(heap_exact, m)?)?;
    m.add_function(wrap_pyfunction!(write_parquet, m)?)?;
    m.add_function(wrap_pyfunction!(read_parquet, m)?)?;
    m.add_function(wrap_pyfunction!(parquet_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(query_arrow, m)?)?;
    m.add_function(wrap_pyfunction!(parse_sql, m)?)?;
    m.add("Diagnostic", m.py().get_type::<Diagnostic>())?;
    m.add_class::<Core>()?;
    m.add_class::<Expr>()?;
    m.add_class::<CubeHandle>()?;
    m.add_class::<StoreHandle>()?;
    m.add_class::<PlanHandle>()?;
    m.add_class::<Diff>()?;
    m.add_class::<CellBlock>()?;
    Ok(())
}
