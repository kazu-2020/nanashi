//! The run of a resolved SQL plan (`POST /sql`, docs/sql.md): one summarize for each aggregate, an outer join on
//! the group keys, ORDER BY, LIMIT, and the Arrow or JSON encode. Names take part only as output text.

use crate::*;
use arrow_array::{ArrayRef, DictionaryArray, Float64Array, Int32Array, RecordBatch, StringArray};
use arrow_ipc::writer::StreamWriter;
use arrow_schema::{DataType, Field, Schema};
use std::cmp::Ordering;
use std::collections::HashMap;
use std::fmt::Write;

/// An output column or a sort key: an index into `Select::keep` or `Select::aggs`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Col {
    Dim(usize),
    Agg(usize),
}

#[derive(Clone, Copy, Debug)]
pub struct Key {
    pub col: Col,
    pub desc: bool,
    pub nulls_first: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Format {
    Arrow,
    Json,
}

/// A resolved plan. The caller makes sure that:
/// - each Store has every dimension of `keep` and `region`,
/// - `columns` and `order` index into `keep` and `aggs`,
/// - `names[j]` has one name for each member of `keep[j]`.
pub struct Select<S> {
    /// The GROUP BY dimensions, in SQL order (the key order of the join).
    pub keep: Vec<DimId>,
    pub aggs: Vec<(Arc<Store>, ReadAgg)>,
    pub region: Restrict,
    /// The SELECT list: (output name, column).
    pub columns: Vec<(String, Col)>,
    pub order: Vec<Key>,
    pub limit: Option<usize>,
    /// For each kept dimension: member number -> name (the Arrow dictionary).
    pub names: Vec<Vec<S>>,
    /// For each kept dimension: member number -> rank, or None for the order of the numbers.
    pub ranks: Vec<Option<Vec<u32>>>,
    /// The most cells (rows x columns) in the result.
    pub max_cells: usize,
    pub seq: u64,
    pub format: Format,
}

/// The joined groups, in ascending key order. Row i is one group.
struct Table {
    keys: Vec<u64>,
    vals: Vec<Vec<f64>>,
    /// For each aggregate: None if every row has a value.
    valid: Vec<Option<Vec<bool>>>,
}

/// Run the plan and return the encoded body. Err: sql_too_large {rows, cells, max}, or sql_budget {detail}.
/// One Budget holds the stores and every buffer of the query.
pub fn run<S: AsRef<str> + Sync>(sel: &Select<S>, cat: &Catalog) -> std::result::Result<Vec<u8>, Diag> {
    let budget = |e: String| Diag::new("sql_budget").arg("detail", e);
    let sizes: Vec<u64> = sel.keep.iter().map(|&d| cat.dims[d].size as u64).collect();
    // The key of each Metric fits in 64 bits and has all kept dimensions, so this product cannot overflow
    let mut strides = vec![1u64; sizes.len()];
    for j in (0..sizes.len().saturating_sub(1)).rev() {
        strides[j] = strides[j + 1] * sizes[j + 1];
    }
    let b = Budget::new(cat.cfg.max_bytes, held(&sel.aggs.iter().map(|(s, _)| Src::Store(s.clone())).collect::<Vec<_>>()).saturating_add(cat.cfg.cache_bound()));
    let zero = b.mark();
    let one = |(s, a): &(Arc<Store>, ReadAgg), b: &Budget| -> Result<Vec<(u64, f64)>> {
        let start = b.mark();
        let g = s.summarize(&sel.keep, &sel.region, *a, cat, b)?;
        b.settle_bytes(start, g.vals.len() * (8 + 4 * g.cols.len()))?;
        b.need(g.vals.len(), "SQL の結合")?;
        let pairs = pack(g, s, &sel.keep, &strides);
        b.settle_bytes(start, pairs.capacity() * CELL_BYTES)?;
        Ok(pairs)
    };
    // A Budget is not Sync, so with a limit the aggregates run one at a time
    let lists: Vec<Vec<(u64, f64)>> = if cat.cfg.max_bytes == usize::MAX {
        sel.aggs.par_iter().map(|a| one(a, &Budget::new(usize::MAX, 0))).collect::<Result<_>>()
    } else {
        sel.aggs.iter().map(|a| one(a, &b)).collect::<Result<_>>()
    }
    .map_err(budget)?;
    let width = 8 + 9 * sel.aggs.len();
    let groups = lists.iter().map(Vec::len).sum::<usize>();
    b.need((groups.saturating_mul(width + 16)).div_ceil(CELL_BYTES), "SQL の結合").map_err(budget)?;
    let t = join(lists, sel.keep.is_empty(), &sel.aggs.iter().map(|a| a.1).collect::<Vec<_>>());
    let n = t.keys.len();
    b.settle_bytes(zero, n * width).map_err(budget)?;
    let take = sel.limit.map_or(n, |l| l.min(n));
    let cells = take.saturating_mul(sel.columns.len());
    if cells > sel.max_cells {
        return Err(Diag::new("sql_too_large").arg("rows", Arg::Int(take as i64)).arg("cells", Arg::Int(cells as i64)).arg("max", Arg::Int(sel.max_cells as i64)));
    }
    let dim_keys = sel.order.iter().filter(|k| matches!(k.col, Col::Dim(_))).count();
    b.need((n * 4 * (1 + dim_keys)).div_ceil(CELL_BYTES), "SQL の ORDER BY").map_err(budget)?;
    let member = |j: usize, k: u64| ((k / strides[j]) % sizes[j]) as u32;
    let rows = order(&t, &sel.order, take, |j, r| {
        let m = member(j, t.keys[r]);
        sel.ranks[j].as_ref().map_or(m, |rank| rank[m as usize])
    });
    b.settle_bytes(zero, n * width + rows.capacity() * 4).map_err(budget)?;
    let size = encoded_size(sel, &t, &rows, member);
    b.need((if sel.format == Format::Arrow { 2 * size } else { size }).div_ceil(CELL_BYTES), "SQL の結果").map_err(budget)?;
    match sel.format {
        Format::Json => Ok(json(sel, &t, &rows, member, size)),
        Format::Arrow => arrow(sel, &t, &rows, member).map_err(budget),
    }
}

/// The most bytes of the JSON body, or about the bytes of the Arrow batch (the IPC stream is a second copy).
fn encoded_size<S: AsRef<str>>(sel: &Select<S>, t: &Table, rows: &[u32], member: impl Fn(usize, u64) -> u32) -> usize {
    let names = sel.columns.iter().map(|(name, _)| escaped_len(name) + 3).fold(0, usize::saturating_add);
    if sel.format == Format::Arrow {
        let text = |j: usize| sel.names[j].iter().map(|s| s.as_ref().len()).sum::<usize>();
        let cols = sel.columns.iter().map(|(_, c)| match *c {
            Col::Dim(j) => (4 * rows.len()).saturating_add(text(j) + 4 * (sel.names[j].len() + 1) + 256),
            Col::Agg(_) => rows.len().saturating_mul(12).saturating_add(256),
        });
        return cols.fold(256usize.saturating_add(names), usize::saturating_add);
    }
    // 24 bytes is the longest `{:?}` text of an f64, for example -2.2250738585072014e-308
    let row = sel.columns.iter().map(|(_, c)| if matches!(c, Col::Agg(_)) { 25 } else { 3 }).sum::<usize>() + 3;
    let dims = sel.columns.iter().filter_map(|(_, c)| if let Col::Dim(j) = *c { Some(j) } else { None });
    let text = dims.map(|j| rows.iter().map(|&r| escaped_len(sel.names[j][member(j, t.keys[r as usize]) as usize].as_ref())).fold(0, usize::saturating_add));
    text.fold(rows.len().saturating_mul(row), usize::saturating_add).saturating_add(64 + names)
}

/// The bytes that `quote` writes between the quotes.
fn escaped_len(s: &str) -> usize {
    s.len() + s.bytes().map(|b| match b {
        b'"' | b'\\' => 1,
        0..0x20 => 5,
        _ => 0,
    }).sum::<usize>()
}

/// The groups of one aggregate as (key, value) pairs in ascending key order. The key is a mixed radix of the
/// member numbers in `keep` order, first dimension most significant, so key order is the member-number order.
fn pack(g: Groups, s: &Store, keep: &[DimId], strides: &[u64]) -> Vec<(u64, f64)> {
    let order: Vec<DimId> = s.metric_dims.iter().copied().filter(|d| keep.contains(d)).collect();
    let at: Vec<usize> = keep.iter().map(|d| order.iter().position(|x| x == d).expect("the Metric has each kept dimension")).collect();
    let mut pairs: Vec<(u64, f64)> = (0..g.vals.len()).map(|i| (at.iter().zip(strides).map(|(&p, &st)| g.cols[p][i] as u64 * st).sum(), g.vals[i])).collect();
    if !pairs.is_sorted_by_key(|p| p.0) {
        pairs.par_sort_unstable_by_key(|p| p.0);
    }
    pairs
}

/// The outer join of the sorted lists: one row for each key of any list. An absent value is NULL, but an absent
/// COUNT is 0. With no kept dimensions, there is always one row (key 0).
fn join(lists: Vec<Vec<(u64, f64)>>, no_keep: bool, aggs: &[ReadAgg]) -> Table {
    let keys = if no_keep {
        vec![0]
    } else {
        lists.iter().fold(Vec::new(), |acc: Vec<u64>, l| {
            if acc.is_empty() {
                return l.iter().map(|p| p.0).collect();
            }
            let mut out = Vec::with_capacity(acc.len().max(l.len()));
            let (mut i, mut j) = (0, 0);
            while i < acc.len() || j < l.len() {
                let (a, b) = (acc.get(i).copied().unwrap_or(u64::MAX), l.get(j).map_or(u64::MAX, |p| p.0));
                let at_end = (i == acc.len(), j == l.len());
                let k = match at_end {
                    (true, _) => b,
                    (_, true) => a,
                    _ => a.min(b),
                };
                out.push(k);
                i += (i < acc.len() && a == k) as usize;
                j += (j < l.len() && b == k) as usize;
            }
            out
        })
    };
    let (mut vals, mut valid) = (Vec::with_capacity(lists.len()), Vec::with_capacity(lists.len()));
    for (l, &a) in lists.iter().zip(aggs) {
        let absent = if a == ReadAgg::Count { 0.0 } else { f64::NAN };
        if l.len() == keys.len() {
            vals.push(l.iter().map(|p| p.1).collect());
            valid.push(None);
            continue;
        }
        let (mut v, mut ok) = (Vec::with_capacity(keys.len()), Vec::with_capacity(keys.len()));
        let mut it = l.iter().peekable();
        for &k in &keys {
            match it.next_if(|p| p.0 == k) {
                Some(p) => {
                    v.push(p.1);
                    ok.push(true);
                }
                None => {
                    v.push(absent);
                    ok.push(a == ReadAgg::Count);
                }
            }
        }
        vals.push(v);
        valid.push(if a == ReadAgg::Count { None } else { Some(ok) });
    }
    Table { keys, vals, valid }
}

/// NaN is above every number, and -0.0 equals 0.0 (DuckDB).
fn cmp_f64(a: f64, b: f64) -> Ordering {
    match (a.is_nan(), b.is_nan()) {
        (true, true) => Ordering::Equal,
        (true, false) => Ordering::Greater,
        (false, true) => Ordering::Less,
        _ => a.partial_cmp(&b).expect("no NaN"),
    }
}

/// The first `take` rows in ORDER BY order. `rank(j, row)` is the sort value of kept dimension j. Ties keep the
/// key order, so the result is the same for each run.
fn order(t: &Table, keys: &[Key], take: usize, rank: impl Fn(usize, usize) -> u32 + Sync) -> Vec<u32> {
    enum Sort {
        Ranks(Vec<u32>),
        Agg(usize),
    }
    let n = t.keys.len();
    if keys.is_empty() || take == 0 {
        return (0..take as u32).collect();
    }
    let sorts: Vec<Sort> = keys
        .iter()
        .map(|k| match k.col {
            Col::Dim(j) => Sort::Ranks((0..n).into_par_iter().map(|r| rank(j, r)).collect()),
            Col::Agg(i) => Sort::Agg(i),
        })
        .collect();
    let cmp = |&a: &u32, &b: &u32| -> Ordering {
        let (a, b) = (a as usize, b as usize);
        for (k, sort) in keys.iter().zip(&sorts) {
            let o = match *sort {
                Sort::Ranks(ref d) => dir(d[a].cmp(&d[b]), k.desc),
                Sort::Agg(i) => {
                    let null = |r: usize| t.valid[i].as_ref().is_some_and(|v| !v[r]);
                    match (null(a), null(b)) {
                        (true, true) => Ordering::Equal,
                        (true, false) => if k.nulls_first { Ordering::Less } else { Ordering::Greater },
                        (false, true) => if k.nulls_first { Ordering::Greater } else { Ordering::Less },
                        (false, false) => dir(cmp_f64(t.vals[i][a], t.vals[i][b]), k.desc),
                    }
                }
            };
            if o != Ordering::Equal {
                return o;
            }
        }
        a.cmp(&b)
    };
    let mut rows: Vec<u32> = (0..n as u32).collect();
    if take < n {
        rows.select_nth_unstable_by(take - 1, cmp);
        rows.truncate(take);
    }
    rows.par_sort_unstable_by(cmp);
    rows
}

fn dir(o: Ordering, desc: bool) -> Ordering {
    if desc {
        o.reverse()
    } else {
        o
    }
}

/// One record batch through `rows`. A dimension column is a dictionary of all its members, with the member
/// numbers as keys. An aggregate column is a nullable float64.
fn arrow<S: AsRef<str>>(sel: &Select<S>, t: &Table, rows: &[u32], member: impl Fn(usize, u64) -> u32) -> Result<Vec<u8>> {
    let e = |e: arrow_schema::ArrowError| format!("Arrow IPC: {e}");
    let mut fields = Vec::with_capacity(sel.columns.len());
    let mut columns: Vec<ArrayRef> = Vec::with_capacity(sel.columns.len());
    for (name, col) in &sel.columns {
        match *col {
            Col::Dim(j) => {
                let keys = Int32Array::from_iter_values(rows.iter().map(|&r| member(j, t.keys[r as usize]) as i32));
                let values = StringArray::from_iter_values(sel.names[j].iter().map(|s| s.as_ref()));
                fields.push(Field::new(name, DataType::Dictionary(Box::new(DataType::Int32), Box::new(DataType::Utf8)), false));
                columns.push(Arc::new(DictionaryArray::try_new(keys, Arc::new(values)).map_err(e)?));
            }
            Col::Agg(i) => {
                let ok = |r: usize| t.valid[i].as_ref().is_none_or(|v| v[r]);
                fields.push(Field::new(name, DataType::Float64, true));
                columns.push(Arc::new(rows.iter().map(|&r| ok(r as usize).then(|| t.vals[i][r as usize])).collect::<Float64Array>()));
            }
        }
    }
    let schema = Arc::new(Schema::new_with_metadata(fields, HashMap::from([("seq".to_string(), sel.seq.to_string())])));
    let batch = RecordBatch::try_new(schema.clone(), columns).map_err(e)?;
    let mut w = StreamWriter::try_new(Vec::new(), &schema).map_err(e)?;
    w.write(&batch).map_err(e)?;
    w.into_inner().map_err(e)
}

/// `{"seq": n, "columns": [...], "rows": [[...], ...]}`. NaN and the infinities are strings (proto3 JSON).
fn json<S: AsRef<str>>(sel: &Select<S>, t: &Table, rows: &[u32], member: impl Fn(usize, u64) -> u32, size: usize) -> Vec<u8> {
    let mut out = String::with_capacity(size);
    write!(out, "{{\"seq\":{},\"columns\":[", sel.seq).expect("a String write does not fail");
    for (i, (name, _)) in sel.columns.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        quote(&mut out, name);
    }
    out.push_str("],\"rows\":[");
    for (n, &r) in rows.iter().enumerate() {
        let r = r as usize;
        out.push_str(if n == 0 { "[" } else { ",[" });
        for (i, (_, col)) in sel.columns.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            match *col {
                Col::Dim(j) => quote(&mut out, sel.names[j][member(j, t.keys[r]) as usize].as_ref()),
                Col::Agg(a) if t.valid[a].as_ref().is_some_and(|v| !v[r]) => out.push_str("null"),
                Col::Agg(a) => match t.vals[a][r] {
                    x if x.is_finite() => write!(out, "{x:?}").expect("a String write does not fail"),
                    x if x.is_nan() => out.push_str("\"NaN\""),
                    x if x > 0.0 => out.push_str("\"Infinity\""),
                    _ => out.push_str("\"-Infinity\""),
                },
            }
        }
        out.push(']');
    }
    out.push_str("]}");
    out.into_bytes()
}

