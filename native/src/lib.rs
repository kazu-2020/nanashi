//! Python から使う入口。Metric の格納データ（Store）と評価の途中結果（Cube）は Rust 側に置き、
//! Python には中身を持たないハンドルだけを渡す。

mod core;

use crate::core::{eval, Agg, Catalog, Cube, DimId, DimInfo, Kind, Mapping, Node, Op, Restrict, Sel, Src, Store};
use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use rayon::prelude::*;
use std::sync::Arc;

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

fn mapping(dst_size: u32, fwd: Vec<i64>) -> Mapping {
    let mut inv = vec![Vec::new(); dst_size as usize];
    for (s, &t) in fwd.iter().enumerate() {
        if t >= 0 {
            inv[t as usize].push(s as u32);
        }
    }
    Mapping { fwd, inv }
}

#[pyclass(frozen)]
struct Expr {
    node: Arc<Node>,
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

type Region = Vec<(DimId, Vec<u32>)>;

impl Core {
    fn restrict(&self, region: &Region) -> Restrict {
        let mut r = Restrict::all(self.cat.dims.len());
        for (d, ms) in region {
            r = r.with(*d, Sel::new(ms.clone(), self.cat.dims[*d].size));
        }
        r
    }

    fn source(obj: &Bound<'_, PyAny>) -> PyResult<Src> {
        if let Ok(s) = obj.cast::<StoreHandle>() {
            return Ok(Src::Store(s.borrow().store.clone()));
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
                Node::Bin(op, child(2)?, child(3)?)
            }
            "not" => Node::Not(child(1)?),
            "if" => {
                let e = t.get_item(3)?;
                Node::If(child(1)?, child(2)?, if e.is_none() { None } else { Some(Box::new(self.node(&e)?)) })
            }
            "filter" => Node::Filter(child(1)?, child(2)?),
            "on" => Node::On(child(1)?, child(2)?),
            "coalesce" => Node::Coalesce(child(1)?, child(2)?),
            "expand" => Node::Expand(child(1)?, t.get_item(2)?.extract()?),
            "isblank" => Node::IsBlank(child(1)?),
            "ifblank" => Node::IfBlank(child(1)?, t.get_item(2)?.extract()?),
            "byagg" => Node::ByAgg {
                child: child(1)?,
                src: t.get_item(2)?.extract()?,
                dst: t.get_item(3)?.extract()?,
                map: t.get_item(4)?.extract()?,
                agg: agg(t.get_item(5)?.extract()?)?,
            },
            "bylookup" => Node::ByLookup {
                child: child(1)?,
                src: t.get_item(2)?.extract()?,
                dst: t.get_item(3)?.extract()?,
                map: t.get_item(4)?.extract()?,
            },
            "remove" => Node::Remove { child: child(1)?, dim: t.get_item(2)?.extract()?, agg: agg(t.get_item(3)?.extract()?)? },
            "shift" => Node::Shift { child: child(1)?, dim: t.get_item(2)?.extract()?, n: t.get_item(3)?.extract()? },
            "asaxis" => Node::AsAxis { child: child(1)?, dim: t.get_item(2)?.extract()? },
            "select" => Node::Select { child: child(1)?, dim: t.get_item(2)?.extract()?, member: t.get_item(3)?.extract()? },
            _ => return Err(err(format!("未知のノード {tag}"))),
        })
    }
}

#[pymethods]
impl Core {
    #[new]
    fn new() -> Core {
        Core { cat: Arc::new(Catalog::default()) }
    }

    /// 同じ軸と対応表を持つ Core（モデルの複製用）。以後の変更は互いに影響しない（書き込み時に複製）。
    fn fork(&self) -> Core {
        Core { cat: self.cat.clone() }
    }

    /// 同じ中身を指す別のハンドル。どちらかに書き込むと、そのときに中身が複製される。
    fn share(&self, store: &Bound<'_, StoreHandle>) -> StoreHandle {
        StoreHandle { store: store.borrow().store.clone() }
    }

    fn add_dim(&mut self, size: u32, ordered: bool) -> usize {
        let cat = Arc::make_mut(&mut self.cat);
        cat.dims.push(DimInfo { size, ordered });
        cat.dims.len() - 1
    }

