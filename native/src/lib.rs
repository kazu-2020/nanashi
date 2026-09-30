//! Python から使う入口。Metric の格納データ（Store）と評価の途中結果（Cube）は Rust 側に置き、
//! Python には中身を持たないハンドルだけを渡す。

use nanashi_engine::check::{self, Env, TKind, Ty};
use nanashi_engine::plan::{self, Env as RangeEnv, Formula, Metric, Plan, Reg, Step};
use nanashi_engine::{eval, graph, pq, Agg, Catalog, Cube, DimId, DimInfo, Kind, Mapping, Node, Op, Restrict, Sel, Src, Store};
use bytes::Bytes;
use numpy::PyReadonlyArray1;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::{PyBool, PyBytes, PyDict, PyInt, PyIterator, PyList, PyTuple};
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::{Arc, OnceLock};

fn err(msg: String) -> PyErr {
    PyValueError::new_err(msg)
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

    fn added(&self, added: &[(DimId, Vec<u32>)]) -> PyResult<()> {
        added.iter().try_for_each(|(d, ms)| self.members(*d, ms))
    }

    /// 計算計画の Metric の番号 m の範囲。
    fn reg(&self, plan: &Plan, m: usize, r: Region) -> PyResult<(usize, Reg)> {
        if m >= plan.metrics.len() {
            return Err(err(format!("Metric の番号 {m} が計算計画にない")));
        }
        r.iter().try_for_each(|(d, ms)| self.members(*d, ms))?;
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

    /// 調整値を変える。評価と再計算の段取りにはすぐ効き、格納データの持ち方（差分をまとめ直す件数、
    /// 索引を作る行数、並列にする件数）は、このあと作る格納データから効く。fail_at はテスト用で、
    /// (Metric の番号, panic させるか) か None。
    #[pyo3(signature = (**config))]
    fn configure(&mut self, config: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        let Some(config) = config else { return Ok(()) };
        let cfg = &mut Arc::make_mut(&mut self.cat).cfg;
        for (k, v) in config.iter() {
            let k: String = k.extract()?;
            match k.as_str() {
                "compact_min" => cfg.compact_min = v.extract()?,
                "par_min" => cfg.par_min = v.extract()?,
                "postings_min_rows" => cfg.postings_min_rows = v.extract()?,
                "stream_always" => cfg.stream_always = v.extract()?,
                "semi_max" => cfg.semi_max = v.extract()?,
                "widen_min_rows" => cfg.widen_min_rows = v.extract()?,
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

    /// 格納データから軸 dim のメンバー m を消し、後ろの番号を詰める（values なら値の番号も）。
    fn remove_member(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, dim: DimId, m: u32, values: bool) -> PyResult<()> {
        self.dim(dim)?; // 軸は先に縮めてあるので、m は今の大きさと等しくてよい（範囲の外は何も消さない）
        let mut handle = store.try_borrow_mut().map_err(busy)?;
        let target = Arc::make_mut(&mut handle.store);
        py.detach(|| target.remove_member(dim, m, values));
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
    /// 返すのは (変換した式, 軸の番号の列, 種類, メンバー型の軸の番号, 警告)。型の誤りは ValueError
    /// （文言は Python の参照実装と同じ）。
    #[allow(clippy::type_complexity)]
    fn compile(
        &self,
        tree: &Bound<'_, PyAny>,
        names: Vec<String>,
        types: Vec<(Vec<DimId>, String, i64)>,
    ) -> PyResult<(Expr, Vec<DimId>, String, i64, Vec<String>)> {
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
        let ty = check::infer(&mut node, &env, &mut warnings).map_err(err)?;
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
        let cubes: Vec<nanashi_engine::Result<Cube>> = py.detach(move || jobs.par_iter().map(|(n, s, r)| eval(n, &cat, s, r)).collect());
        cubes.into_iter().map(|c| Ok(CubeHandle { cube: Arc::new(c.map_err(err)?) })).collect()
    }

    /// 格納データから region の範囲を切り出した、新しい格納データ（GIL を外して読む）。
    fn filter(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, region: Region) -> PyResult<StoreHandle> {
        let (s, r) = (read(store)?, self.restrict(&region)?);
        Ok(StoreHandle { store: Arc::new(py.detach(move || s.slice(&r))) })
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

    /// region の範囲の行を宣言した軸の順のメンバー順に並べ、offset 件目から limit 件だけ返す
    /// （軸ごとのメンバー番号の列、値の列、真偽値か、範囲の全行数）。表示やページングに使う。
    #[pyo3(signature = (store, region, offset = 0, limit = None))]
    fn rows_in(
        &self,
        py: Python<'_>,
        store: &Bound<'_, StoreHandle>,
        region: Region,
        offset: usize,
        limit: Option<usize>,
    ) -> PyResult<Page> {
        let (s, r) = (read(store)?, self.restrict(&region)?);
        let is_bool = s.kind == Kind::Bool;
        let (cols, values, total) = py.detach(move || s.rows_in(&r, offset, limit));
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

    /// store_to_parquet で書いたバイト列から格納データを作る。GIL を外して行う。
    #[allow(clippy::wrong_self_convention)] // Python から呼ぶ名前を保つ
    #[pyo3(signature = (data, dims, index, value, names))]
    fn store_from_parquet(
        &self,
        py: Python<'_>,
        data: PyBackedBytes,
        dims: Vec<DimId>,
        index: Option<DimId>,
        value: &str,
        names: Vec<String>,
    ) -> PyResult<StoreHandle> {
        let value = pq::Value::parse(value).map_err(err)?;
        self.dims(&dims)?;
        let cat = self.cat.clone();
        let store = py
            .detach(move || {
                let sizes: Vec<u32> = dims.iter().map(|d| cat.dims[*d].size).collect();
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

    /// old（変更前）と new（変更後）で値が違うセル。old が None なら new の全セル（変更前はすべて空）。
    /// キーの詰め方が違えば None（呼び出し側が別の方法で比べる）。GIL を外して行う。
    #[pyo3(signature = (old, new))]
    fn diff_block(&self, py: Python<'_>, old: Option<&Bound<'_, StoreHandle>>, new: &Bound<'_, StoreHandle>) -> PyResult<Option<Diff>> {
        let a = old.map(read).transpose()?;
        let b = read(new)?;
        Ok(py.detach(move || {
            let Some(a) = a else {
                let (cols, values) = b.rows();
                let old = vec![None; values.len()];
                return Some(Diff { cols, old, new: values.into_iter().map(Some).collect() });
            };
            let cells = a.diff_cells(&b)?;
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

    /// 変更の塊を格納データにまとめて書き込む（記録の再生）。dim_ids は宣言した軸の順に、軸ごとの
    /// メンバーの位置から ID への表。value_ids はメンバー型の値の軸の同じ表（ほかの型なら None）。
    /// 今の軸にない ID のセルは飛ばす（そのトランザクションで消したメンバーのセル）。
    #[pyo3(signature = (store, block, dim_ids, value_ids))]
    fn apply_block(
        &self,
        py: Python<'_>,
        store: &Bound<'_, StoreHandle>,
        block: &CellBlock,
        dim_ids: Vec<Vec<i64>>,
        value_ids: Option<Vec<i64>>,
    ) -> PyResult<()> {
        let mut s = read(store)?;
        let c = &block.c;
        if c.ids.len() != s.metric_dims.len() || dim_ids.len() != c.ids.len() {
            return Ok(()); // 軸の数が違う（Metric を定義し直した）。名前で比べる経路と同じく飛ばす
        }
        let s = py.detach(move || -> Result<Arc<Store>, String> {
            let pos = |ids: &[i64]| -> FxHashMap<i64, u32> { ids.iter().enumerate().map(|(p, &i)| (i, p as u32)).collect() };
            let maps: Vec<FxHashMap<i64, u32>> = dim_ids.iter().map(|x| pos(x)).collect();
            let vmap = value_ids.as_deref().map(pos);
            let mut cols: Vec<Vec<u32>> = vec![Vec::with_capacity(c.len()); maps.len()];
            let mut values = Vec::with_capacity(c.len());
            'row: for r in 0..c.len() {
                let mut key = Vec::with_capacity(maps.len());
                for (m, ids) in maps.iter().zip(&c.ids) {
                    match m.get(&ids[r]) {
                        Some(&p) => key.push(p),
                        None => continue 'row,
                    }
                }
                let v = match (c.new[r], &vmap) {
                    (Some(v), Some(vm)) => Some(*vm.get(&(v as i64)).ok_or_else(|| format!("値のメンバーの ID {} が軸にない", v as i64))? as f64),
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

    /// 差分集計する SUM の、各グループの件数を求める式（一番内側の集計を COUNT にしたもの）。読み出し元は同じ。
    fn count_formula(&self, expr: &Expr) -> Expr {
        Expr { node: Arc::new(plan::inner_count(&expr.node)), refs: expr.refs }
    }

    /// 差分再計算の計算計画を作る。
    ///
    /// metrics は Metric ごとの (式, 差分集計するか, 集計元か)。式は (Expr, 読み出す Metric の番号) で、
    /// 差分集計する Metric は、件数の式と差分の式をここで作る。levels は依存の段ごとの
    /// (scan の軸または None, Metric の番号) の並び。
    fn make_plan(&self, metrics: Vec<Bound<'_, PyTuple>>, levels: Vec<Vec<(Option<DimId>, Vec<usize>)>>) -> PyResult<PlanHandle> {
        let mut out = Vec::with_capacity(metrics.len());
        for t in &metrics {
            let f = formula(&t.get_item(0)?)?;
            let wants_delta: bool = t.get_item(1)?.extract()?;
            let (delta, count) = match (&f, wants_delta) {
                (Some(f), true) => match plan::derive_delta(f) {
                    Some((d, c)) => (Some(d), c),
                    None => return Err(err("差分集計の対象でない式に差分集計を指定した".into())),
                },
                _ => (None, None),
            };
            out.push(Metric { formula: f, count, delta, source: t.get_item(2)?.extract()? });
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
    /// 段ごとの段階の番号の列と、依存グラフの辺。循環の誤りは ValueError（文言は Python の参照実装と同じ）。
    #[allow(clippy::type_complexity)]
    fn plan(&self, formulas: Vec<Bound<'_, PyAny>>, names: Vec<String>, dims: Vec<Vec<DimId>>) -> PyResult<(Vec<(Vec<usize>, Option<DimId>)>, Vec<Vec<usize>>, graph::Edges)> {
        let formulas: Vec<Option<Formula>> = formulas.iter().map(formula).collect::<PyResult<_>>()?;
        check_refs(formulas.iter(), formulas.len())?;
        if names.len() != formulas.len() || dims.len() != formulas.len() {
            return Err(err("名前と軸の数が式の数と合わない".into()));
        }
        dims.iter().try_for_each(|ds| self.dims(ds))?;
        graph::plan(&self.cat, &formulas, &names, &dims).map_err(err)
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

    /// 差分再計算を 1 回の呼び出しで行う（GIL を外して）。stores と counts は Metric の番号順の
    /// 格納データで、その場で書き換える。changed は入力の変更範囲、added は軸ごとの追加したメンバー、
    /// olds は差分集計の集計元になる入力の変更前の値、forced は必ず計算し直す計算 Metric の範囲。
    /// 再計算した (Metric, 差分集計か, 範囲) を返す。full なら全体の再計算で、段を必ず並列に計算する。
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (plan, stores, counts, changed, added, olds, forced, full = false))]
    fn recalc_changes(
        &self,
        py: Python<'_>,
        plan: &PlanHandle,
        stores: Vec<Bound<'_, StoreHandle>>,
        counts: Vec<Option<Bound<'_, StoreHandle>>>,
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
        if stores.len() != n || counts.len() != n {
            return Err(err(format!("格納データの数 {} が計算計画の Metric の数 {n} と合わない", stores.len())));
        }
        // 格納データをハンドルから取り出して渡し、終わったら戻す（参照を増やすと書き込み時に複製されるため）。
        // 失敗しても panic しても必ず戻す（途中まで書いた計算 Metric が残るので、呼び出し側は次に全体を
        // 計算し直す）。入力の格納データは書き換えないので取り出さずに渡す。終わるまでハンドルを借りたままに
        // して、ほかのスレッドが途中で触れないようにする
        let empty = Arc::new(Store::new(&[], None, Kind::Num, &self.cat).map_err(err)?);
        let mut guard = Returned { stores: Vec::new(), counts: Vec::new(), own: Vec::new(), own_counts: Vec::new() };
        for (h, m) in stores.iter().zip(&plan.plan.metrics) {
            let mut h = h.try_borrow_mut().map_err(busy)?;
            guard.own.push(if m.formula.is_some() { std::mem::replace(&mut h.store, empty.clone()) } else { h.store.clone() });
            guard.stores.push(h);
        }
        for h in &counts {
            let mut h = h.as_ref().map(|h| h.try_borrow_mut().map_err(busy)).transpose()?;
            guard.own_counts.push(h.as_mut().map(|h| std::mem::replace(&mut h.store, empty.clone())));
            guard.counts.push(h);
        }
        let (cat, plan) = (self.cat.clone(), plan.plan.clone());
        let (own, own_counts) = (&mut guard.own, &mut guard.own_counts);
        let result = py.detach(|| {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                plan::recalc(&cat, &plan, own, own_counts, changed, &added, olds, forced, full)
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

/// Rust の側で確保しているメモリを数えるアロケータ。中身の確保は System に任せる。
/// 数えるのは track_heap(true) の後だけで、止めている間は確保のたびにフラグを 1 回読むだけにする
/// （原子的な加減算を毎回すると、並列の再計算で数 % 遅くなったため）。Python のオブジェクトは数えない。
struct Counting;

static HEAP_TRACK: AtomicBool = AtomicBool::new(false);
static HEAP_NOW: AtomicIsize = AtomicIsize::new(0); // 数え始めてからの確保と解放の差
static HEAP_PEAK: AtomicIsize = AtomicIsize::new(0);

fn heap_add(n: isize) {
    if !HEAP_TRACK.load(Ordering::Relaxed) {
        return;
    }
    let now = HEAP_NOW.fetch_add(n, Ordering::Relaxed) + n;
    if n > 0 && now > HEAP_PEAK.load(Ordering::Relaxed) {
        HEAP_PEAK.fetch_max(now, Ordering::Relaxed);
    }
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

/// ヒープを数えるかを切り替える。数え始めるときは今の量と最大を 0 にする。
/// 数える前に確保したメモリの解放は差し引かれるので、モデルを作る前に数え始める。
#[pyfunction]
fn track_heap(on: bool) {
    if on {
        HEAP_NOW.store(0, Ordering::Relaxed);
        HEAP_PEAK.store(0, Ordering::Relaxed);
    }
    HEAP_TRACK.store(on, Ordering::Relaxed);
}

/// 数え始めてから Rust の側で確保しているメモリと、reset_heap_peak の後の最大（バイト）。
#[pyfunction]
fn heap() -> (usize, usize) {
    (HEAP_NOW.load(Ordering::Relaxed).max(0) as usize, HEAP_PEAK.load(Ordering::Relaxed).max(0) as usize)
}

/// ヒープの最大を今の量に戻す（ある処理の間の最大を測るときに、その前に呼ぶ）。
#[pyfunction]
fn reset_heap_peak() {
    HEAP_PEAK.store(HEAP_NOW.load(Ordering::Relaxed), Ordering::Relaxed);
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

    /// メンバー番号を ID に直した変更の塊にする。dim_ids は軸ごとの位置から ID への表、value_ids は
    /// メンバー型の値の軸の表（ほかの型なら None）、value は値の種類（number、boolean、member）。
    #[pyo3(signature = (dim_ids, value_ids, value))]
    fn to_block(&self, py: Python<'_>, dim_ids: Vec<Vec<i64>>, value_ids: Option<Vec<i64>>, value: &str) -> PyResult<CellBlock> {
        let kind = pq::Change::parse(value).map_err(err)?;
        if dim_ids.len() != self.cols.len() {
            return Err(err("軸の表の数が軸の数と合わない".into()));
        }
        let c = py
            .detach(|| -> Result<pq::Changes, String> {
                let at = |ids: &[i64], p: u32| ids.get(p as usize).copied().ok_or_else(|| format!("メンバー番号 {p} の ID がない"));
                let ids = self
                    .cols
                    .iter()
                    .zip(&dim_ids)
                    .map(|(c, t)| c.iter().map(|&p| at(t, p)).collect::<Result<Vec<i64>, String>>())
                    .collect::<Result<_, _>>()?;
                let conv = |xs: &[Option<f64>]| -> Result<Vec<Option<f64>>, String> {
                    match &value_ids {
                        None => Ok(xs.to_vec()),
                        Some(t) => xs.iter().map(|v| v.map(|x| at(t, x as u32).map(|i| i as f64)).transpose()).collect(),
                    }
                };
                Ok(pq::Changes { ids, old: conv(&self.old)?, new: conv(&self.new)?, kind })
            })
            .map_err(err)?;
        Ok(CellBlock::new(c))
    }
}

/// 1 つの入力 Metric の、書き換えたセルの塊（座標はメンバーの ID）。記録の "rows" に、行の列の
/// 代わりに入る。行の列と同じく、長さを持ち、[座標の ID の列, 変更前, 変更後] を順に返す。
#[pyclass(frozen, sequence)]
struct CellBlock {
    c: pq::Changes,
    keys: OnceLock<FxHashSet<Vec<i64>>>, // 座標の集まり（重なりを調べるときに作る）
}

impl CellBlock {
    fn new(c: pq::Changes) -> CellBlock {
        CellBlock { c, keys: OnceLock::new() }
    }

    fn key(&self, r: usize) -> Vec<i64> {
        self.c.ids.iter().map(|x| x[r]).collect()
    }

    fn keys(&self) -> &FxHashSet<Vec<i64>> {
        self.keys.get_or_init(|| (0..self.c.len()).map(|r| self.key(r)).collect())
    }

    fn value<'py>(&self, py: Python<'py>, v: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        Ok(match (v, self.c.kind) {
            (None, _) => py.None().into_bound(py),
            (Some(x), pq::Change::Num) => x.into_pyobject(py)?.into_any(),
            (Some(x), pq::Change::Bool) => PyBool::new(py, x != 0.0).to_owned().into_any(),
            (Some(x), pq::Change::Int) => (x as i64).into_pyobject(py)?.into_any(),
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

    /// 座標の軸の数。
    #[getter]
    fn width(&self) -> usize {
        self.c.ids.len()
    }

    /// 行の列 [座標の ID の列, 変更前, 変更後]（メンバー型の値は ID、真偽値は bool）。
    fn rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let rows = PyList::empty(py);
        for r in 0..self.c.len() {
            let row = PyList::new(py, [
                PyList::new(py, self.key(r))?.into_any(),
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
        if let Ok(b) = other.cast::<CellBlock>() {
            return Ok(self.c == b.get().c);
        }
        self.rows(py)?.eq(other)
    }

    /// 座標が key（座標の ID の列）の行の [変更前, 変更後] の列（履歴を引くときに、全行を Python にしない）。
    fn find<'py>(&self, py: Python<'py>, key: Vec<i64>) -> PyResult<Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> {
        if key.len() != self.c.ids.len() {
            return Ok(Vec::new());
        }
        let rows: Vec<usize> = py.detach(|| (0..self.c.len()).filter(|&r| self.c.ids.iter().zip(&key).all(|(c, k)| c[r] == *k)).collect());
        rows.into_iter().map(|r| Ok((self.value(py, self.c.old[r])?, self.value(py, self.c.new[r])?))).collect()
    }

    /// keys（座標の ID の列の列）のどれかを含むか。
    fn contains_any(&self, py: Python<'_>, keys: Vec<Vec<i64>>) -> bool {
        py.detach(|| {
            let set = self.keys();
            keys.iter().any(|k| set.contains(k))
        })
    }

    /// other と同じセルを含むか。
    fn overlaps(&self, py: Python<'_>, other: &CellBlock) -> bool {
        py.detach(|| {
            let (small, large) = if self.c.len() <= other.c.len() { (self, other) } else { (other, self) };
            let set = large.keys();
            (0..small.c.len()).any(|r| set.contains(&small.key(r)))
        })
    }

    /// Parquet のバイト列にする（列は names の軸、old、new）。
    #[pyo3(signature = (names, meta))]
    fn to_parquet<'py>(&self, py: Python<'py>, names: Vec<String>, meta: Vec<(String, String)>) -> PyResult<Bound<'py, PyBytes>> {
        let buf = py.detach(|| pq::write_changes(&names, &self.c, &meta)).map_err(err)?;
        Ok(PyBytes::new(py, &buf))
    }

    /// to_parquet で書いたバイト列から作る。
    #[staticmethod]
    fn from_parquet(py: Python<'_>, data: PyBackedBytes) -> PyResult<CellBlock> {
        let (_, c) = py.detach(move || pq::read_changes(Bytes::from_owner(data))).map_err(err)?;
        Ok(CellBlock::new(c))
    }

    /// 行の列 [座標の ID の列, 変更前, 変更後] から作る。値の型は、すべて真偽値なら boolean、
    /// すべて整数ならメンバーの ID、ほかは number とみなす。
    #[staticmethod]
    fn from_rows(rows: &Bound<'_, PyAny>) -> PyResult<CellBlock> {
        let mut ids: Vec<Vec<i64>> = Vec::new();
        let (mut old, mut new) = (Vec::new(), Vec::new());
        let (mut floats, mut bools, mut ints) = (false, false, false);
        let mut value = |v: Bound<'_, PyAny>| -> PyResult<Option<f64>> {
            if v.is_none() {
                Ok(None)
            } else if let Ok(b) = v.cast::<PyBool>() {
                bools = true;
                Ok(Some(if b.is_true() { 1.0 } else { 0.0 }))
            } else if v.cast::<PyInt>().is_ok() {
                ints = true;
                Ok(Some(v.extract::<i64>()? as f64))
            } else {
                floats = true;
                Ok(Some(v.extract::<f64>()?))
            }
        };
        for (r, row) in rows.try_iter()?.enumerate() {
            let row = row?;
            let key: Vec<i64> = row.get_item(0)?.extract()?;
            if r == 0 {
                ids = vec![Vec::new(); key.len()];
            } else if key.len() != ids.len() {
                return Err(err("行ごとに軸の数が違う".into()));
            }
            for (col, k) in ids.iter_mut().zip(key) {
                col.push(k);
            }
            old.push(value(row.get_item(1)?)?);
            new.push(value(row.get_item(2)?)?);
        }
        let kind = if floats || (bools && ints) {
            pq::Change::Num
        } else if bools {
            pq::Change::Bool
        } else if ints {
            pq::Change::Int
        } else {
            pq::Change::Num
        };
        Ok(CellBlock::new(pq::Changes { ids, old, new, kind }))
    }

    /// PostgreSQL の COPY（テキスト形式）の行（model_id、seq、metric、座標の配列、変更前、変更後）。
    fn copy_text<'py>(&self, py: Python<'py>, model_id: &str, seq: i64, metric: i64) -> Bound<'py, PyBytes> {
        let text = py.detach(|| {
            use std::fmt::Write;
            let prefix = format!("{}\t{seq}\t{metric}\t", copy_escape(model_id));
            let mut out = String::with_capacity(self.c.len() * (prefix.len() + 40));
            for r in 0..self.c.len() {
                out.push_str(&prefix);
                out.push('{');
                for (j, x) in self.c.ids.iter().enumerate() {
                    if j > 0 {
                        out.push(',');
                    }
                    write!(out, "{}", x[r]).unwrap();
                }
                out.push_str("}\t");
                copy_float(&mut out, self.c.old[r]);
                out.push('\t');
                copy_float(&mut out, self.c.new[r]);
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

/// write_parquet で書いたバイト列を (列の並び, 値の並び) に戻す。sizes は軸ごとのメンバー数。
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

/// Parquet のフッターのキーと値（本体は読まない）。
#[pyfunction]
fn parquet_metadata(py: Python<'_>, data: PyBackedBytes) -> PyResult<Vec<(String, String)>> {
    py.detach(move || pq::metadata(Bytes::from_owner(data))).map_err(err)
}

#[pymodule(gil_used = false)] // free-threaded の Python でも GIL を有効に戻さない（格納データは Arc と永続的な木で共有する）
fn nanashi_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(track_heap, m)?)?;
    m.add_function(wrap_pyfunction!(heap, m)?)?;
    m.add_function(wrap_pyfunction!(reset_heap_peak, m)?)?;
    m.add_function(wrap_pyfunction!(write_parquet, m)?)?;
    m.add_function(wrap_pyfunction!(read_parquet, m)?)?;
    m.add_function(wrap_pyfunction!(parquet_metadata, m)?)?;
    m.add_class::<Core>()?;
    m.add_class::<Expr>()?;
    m.add_class::<CubeHandle>()?;
    m.add_class::<StoreHandle>()?;
    m.add_class::<PlanHandle>()?;
    m.add_class::<Diff>()?;
    m.add_class::<CellBlock>()?;
    Ok(())
}
