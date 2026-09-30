//! nanashi の計算エンジン（Python に依存しない）。疎な Cube の格納、式の型検査と評価、計算計画、
//! 差分再計算、Parquet の読み書き。Python からは nanashi_core（native/src/lib.rs）を通して使う。
//!
//! キーは各軸のメンバー番号をビット単位で詰めた u64。値は f64 で、真偽値は 1.0 / 0.0。
//! 空のセルはキーがないことで表す。

mod ast;
mod catalog;
mod cells;
pub mod check;
mod config;
mod cube;
mod eval;
pub mod graph;
mod key;
pub mod plan;
pub mod pq;
mod restrict;
mod store;

pub use ast::*;
pub use catalog::*;
pub use config::*;
pub(crate) use cells::*;
pub use cube::*;
pub use eval::*;
pub use key::*;
pub use restrict::*;
pub use store::*;

pub(crate) use imbl::ordmap::DiffItem;
pub(crate) use imbl::OrdMap;
pub(crate) use rayon::prelude::*;
pub(crate) use rustc_hash::{FxHashMap, FxHashSet};
pub(crate) use std::ops::Bound;
pub(crate) use std::sync::{Arc, OnceLock};