    /// 軸のメンバー数を変える（メンバーの追加と削除）。
    fn resize_dim(&mut self, dim: DimId, size: u32) {
        Arc::make_mut(&mut self.cat).dims[dim].size = size;
    }

    /// 格納データから軸 dim のメンバー m を消し、後ろの番号を詰める（values なら値の番号も）。
    fn remove_member(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, dim: DimId, m: u32, values: bool) {
        let mut handle = store.borrow_mut();
        let target = Arc::make_mut(&mut handle.store);
        py.detach(|| target.remove_member(dim, m, values));
    }

    /// 値が value のセルを消す。
    fn drop_value(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, value: f64) {
        let mut handle = store.borrow_mut();
        let target = Arc::make_mut(&mut handle.store);
        py.detach(|| target.drop_value(value));
    }

    /// 値が value のセルを囲む範囲（軸ごとのメンバー番号）。なければ None。
    fn region_of_value(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, value: f64) -> Option<Vec<Vec<u32>>> {
        let s = store.borrow().store.clone();
        py.detach(move || s.region_of_value(value))
    }

    /// src のメンバー番号 -> dst のメンバー番号（なければ -1）の対応を登録する。
    fn add_mapping(&mut self, dst_size: u32, fwd: Vec<i64>) -> usize {
        let cat = Arc::make_mut(&mut self.cat);
        cat.maps.push(mapping(dst_size, fwd));
        cat.maps.len() - 1
    }

    /// 登録済みの対応を置き換える（メンバーの追加やプロパティの設定のあと）。番号は変わらない。
    fn set_mapping(&mut self, id: usize, dst_size: u32, fwd: Vec<i64>) {
        Arc::make_mut(&mut self.cat).maps[id] = mapping(dst_size, fwd);
    }

    /// メンバーが増えてキーのビット幅に収まらなくなった格納データを詰め直す。収まるなら None。
    fn fit(&self, store: &Bound<'_, StoreHandle>) -> PyResult<Option<StoreHandle>> {
        let s = &store.borrow().store;
        if s.pack.fits(&self.cat) {
            return Ok(None);
        }
        Ok(Some(StoreHandle { store: Arc::new(s.repacked(&self.cat).map_err(err)?) }))
    }

    fn compile(&self, tree: &Bound<'_, PyAny>) -> PyResult<Expr> {
        Ok(Expr { node: Arc::new(self.node(tree)?) })
    }

    #[pyo3(signature = (dims, index, is_bool))]
    fn empty(&self, dims: Vec<DimId>, index: Option<DimId>, is_bool: bool) -> PyResult<StoreHandle> {
        let store = Store::new(&dims, index, kind_of(is_bool), &self.cat).map_err(err)?;
        Ok(StoreHandle { store: Arc::new(store) })
    }

    #[allow(clippy::wrong_self_convention)] // Python から呼ぶ名前を保つ
    fn from_rows(&self, dims: Vec<DimId>, index: Option<DimId>, is_bool: bool, cols: Vec<Vec<u32>>, values: Vec<f64>) -> PyResult<StoreHandle> {
        let store = Store::new(&dims, index, kind_of(is_bool), &self.cat).map_err(err)?;
        let cols: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect();
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
        let store = Store::new(&dims, index, kind_of(is_bool), &self.cat).map_err(err)?;
        let slices: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect::<Result<_, _>>()?;
        Ok(StoreHandle { store: Arc::new(store.with_rows(&slices, values.as_slice()?).map_err(err)?) })
    }

    fn write(&self, store: &Bound<'_, StoreHandle>, key: Vec<u32>, value: Option<f64>) {
        Arc::make_mut(&mut store.borrow_mut().store).write(&key, value);
    }

    fn evaluate(&self, py: Python<'_>, expr: &Expr, sources: Vec<Bound<'_, PyAny>>, region: Region) -> PyResult<CubeHandle> {
        let src: Vec<Src> = sources.iter().map(Core::source).collect::<PyResult<_>>()?;
        let (node, cat, r) = (expr.node.clone(), self.cat.clone(), self.restrict(&region));
        let cube = py.detach(move || eval(&node, &cat, &src, &r)).map_err(err)?;
        Ok(CubeHandle { cube: Arc::new(cube) })
    }

