//! 速さのための調整値。Catalog ごと（Python の Core ごと）に持ち、格納データは作ったときの値を写して持つ。
//! テストでは小さくして、大きなモデルでだけ通る経路を小さなモデルでも通す。

/// 速さのための調整値。結果は変えない（どの値でも同じ結果になることをテストで確かめる）。
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Config {
    /// 差分がこの件数（と本体の 1/8）を超えたら本体にまとめ直す。
    pub compact_min: usize,
    /// これより少ない件数は並列にしない。rayon を呼ぶだけでスレッドプールへの受け渡し（数マイクロ秒）が
    /// かかり、差分再計算のように小さな演算を何百回も繰り返すときに積み上がるため。
    pub par_min: usize,
    /// これより行の少ない本体には、分割軸以外の軸の索引を作らない。
    pub postings_min_rows: usize,
    /// 集計先が少なくなくても、読みながら集計する経路を使う（テスト用）。
    pub stream_always: bool,
    /// INNER JOIN の左がこの件数以下なら、右を左のメンバーに絞って評価する。
    pub semi_max: usize,
    /// これより行の多い Metric では、値が変わった範囲がセル全体の大半を占めれば全体に広げる。
    pub widen_min_rows: usize,
    /// 1 つの式の評価が同時に持つ途中結果のバイト数の上限。並列に評価するときは、この予算を分ける。
    pub max_bytes: usize,
    /// 融合した式の評価や集計で、並列にするときの仕事の単位（行数）。
    pub chunk: usize,
    /// 集計先の全組み合わせの配列へ足し込む集計を、配列の大きさによらず使う（テスト用）。
    pub dense_always: bool,
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
            par_min: 16_384,
            postings_min_rows: 1024,
            stream_always: false,
            semi_max: 4096,
            widen_min_rows: 4096,
            max_bytes: usize::MAX,
            fuse: true,
            chunk: 1 << 16,
            dense_always: false,
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
