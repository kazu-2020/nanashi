//! 軸、プロパティの対応表、値の種類。

#[allow(unused_imports)]
use crate::*;

pub type DimId = usize;

pub type Result<T> = std::result::Result<T, String>;

#[derive(Clone, Debug)]
pub struct DimInfo {
    pub size: u32,
    pub ordered: bool,
    pub name: String, // 型検査の文言に使う
}

/// 軸のプロパティ（例: Employee.Department）。src のメンバー -> dst のメンバー。
#[derive(Clone, Debug)]
pub struct Mapping {
    pub fwd: Vec<i64>,      // src メンバー -> dst メンバー（なければ -1）
    pub inv: Vec<Vec<u32>>, // dst メンバー -> src メンバーの一覧
}

/// 軸と対応表。複製したモデルどうしで Arc で共有し、変えるときに写す（対応表は Arc なので、
/// 軸を 1 つ変えても対応表の中身までは写さない）。
#[derive(Clone, Default, Debug)]
pub struct Catalog {
    pub dims: Vec<DimInfo>,
    pub maps: Vec<Arc<Mapping>>,
    pub cfg: Config,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Num,
    Bool,
}
