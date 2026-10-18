//! 速さのための調整値。Catalog ごと（Python の Core ごと）に持ち、格納データは作ったときの値を写して持つ。
//! テストでは小さくして、大きなモデルでだけ通る経路を小さなモデルでも通す。

/// 速さのための調整値。結果は変えない（どの値でも同じ結果になることをテストで確かめる）。
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Config {
    /// The fewest rows of the delta limit. The delta limit is 1/8 of the base rows, between compact_min and delta_max.
    pub compact_min: usize,
    /// The most rows of the delta for any base size. If compact_min is larger, compact_min is the limit.
    pub delta_max: usize,
    /// The rows after which a segment of the base ends, at the next partition-member boundary.
    /// shortcut: 4 M rows make a value column of 32 MB, the largest dynamic mmap threshold of glibc. So glibc
    /// maps the column, and a free gives the memory back to the OS. A smaller column stays in the malloc arena
    /// of its thread, and RSS grows by about 300 MB at x32. If the segment size must change, use a column
    /// allocator that maps its own memory.
    pub seg_rows: usize,
    /// これより少ない件数は並列にしない。rayon を呼ぶだけでスレッドプールへの受け渡し（数マイクロ秒）が
    /// かかり、差分再計算のように小さな演算を何百回も繰り返すときに積み上がるため。
    pub par_min: usize,
    /// これより行の少ない本体には、分割軸以外の軸の索引を作らない。
    pub postings_min_rows: usize,
    /// 集計先が少なくなくても、読みながら集計する経路を使う（テスト用）。
    pub stream_always: bool,
    /// これより行の多い Metric では、値が変わった範囲がセル全体の大半を占めれば全体に広げる。
    pub widen_min_rows: usize,
    /// 1 つの式の評価が同時に持つ途中結果のバイト数の上限。並列に評価するときは、この予算を分ける。
    pub max_bytes: usize,
    /// 融合した式の評価や集計で、並列にするときの仕事の単位（行数）。
    pub chunk: usize,
    /// 集計先の全組み合わせの配列へ足し込む集計を、配列の大きさによらず使う（テスト用）。
    pub dense_always: bool,
    /// If the first 2^16 rows of an aggregation into an array touch more bytes of it than this, the rows go
    /// through bucket buffers (eval/agg.rs).
    pub scatter_bytes: usize,
    /// 要素ごとの演算をつないだ式を、途中結果を作らずに 1 回の走査で評価する（eval/fuse.rs）。
    /// false にすると演算ごとに評価する（テストで両方の結果を突き合わせる）。
    pub fuse: bool,
    /// テスト用に、この Metric の番号の書き戻しで再計算を失敗させる（true なら panic させる）。
    pub fail_at: Option<(usize, bool)>,
}

impl Default for Config {
    fn default() -> Config {
        Config {
            compact_min: 4096,
            delta_max: 1 << 21,
            seg_rows: 1 << 22,
            par_min: 16_384,
            postings_min_rows: 1024,
            stream_always: false,
            widen_min_rows: 4096,
            max_bytes: usize::MAX,
            fuse: true,
            chunk: 1 << 16,
            dense_always: false,
            scatter_bytes: 4 << 20,
            fail_at: None,
        }
    }
}

impl Config {
    #[inline]
    pub fn par(&self, n: usize) -> bool {
        n >= self.par_min
    }
}
