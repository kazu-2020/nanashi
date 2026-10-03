//! 範囲（軸ごとの対象メンバー）。

#[allow(unused_imports)]
use crate::*;

/// 1 つの軸で対象にするメンバーの集合。所属の検査はビット集合で行う（メンバー数の 1/8 バイト）。
#[derive(Debug)]
pub struct Sel {
    pub members: Vec<u32>, // 昇順
    pub(crate) mask: Vec<u64>,
}

impl Sel {
    pub fn new(mut members: Vec<u32>, size: u32) -> Sel {
        members.sort_unstable();
        members.dedup();
        let mut mask = vec![0u64; (size as usize).div_ceil(64)];
        for &m in &members {
            if (m as usize) < size as usize {
                mask[(m >> 6) as usize] |= 1u64 << (m & 63);
            }
        }
        Sel { members, mask }
    }

    #[inline]
    pub fn has(&self, m: u32) -> bool {
        self.mask.get((m >> 6) as usize).is_some_and(|w| (w >> (m & 63)) & 1 == 1)
    }
}

/// 軸ごとの対象メンバー。None の軸は全メンバー。
#[derive(Clone, Debug)]
pub struct Restrict {
    pub(crate) sels: Vec<Option<Arc<Sel>>>,
}

impl Restrict {
    pub fn all(n_dims: usize) -> Restrict {
        Restrict { sels: vec![None; n_dims] }
    }

    pub fn get(&self, d: DimId) -> Option<&Arc<Sel>> {
        self.sels.get(d).and_then(|s| s.as_ref())
    }

    pub fn with(&self, d: DimId, sel: Sel) -> Restrict {
        let mut r = self.clone();
        r.sels[d] = Some(Arc::new(sel));
        r
    }

    pub fn without(&self, ds: &[DimId]) -> Restrict {
        let mut r = self.clone();
        for &d in ds {
            r.sels[d] = None;
        }
        r
    }

    pub fn is_all(&self) -> bool {
        self.sels.iter().all(|s| s.is_none())
    }
}

pub(crate) fn members(cat: &Catalog, d: DimId, r: &Restrict) -> Vec<u32> {
    match r.get(d) {
        Some(sel) => sel.members.clone(),
        None => (0..cat.dims[d].size).collect(),
    }
}

pub(crate) fn checks(pack: &Packing, r: &Restrict) -> Vec<(usize, Arc<Sel>)> {
    pack.dims.iter().enumerate().filter_map(|(i, &d)| r.get(d).map(|s| (i, s.clone()))).collect()
}

#[inline]
pub(crate) fn pass(pack: &Packing, key: u64, checks: &[(usize, Arc<Sel>)]) -> bool {
    checks.iter().all(|(i, s)| s.has(pack.get(key, *i)))
}
