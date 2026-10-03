//! 式の構文木と、式が読む Metric。

#[allow(unused_imports)]
use crate::*;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Op {
    Add,
    Sub,
    Mul,
    Div,
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    And,
    Or,
}

#[derive(Clone, Copy, Debug)]
pub enum Agg {
    Sum,
    Avg,
    Min,
    Max,
    Count,
    First, // 値が 1 つしかないグループ用（引き下ろしの内部で使う）
}

/// 式の構文木。Bin、If、IsBlank、IfBlank の Vec<DimId> は、軸にメンバーを追加したときに
/// 新しいメンバーへ値が広がる軸（影響範囲の計算に使う。評価には使わない）で、型検査（check.rs）が埋める。
/// By は型検査が式の軸を見て ByAgg か ByLookup に決め、ByMetric（対応表がメンバー型の Metric）は
/// 型検査が AsAxis との結合と集計に書き換えるので、どちらも評価には現れない。
#[derive(Clone, Debug)]
pub enum Node {
    Ref(usize), // 評価時に渡す読み出し元の番号
    DimRef(DimId), // 軸そのもの。値は各セルのメンバー番号
    Const(f64, Kind),
    MemberConst(DimId, u32), // 軸のメンバーの定数（Month."Mar"）。値はメンバー番号
    Bin(Op, Box<Node>, Box<Node>, Vec<DimId>),
    Not(Box<Node>),
    If(Box<Node>, Box<Node>, Option<Box<Node>>, Vec<DimId>),
    Filter(Box<Node>, Box<Node>),
    On(Box<Node>, Box<Node>),
    Expand(Box<Node>, Vec<DimId>),
    IsBlank(Box<Node>, Vec<DimId>),
    IfBlank(Box<Node>, f64, bool, Vec<DimId>), // 既定値と、それが真偽値か
    By { child: Box<Node>, src: DimId, dst: DimId, map: usize, agg: Option<Agg>, dim: String, prop: String },
    ByMetric { child: Box<Node>, src: DimId, metric: usize, agg: Option<Agg>, dim: String, prop: String }, // metric は Ref の番号
    ByAgg { child: Box<Node>, src: DimId, dst: DimId, map: usize, agg: Agg },
    ByLookup { child: Box<Node>, src: DimId, dst: DimId, map: usize },
    Remove { child: Box<Node>, dim: DimId, agg: Agg },
    Shift { child: Box<Node>, dim: DimId, n: i64 },
    Select { child: Box<Node>, dim: DimId, member: u32, name: String },
    AsAxis { child: Box<Node>, dim: DimId },
    Coalesce(Box<Node>, Box<Node>), // 左に値があればそれ、なければ右
}

/// 式が読む Metric（格納データ、または評価の途中結果）。
#[derive(Clone)]
pub enum Src {
    Store(Arc<Store>),
    Cube(Arc<Cube>),
}

impl Src {
    pub fn read(&self, cfg: &Config, r: &Restrict) -> Cube {
        match self {
            Src::Store(s) => s.read(r),
            Src::Cube(c) => c.filter(cfg, r),
        }
    }
}
