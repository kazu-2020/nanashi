//! Cube（評価の途中結果）。キー順に並んだセルの列。

#[allow(unused_imports)]
use crate::*;

#[derive(Clone, Debug)]
pub struct Cube {
    pub pack: Packing,
    pub kind: Kind,
    pub cells: Vec<(u64, f64)>,
}

impl Cube {
    pub fn dims(&self) -> &[DimId] {
        &self.pack.dims
    }

    pub fn filter(&self, cfg: &Config, r: &Restrict) -> Cube {
        let cs = checks(&self.pack, r);
        if cs.is_empty() {
            return self.clone();
        }
        let cells = map_cells(cfg, &self.cells, |k, v| pass(&self.pack, k, &cs).then_some((k, v)));
        Cube { pack: self.pack.clone(), kind: self.kind, cells }
    }

    pub fn repack(&self, cfg: &Config, pack: &Packing) -> Cube {
        if &self.pack == pack {
            return self.clone();
        }
        let proj = Proj::new(&self.pack, pack);
        let cells = map_cells(cfg, &self.cells, |k, v| Some((proj.apply(&self.pack, pack, k), v)));
        Cube { pack: pack.clone(), kind: self.kind, cells }
    }
}