    /// 互いに独立な式をまとめて評価する（GIL を外して並列に）。
    fn evaluate_many(&self, py: Python<'_>, items: Vec<(PyRef<'_, Expr>, Vec<Bound<'_, PyAny>>, Region)>) -> PyResult<Vec<CubeHandle>> {
        let jobs: Vec<(Arc<Node>, Vec<Src>, Restrict)> = items
            .iter()
            .map(|(e, s, reg)| Ok((e.node.clone(), s.iter().map(Core::source).collect::<PyResult<_>>()?, self.restrict(reg))))
            .collect::<PyResult<_>>()?;
        let cat = self.cat.clone();
        let cubes: Vec<core::Result<Cube>> = py.detach(move || jobs.par_iter().map(|(n, s, r)| eval(n, &cat, s, r)).collect());
        cubes.into_iter().map(|c| Ok(CubeHandle { cube: Arc::new(c.map_err(err)?) })).collect()
    }

    /// 格納データから region の範囲を切り出した、新しい格納データ。
    fn filter(&self, store: &Bound<'_, StoreHandle>, region: Region) -> StoreHandle {
        StoreHandle { store: Arc::new(store.borrow().store.slice(&self.restrict(&region))) }
    }

    fn replace(&self, py: Python<'_>, store: &Bound<'_, StoreHandle>, region: Region, new: &Bound<'_, PyAny>) -> PyResult<()> {
        let new = Core::as_cube(new)?;
        let r = self.restrict(&region);
        let mut handle = store.borrow_mut();
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
        let r = self.restrict(&region);
        let mut handle = store.borrow_mut();
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
        let s = &store.borrow().store;
        let mut out = Store::new(&s.metric_dims, index, s.kind, &self.cat).map_err(err)?;
        out.replace(&Restrict::all(self.cat.dims.len()), &s.as_cube()).map_err(err)?;
        Ok(StoreHandle { store: Arc::new(out) })
    }

    fn rows(&self, store: &Bound<'_, StoreHandle>) -> (Vec<Vec<u32>>, Vec<f64>, bool) {
        let s = &store.borrow().store;
        let (cols, values) = s.rows();
        (cols, values, s.kind == Kind::Bool)
    }

    fn size(&self, store: &Bound<'_, StoreHandle>) -> usize {
        store.borrow().store.len()
    }

    fn index_dim(&self, store: &Bound<'_, StoreHandle>) -> Option<DimId> {
        store.borrow().store.index_dim()
    }

    fn metric_dims(&self, store: &Bound<'_, StoreHandle>) -> Vec<DimId> {
        store.borrow().store.metric_dims.clone()
    }

    fn cube_len(&self, cube: &Bound<'_, CubeHandle>) -> usize {
        cube.get().cube.cells.len()
    }
}

/// 差分を本体にまとめ直す件数の下限を変える（テスト用）。
#[pyfunction]
fn set_compact_min(n: usize) {
    core::COMPACT_MIN.store(n, std::sync::atomic::Ordering::Relaxed);
}

/// これより少ない件数は並列にしない下限を変える（テスト用。0 にすると常に並列）。
#[pyfunction]
fn set_par_min(n: usize) {
    core::PAR_MIN.store(n, std::sync::atomic::Ordering::Relaxed);
}

/// 準結合で絞り込む片側の件数の上限を変える（テスト用。0 にすると絞り込まない）。
#[pyfunction]
fn set_semi_max(n: usize) {
    core::SEMI_MAX.store(n, std::sync::atomic::Ordering::Relaxed);
}

/// 分割軸以外の軸の索引を使う本体の行数の下限を変える（テスト用。0 にすると常に使う）。
#[pyfunction]
fn set_postings_min_rows(n: usize) {
    core::POSTINGS_MIN_ROWS.store(n, std::sync::atomic::Ordering::Relaxed);
}

#[pymodule]
fn nanashi_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(set_compact_min, m)?)?;
    m.add_function(wrap_pyfunction!(set_par_min, m)?)?;
    m.add_function(wrap_pyfunction!(set_semi_max, m)?)?;
    m.add_function(wrap_pyfunction!(set_postings_min_rows, m)?)?;
    m.add_class::<Core>()?;
    m.add_class::<Expr>()?;
    m.add_class::<CubeHandle>()?;
    m.add_class::<StoreHandle>()?;
    Ok(())
}
