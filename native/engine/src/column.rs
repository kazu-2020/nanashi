//! 再配置できる不変の平らな列（Column）。格納データの本体（キーの列と値の列）をこの型で持つ。
//!
//! 列は T を隙間なく並べたバイト列で、中にポインタを持たない。そのためヒープにもファイルにも
//! 共有メモリにも同じ並びで置け、置き場所が変わっても読む側のコードは変わらない（`&[T]` として読む）。
//! u64 のキーと f64 の値の列は、Arrow の UInt64 / Float64 の配列の本体と同じ並びである。
//!
//! 列は、置き場所を持つ持ち主（`ColumnOwner`）への参照で持つ。持ち主はヒープの `Box<[T]>` のほか、
//! ファイルの写像や共有メモリの区画でもよく、列が生きている間は持ち主も生きる。複製は参照を増やすだけで、
//! 1 つの持ち主の一部を切り出した列（`slice`）も同じ持ち主を共有する。
//!
//! 今の持ち主はヒープだけである。メモリの数え方は、列の大きさ（`len` × T のバイト数）は置き場所によらず、
//! Rust のヒープにある分（`heap_bytes`）だけを持ち主が答える。

use std::fmt;
use std::marker::PhantomData;
use std::ops::{Deref, Range};
use std::ptr::NonNull;
use std::sync::Arc;

/// 列の持ち主。列のバイト列を、列が生きている間、動かさずに持つ。
pub trait ColumnOwner: Send + Sync + 'static {
    /// Rust のヒープに確保している分（バイト）。ファイルの写像や共有メモリなら 0。
    fn heap_bytes(&self) -> usize;
}

impl<T: Send + Sync + 'static> ColumnOwner for Box<[T]> {
    fn heap_bytes(&self) -> usize {
        self.len() * std::mem::size_of::<T>()
    }
}

/// 不変で連続した T の列。`&[T]` として読む。複製は O(1) で、持ち主を共有する。
pub struct Column<T> {
    ptr: NonNull<T>,
    len: usize,
    owner: Arc<dyn ColumnOwner>,
    _t: PhantomData<T>,
}

// 列は不変で、持ち主は Send + Sync なので、T がそうなら列もスレッドをまたげる。
unsafe impl<T: Send + Sync> Send for Column<T> {}
unsafe impl<T: Send + Sync> Sync for Column<T> {}

impl<T: Copy + Send + Sync + 'static> Column<T> {
    /// 空の列。
    pub fn empty() -> Column<T> {
        Column::from(Vec::new())
    }

    /// 持ち主 owner の持つ列（`owner.as_ref()` の全体）。持ち主は Arc に入れて動かさないので、
    /// 列が生きている間、その参照は有効のままである。
    pub fn from_owner<O: ColumnOwner + AsRef<[T]>>(owner: O) -> Column<T> {
        let owner: Arc<O> = Arc::new(owner);
        let s: &[T] = (*owner).as_ref();
        let (ptr, len) = (NonNull::from(s).cast::<T>(), s.len());
        Column { ptr, len, owner, _t: PhantomData }
    }

    /// 同じ持ち主を共有する、range の部分の列。
    pub fn slice(&self, range: Range<usize>) -> Column<T> {
        assert!(range.start <= range.end && range.end <= self.len, "列の範囲が外れている");
        // SAFETY: range は self の範囲に収まっている
        let ptr = unsafe { NonNull::new_unchecked(self.ptr.as_ptr().add(range.start)) };
        Column { ptr, len: range.len(), owner: self.owner.clone(), _t: PhantomData }
    }

    /// 持ち主が Rust のヒープに確保している分（バイト）。持ち主の全体を数えるので、`slice` で
    /// 切り出した列も持ち主の全体の分を答える。
    pub fn heap_bytes(&self) -> usize {
        self.owner.heap_bytes()
    }

    /// 列の大きさ（バイト）。置き場所によらない。
    pub fn bytes(&self) -> usize {
        self.len * std::mem::size_of::<T>()
    }
}