fn quote(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => write!(out, "\\u{:04x}", c as u32).expect("a String write does not fail"),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow_array::cast::AsArray;
    use arrow_array::types::{Float64Type, Int32Type};
    use arrow_array::Array;
    use arrow_ipc::reader::StreamReader;

    /// Dimensions: 0 Month (4 members), 1 Product (3 members).
    fn catalog() -> Catalog {
        let dims = vec![DimInfo { size: 4, ordered: true, name: "Month".into() }, DimInfo { size: 3, ordered: false, name: "Product".into() }];
        Catalog { dims, maps: Vec::new(), cfg: Config::default() }
    }

    /// A Metric on (Product, Month): its declared order is not the GROUP BY order.
    fn store(cat: &Catalog, rows: &[(u32, u32, f64)]) -> Arc<Store> {
        let cols = [rows.iter().map(|r| r.0).collect::<Vec<_>>(), rows.iter().map(|r| r.1).collect()];
        let vals: Vec<f64> = rows.iter().map(|r| r.2).collect();
        let slices: Vec<&[u32]> = cols.iter().map(|c| c.as_slice()).collect();
        Arc::new(Store::new(&[1, 0], Some(1), Kind::Num, cat).unwrap().with_rows(&slices, &vals).unwrap())
    }

    const MONTHS: [&str; 4] = ["Jan", "Feb", "Mar", "Apr"];
    const PRODUCTS: [&str; 3] = ["A", "B", "C"];

    fn select(aggs: Vec<(Arc<Store>, ReadAgg)>, columns: &[(&str, Col)], order: Vec<Key>, limit: Option<usize>, format: Format) -> Select<&'static str> {
        Select {
            keep: vec![0, 1],
            aggs,
            region: Restrict::all(2),
            columns: columns.iter().map(|(n, c)| (n.to_string(), *c)).collect(),
            order,
            limit,
            names: vec![MONTHS.to_vec(), PRODUCTS.to_vec()],
            ranks: vec![None, Some(vec![2, 0, 1])],
            max_cells: 100,
            seq: 7,
            format,
        }
    }

    fn text(b: Vec<u8>) -> String {
        String::from_utf8(b).unwrap()
    }

    fn three(cat: &Catalog) -> Vec<(Arc<Store>, ReadAgg)> {
        let revenue = store(cat, &[(0, 0, 10.0), (1, 1, 5.0), (2, 1, f64::INFINITY)]);
        let cost = store(cat, &[(0, 0, 3.0), (0, 2, 4.0)]);
        vec![(revenue.clone(), ReadAgg::Sum), (cost.clone(), ReadAgg::Max), (cost, ReadAgg::Count)]
    }

    const COLS: [(&str, Col); 5] = [("Month", Col::Dim(0)), ("Product", Col::Dim(1)), ("rev", Col::Agg(0)), ("max(Cost)", Col::Agg(1)), ("n", Col::Agg(2))];

    #[test]
    fn the_outer_join_of_three_aggregates_with_holes() {
        let cat = catalog();
        let body = run(&select(three(&cat), &COLS, vec![], None, Format::Json), &cat).unwrap();
        assert_eq!(
            text(body),
            r#"{"seq":7,"columns":["Month","Product","rev","max(Cost)","n"],"rows":[["Jan","A",10.0,3.0,1.0],["Feb","B",5.0,null,0.0],["Feb","C","Infinity",null,0.0],["Mar","A",null,4.0,1.0]]}"#
        );
    }

    #[test]
    fn order_by_puts_nulls_last_and_uses_the_ranks_of_a_dimension() {
        let cat = catalog();
        let desc = |col| Key { col, desc: true, nulls_first: false };
        let body = run(&select(three(&cat), &COLS[..3], vec![desc(Col::Agg(0))], None, Format::Json), &cat).unwrap();
        assert_eq!(text(body), r#"{"seq":7,"columns":["Month","Product","rev"],"rows":[["Feb","C","Infinity"],["Jan","A",10.0],["Feb","B",5.0],["Mar","A",null]]}"#);
        let asc = Key { col: Col::Dim(1), desc: false, nulls_first: false };
        let body = run(&select(three(&cat), &COLS[..2], vec![asc], None, Format::Json), &cat).unwrap();
        assert_eq!(text(body), r#"{"seq":7,"columns":["Month","Product"],"rows":[["Feb","B"],["Feb","C"],["Jan","A"],["Mar","A"]]}"#);
        let first = Key { col: Col::Agg(1), desc: false, nulls_first: true };
        let body = run(&select(three(&cat), &COLS[3..4], vec![first], None, Format::Json), &cat).unwrap();
        assert_eq!(text(body), r#"{"seq":7,"columns":["max(Cost)"],"rows":[[null],[null],[3.0],[4.0]]}"#);
    }

    #[test]
    fn a_limit_gives_the_prefix_of_the_full_order() {
        let cat = catalog();
        let rows: Vec<(u32, u32, f64)> = (0..3).flat_map(|p| (0..4).map(move |m| (p, m, ((p * 7 + m * 3) % 5) as f64))).collect();
        let aggs = vec![(store(&cat, &rows), ReadAgg::Sum)];
        let keys = vec![Key { col: Col::Agg(0), desc: true, nulls_first: false }, Key { col: Col::Dim(0), desc: false, nulls_first: false }];
        let full = run(&select(aggs.clone(), &COLS[..3], keys.clone(), None, Format::Json), &cat).unwrap();
        let full = text(full);
        for limit in 0..14 {
            let body = text(run(&select(aggs.clone(), &COLS[..3], keys.clone(), Some(limit), Format::Json), &cat).unwrap());
            let rows: Vec<&str> = full.split("],[").collect();
            let want = if limit == 0 { r#"{"seq":7,"columns":["Month","Product","rev"],"rows":[]}"#.to_string() } else if limit >= 12 { full.clone() } else { format!("{}]]}}", rows[..limit].join("],[")) };
            assert_eq!(body, want, "limit {limit}");
        }
        assert!(full.starts_with(r#"{"seq":7,"columns":["Month","Product","rev"],"rows":[["Jan","C",4.0],["Apr","A",4.0],["Feb","A",3.0],["Mar","B",3.0],["Apr","C",3.0],"#), "{full}");
    }

    #[test]
    fn no_group_by_gives_one_row_also_without_cells() {
        let cat = catalog();
        let empty = store(&cat, &[]);
        let mut sel = select(vec![(empty.clone(), ReadAgg::Sum), (empty, ReadAgg::Count)], &[("s", Col::Agg(0)), ("c", Col::Agg(1))], vec![], None, Format::Json);
        sel.keep.clear();
        sel.names.clear();
        sel.ranks.clear();
        assert_eq!(text(run(&sel, &cat).unwrap()), r#"{"seq":7,"columns":["s","c"],"rows":[[null,0.0]]}"#);
    }

    #[test]
    fn the_aggregates_share_one_memory_budget() {
        let mut cat = catalog();
        let (mut low, mut high) = (0, 1 << 30);
        while low < high {
            let mid = (low + high) / 2;
            cat.cfg.max_bytes = mid;
            if run(&select(three(&cat)[..1].to_vec(), &COLS[..3], vec![], None, Format::Json), &cat).is_ok() {
                high = mid;
            } else {
                low = mid + 1;
            }
        }
        cat.cfg.max_bytes = low;
        assert!(run(&select(three(&cat)[..1].to_vec(), &COLS[..3], vec![], None, Format::Json), &cat).is_ok());
        cat.cfg.max_bytes = low - 1;
        assert_eq!(run(&select(three(&cat)[..1].to_vec(), &COLS[..3], vec![], None, Format::Json), &cat).unwrap_err().code, "sql_budget");
        // The same Store twice holds no more bytes and gives the same body, but the second aggregate needs room for its groups
        let revenue = three(&cat).swap_remove(0).0;
        cat.cfg.max_bytes = low;
        let twice = vec![(revenue.clone(), ReadAgg::Sum), (revenue, ReadAgg::Max)];
        assert_eq!(run(&select(twice, &COLS[..3], vec![], None, Format::Json), &cat).unwrap_err().code, "sql_budget");
    }

    #[test]
    fn the_budget_counts_a_long_escaped_alias_also_with_limit_0() {
        let mut cat = catalog();
        let aggs = three(&cat)[..1].to_vec();
        let sel = |alias: &str| {
            let mut s = select(aggs.clone(), &COLS[..3], vec![], Some(0), Format::Json);
            s.columns[2].0 = alias.to_string();
            s
        };
        let (mut low, mut high) = (0, 1 << 30);
        while low < high {
            let mid = (low + high) / 2;
            cat.cfg.max_bytes = mid;
            if run(&sel("rev"), &cat).is_ok() {
                high = mid;
            } else {
                low = mid + 1;
            }
        }
        let alias = "\"".repeat(1 << 20);
        cat.cfg.max_bytes = low + (3 << 19);
        assert_eq!(run(&sel(&alias), &cat).unwrap_err().code, "sql_budget");
        cat.cfg.max_bytes = low + (5 << 19);
        assert_eq!(run(&sel(&alias), &cat).unwrap().len(), 52 + (2 << 20));
    }

    #[test]
    fn the_size_estimate_is_not_less_than_the_body() {
        assert_eq!(format!("{:?}", -2.2250738585072014e-308f64).len(), 24);
        let t = Table {
            keys: vec![0, 4, 11],
            vals: vec![vec![-2.2250738585072014e-308, f64::NEG_INFINITY, 0.0], vec![f64::NAN, -0.00012345678901234567, 0.0]],
            valid: vec![None, Some(vec![true, true, false])],
        };
        let member = |j: usize, k: u64| if j == 0 { (k / 3) as u32 } else { (k % 3) as u32 };
        let rows: Vec<u32> = vec![2, 0, 1];
        let columns = [("\u{1}\"\\", Col::Dim(0)), ("é", Col::Dim(1)), ("a", Col::Agg(0)), ("b", Col::Agg(1)), ("m", Col::Dim(0))];
        for format in [Format::Json, Format::Arrow] {
            let mut sel = select(vec![], &columns, vec![], None, format);
            sel.seq = u64::MAX;
            sel.names[0] = vec!["\u{1}\u{1}", "\"\"", "\\", "é"];
            let size = encoded_size(&sel, &t, &rows, member);
            let body = match format {
                Format::Json => json(&sel, &t, &rows, member, size),
                Format::Arrow => arrow(&sel, &t, &rows, member).unwrap(),
            };
            let limit = if format == Format::Arrow { 2 * size } else { size };
            assert!(body.len() <= limit, "{format:?}: {} > {limit}", body.len());
        }
    }

    #[test]
    fn too_many_cells_is_an_error() {
        let cat = catalog();
        let mut sel = select(three(&cat), &COLS, vec![], None, Format::Json);
        sel.max_cells = 19;
        assert_eq!(run(&sel, &cat).unwrap_err(), Diag::new("sql_too_large").arg("rows", Arg::Int(4)).arg("cells", Arg::Int(20)).arg("max", Arg::Int(19)));
        sel.limit = Some(3);
        assert_eq!(text(run(&sel, &cat).unwrap()).matches('[').count(), 5, "columns and 3 rows");
    }

    #[test]
    fn json_escapes_names_and_writes_large_numbers_with_an_exponent() {
        let mut out = String::new();
        quote(&mut out, "a\"b\\c\n\u{1}é");
        assert_eq!(out, r#""a\"b\\c\u000a\u0001é""#);
        let cat = catalog();
        let big = store(&cat, &[(0, 0, 1e20), (1, 0, -2.5e-8), (2, 0, f64::NEG_INFINITY)]);
        let mut sel = select(vec![(big, ReadAgg::Sum)], &[("v", Col::Agg(0))], vec![], None, Format::Json);
        sel.keep = vec![1];
        sel.names = vec![PRODUCTS.to_vec()];
        sel.ranks = vec![None];
        assert_eq!(text(run(&sel, &cat).unwrap()), r#"{"seq":7,"columns":["v"],"rows":[[1e20],[-2.5e-8],["-Infinity"]]}"#);
    }

    #[test]
    fn arrow_has_dictionary_dimensions_and_nullable_aggregates() {
        let cat = catalog();
        let body = run(&select(three(&cat), &COLS[..4], vec![], Some(10), Format::Arrow), &cat).unwrap();
        let r = StreamReader::try_new(body.as_slice(), None).unwrap();
        let schema = r.schema();
        assert_eq!(schema.metadata()["seq"], "7");
        let b: Vec<RecordBatch> = r.map(|b| b.unwrap()).collect();
        assert_eq!(b.len(), 1);
        let b = &b[0];
        let names: Vec<&str> = schema.fields().iter().map(|f| f.name().as_str()).collect();
        assert_eq!(names, ["Month", "Product", "rev", "max(Cost)"]);
        let month = b.column(0).as_dictionary::<Int32Type>();
        assert_eq!(month.keys().values().to_vec(), [0, 1, 1, 2]);
        assert_eq!(month.values().as_string::<i32>().iter().flatten().collect::<Vec<_>>(), MONTHS);
        assert_eq!(b.column(1).as_dictionary::<Int32Type>().keys().values().to_vec(), [0, 1, 2, 0]);
        let cost = b.column(3).as_primitive::<Float64Type>();
        assert_eq!((0..4).map(|i| cost.is_valid(i).then(|| cost.value(i))).collect::<Vec<_>>(), [Some(3.0), None, None, Some(4.0)]);
        assert_eq!(b.column(2).null_count(), 1);
    }
}
