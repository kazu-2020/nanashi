//! キーの詰め方。キーは各軸のメンバー番号をビット単位で詰めた u64 で、先頭の軸が最上位ビットに来る。

#[allow(unused_imports)]
use crate::*;

/// 軸の並びと、各軸のビット位置。先頭の軸が最上位ビットに来る。
#[derive(Clone, Debug, PartialEq)]
pub struct Packing {
    pub dims: Vec<DimId>,
    pub(crate) shifts: Vec<u32>,
    pub(crate) masks: Vec<u64>,
}

pub(crate) fn bits_for(size: u32) -> u32 {
    if size <= 1 {
        1
    } else {
        32 - (size - 1).leading_zeros()
    }
}

impl Packing {
    /// 各軸に、今のメンバー数の 2 倍まで入るビット幅（1 ビットの余裕）を取る。メンバーを追加しても
    /// その範囲なら詰め直さずに済む。余裕を足すと 64 ビットに収まらないときは、余裕なしで詰める。
    pub fn new(dims: &[DimId], cat: &Catalog) -> Result<Packing> {
        let exact: Vec<u32> = dims.iter().map(|&d| bits_for(cat.dims[d].size)).collect();
        let roomy: Vec<u32> = exact.iter().map(|b| b + 1).collect();
        let bits = if roomy.iter().sum::<u32>() <= 64 { roomy } else { exact };
        let total: u32 = bits.iter().sum();
        if total > 64 {
            return Err(format!("軸の組み合わせが 64 ビットに収まらない（{total} ビット）"));
        }
        let mut shifts = vec![0; dims.len()];
        let mut acc = 0;
        for i in (0..dims.len()).rev() {
            shifts[i] = acc;
            acc += bits[i];
        }
        let masks = bits.iter().map(|&b| if b == 64 { u64::MAX } else { (1u64 << b) - 1 }).collect();
        Ok(Packing { dims: dims.to_vec(), shifts, masks })
    }

    #[inline]
    pub fn get(&self, key: u64, i: usize) -> u32 {
        ((key >> self.shifts[i]) & self.masks[i]) as u32
    }

    #[inline]
    pub fn put(&self, i: usize, m: u32) -> u64 {
        debug_assert!(m as u64 <= self.masks[i], "メンバー番号 {m} が軸のビット幅に収まらない");
        (m as u64) << self.shifts[i]
    }

    #[inline]
    pub fn clear(&self, key: u64, i: usize) -> u64 {
        key & !(self.masks[i] << self.shifts[i])
    }

    pub fn pos(&self, d: DimId) -> Option<usize> {
        self.dims.iter().position(|&x| x == d)
    }

    /// 今のメンバー数のメンバー番号が、すべてのビット幅に収まるか。
    pub fn fits(&self, cat: &Catalog) -> bool {
        self.dims.iter().zip(&self.masks).all(|(&d, &mask)| (cat.dims[d].size as u64).saturating_sub(1) <= mask)
    }
}

/// src の詰め方のキーから、dst の詰め方のキーへ（両方にある軸だけを移す）。
pub(crate) struct Proj {
    pub(crate) pairs: Vec<(usize, usize)>,
}

impl Proj {
    pub(crate) fn new(src: &Packing, dst: &Packing) -> Proj {
        let pairs = dst.dims.iter().enumerate().filter_map(|(j, &d)| src.pos(d).map(|i| (i, j))).collect();
        Proj { pairs }
    }

    #[inline]
    pub(crate) fn apply(&self, src: &Packing, dst: &Packing, key: u64) -> u64 {
        let mut out = 0;
        for &(i, j) in &self.pairs {
            out |= dst.put(j, src.get(key, i));
        }
        out
    }
}