impl<T: Copy + Send + Sync + 'static> From<Vec<T>> for Column<T> {
    /// ヒープの列。余分な容量は切り落とし、隙間のない平らなバイト列にする。
    fn from(v: Vec<T>) -> Column<T> {
        Column::from_owner(v.into_boxed_slice())
    }
}

impl<T> Deref for Column<T> {
    type Target = [T];

    #[inline]
    fn deref(&self) -> &[T] {
        // SAFETY: ptr と len は持ち主の持つ列を指し、持ち主は self が生きている間生きていて、列を変えない
        unsafe { std::slice::from_raw_parts(self.ptr.as_ptr(), self.len) }
    }
}

impl<'a, T> IntoIterator for &'a Column<T> {
    type Item = &'a T;
    type IntoIter = std::slice::Iter<'a, T>;

    fn into_iter(self) -> std::slice::Iter<'a, T> {
        self.iter()
    }
}

impl<T> Clone for Column<T> {
    fn clone(&self) -> Column<T> {
        Column { ptr: self.ptr, len: self.len, owner: self.owner.clone(), _t: PhantomData }
    }
}

impl<T: fmt::Debug> fmt::Debug for Column<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_list().entries(self.iter()).finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heap_column_reads_as_slice() {
        let c: Column<u64> = vec![3, 1, 4, 1, 5].into();
        assert_eq!(&*c, &[3, 1, 4, 1, 5]);
        assert_eq!(c.len(), 5);
        assert_eq!(c.bytes(), 40);
        assert_eq!(c.heap_bytes(), 40);
        assert_eq!(c.partition_point(|&k| k < 4), 2);
        assert_eq!(c[2], 4);
    }

    #[test]
    fn empty_column() {
        let c: Column<f64> = Column::empty();
        assert!(c.is_empty());
        assert_eq!(c.bytes(), 0);
        assert_eq!(c.heap_bytes(), 0);
        assert_eq!(c.slice(0..0).len(), 0);
    }

    #[test]
    fn spare_capacity_is_dropped() {
        let mut v = Vec::with_capacity(100);
        v.extend([1.0f64, 2.0]);
        let c: Column<f64> = v.into();
        assert_eq!(c.heap_bytes(), 16);
    }

    #[test]
    fn slices_share_the_owner() {
        let c: Column<u64> = (0..10).collect::<Vec<_>>().into();
        let s = c.slice(3..7);
        assert_eq!(&*s, &[3, 4, 5, 6]);
        assert_eq!(s.heap_bytes(), 80);
        assert_eq!(Arc::strong_count(&c.owner), 2);
        let d = c.clone();
        drop(c);
        assert_eq!(&*s, &[3, 4, 5, 6]); // 元の列を捨てても持ち主は生きている
        assert_eq!(&d[..3], &[0, 1, 2]);
    }

    #[test]
    #[should_panic(expected = "列の範囲が外れている")]
    fn slice_out_of_range_panics() {
        let c: Column<u64> = vec![1, 2, 3].into();
        let _ = c.slice(2..4);
    }

    #[test]
    fn custom_owner_off_heap_counts_zero() {
        struct Static(&'static [u64]);
        impl ColumnOwner for Static {
            fn heap_bytes(&self) -> usize {
                0
            }
        }
        impl AsRef<[u64]> for Static {
            fn as_ref(&self) -> &[u64] {
                self.0
            }
        }
        static DATA: [u64; 3] = [7, 8, 9];
        let c = Column::from_owner(Static(&DATA));
        assert_eq!(&*c, &[7, 8, 9]);
        assert_eq!(c.bytes(), 24);
        assert_eq!(c.heap_bytes(), 0);
    }

    #[test]
    fn column_is_send_and_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<Column<u64>>();
        assert_send_sync::<Column<f64>>();
    }
}
