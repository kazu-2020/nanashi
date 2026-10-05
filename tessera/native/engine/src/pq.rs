//! 軸ごとのメンバー番号の列と値の列を、Parquet のバイト列にする・戻す（保存と読み込み用）。
//!
//! 列は、軸ごとのメンバー番号（UInt32、呼び出し側が決めた名前）と、値の列 v。
//! v の型は値の種類に合わせる（number は Float64、boolean は Boolean、member はメンバー番号の UInt32）。
//! どの列も null を持たない。ファイルの置き場所は呼び出し側が決め、ここではバイト列だけを扱う。

use crate::Result;
use arrow_array::{Array, ArrayRef, BooleanArray, Float64Array, RecordBatch, StringArray, UInt32Array};
use arrow_schema::{DataType, Field, Schema};
use bytes::Bytes;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::{ArrowWriter, ProjectionMask};
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;
use std::collections::HashMap;
use std::sync::Arc;

pub const VALUE: &str = "v";

/// The kind of a value. A member value is a member number (UInt32) in stored data and a member id (Utf8) in a change.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Value {
    Num,
    Bool,
    Member,
}

impl Value {
    pub fn parse(s: &str) -> Result<Value> {
        match s {
            "number" => Ok(Value::Num),
            "boolean" => Ok(Value::Bool),
            "member" => Ok(Value::Member),
            _ => Err(format!("値の種類は number、boolean、member のどれか（{s}）")),
        }
    }

    /// The Arrow type of the value. `member` is the type of a member value.
    fn data_type(self, member: DataType) -> DataType {
        match self {
            Value::Num => DataType::Float64,
            Value::Bool => DataType::Boolean,
            Value::Member => member,
        }
    }
}

fn pq_err(e: impl std::fmt::Display) -> String {
    format!("Parquet: {e}")
}

/// Write the columns to Parquet bytes with ZSTD compression. meta goes into the footer.
fn encode(fields: Vec<Field>, arrays: Vec<ArrayRef>, meta: &[(String, String)]) -> Result<Vec<u8>> {
    let schema = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays).map_err(pq_err)?;
    let kv = meta.iter().map(|(k, v)| KeyValue::new(k.clone(), v.clone())).collect();
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::try_new(1).map_err(pq_err)?))
        .set_key_value_metadata(Some(kv))
        .build();
    let mut buf = Vec::new();
    let mut w = ArrowWriter::try_new(&mut buf, schema, Some(props)).map_err(pq_err)?;
    w.write(&batch).map_err(pq_err)?;
    w.close().map_err(pq_err)?;
    Ok(buf)
}

/// 列を Parquet のバイト列にする。meta はフッターに入れるキーと値。
pub fn write(names: &[String], cols: Vec<Vec<u32>>, values: Vec<f64>, value: Value, meta: &[(String, String)]) -> Result<Vec<u8>> {
    if names.len() != cols.len() {
        return Err("列の名前の数が軸の数と合わない".into());
    }
    if cols.iter().any(|c| c.len() != values.len()) {
        return Err("列の長さがそろっていない".into());
    }
    let mut fields: Vec<Field> = names.iter().map(|n| Field::new(n, DataType::UInt32, false)).collect();
    fields.push(Field::new(VALUE, value.data_type(DataType::UInt32), false));
    let mut arrays: Vec<ArrayRef> = cols.into_iter().map(|c| Arc::new(UInt32Array::from(c)) as ArrayRef).collect();
    arrays.push(match value {
        Value::Num => Arc::new(Float64Array::from(values)),
        Value::Bool => Arc::new(values.iter().map(|v| Some(*v != 0.0)).collect::<BooleanArray>()),
        Value::Member => Arc::new(values.iter().map(|v| *v as u32).collect::<UInt32Array>()),
    });
    encode(fields, arrays, meta)
}

/// 列を名前で選んで読む。expected の各列（名前と型）がファイルのどこにあってもよく、ほかの列は読まない
/// （あとの版が列を足しても、前の版が読める）。見つからないか型が違えばエラー。返すのは、読んだ組の
/// 列の中での、expected の各列の位置。
fn project(builder: ParquetRecordBatchReaderBuilder<Bytes>, expected: &[(&str, DataType)]) -> Result<(ParquetRecordBatchReaderBuilder<Bytes>, Vec<usize>)> {
    let schema = builder.schema().clone();
    let mut roots = Vec::with_capacity(expected.len());
    for (name, t) in expected {
        match schema.fields().iter().position(|f| f.name() == name) {
            Some(i) if schema.field(i).data_type() == t => roots.push(i),
            _ => {
                let show = |xs: Vec<String>| xs.join(", ");
                return Err(format!(
                    "Parquet の列が合わない。期待: [{}]、実際: [{}]",
                    show(expected.iter().map(|(n, t)| format!("{n}: {t}")).collect()),
                    show(schema.fields().iter().map(|f| format!("{}: {}", f.name(), f.data_type())).collect()),
                ));
            }
        }
    }
    let mut sorted = roots.clone();
    sorted.sort_unstable();
    let at = roots.iter().map(|r| sorted.binary_search(r).unwrap()).collect();
    let mask = ProjectionMask::roots(builder.parquet_schema(), sorted);
    Ok((builder.with_projection(mask), at))
}

/// Parquet のバイト列を列に戻す。列は名前で選ぶ（並び順は問わず、ほかの列は読まない）。
/// 列の型が names と value に合い、メンバー番号が軸の大きさ（sizes）未満でなければエラーにする。
pub fn read(data: Bytes, names: &[String], value: Value, sizes: &[u32]) -> Result<(Vec<Vec<u32>>, Vec<f64>)> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(data).map_err(pq_err)?;
    let expected: Vec<(&str, DataType)> = names
        .iter()
        .map(|n| (n.as_str(), DataType::UInt32))
        .chain(std::iter::once((VALUE, value.data_type(DataType::UInt32))))
        .collect();
    let (builder, at) = project(builder, &expected)?;
    let rows = builder.metadata().file_metadata().num_rows() as usize;
    let mut cols: Vec<Vec<u32>> = names.iter().map(|_| Vec::with_capacity(rows)).collect();
    let mut values = Vec::with_capacity(rows);
    for batch in builder.build().map_err(pq_err)? {
        let batch = batch.map_err(pq_err)?;
        if batch.columns().iter().any(|c| c.null_count() > 0) {
            return Err("Parquet の列に空の値がある".into());
        }
        for (j, col) in cols.iter_mut().enumerate() {
            let a = batch.column(at[j]).as_any().downcast_ref::<UInt32Array>().unwrap();
            if let Some(&m) = a.values().iter().find(|&&m| m >= sizes[j]) {
                return Err(format!("{} のメンバー番号 {m} が軸の大きさ {} を超える", names[j], sizes[j]));
            }
            col.extend_from_slice(a.values());
        }
        let v = batch.column(at[names.len()]);
        match value {
            Value::Num => values.extend_from_slice(v.as_any().downcast_ref::<Float64Array>().unwrap().values()),
            Value::Bool => {
                let b = v.as_any().downcast_ref::<BooleanArray>().unwrap();
                values.extend(b.values().iter().map(|x| if x { 1.0 } else { 0.0 }));
            }
            Value::Member => {
                let u = v.as_any().downcast_ref::<UInt32Array>().unwrap();
                values.extend(u.values().iter().map(|&x| x as f64));
            }
        }
    }
    Ok((cols, values))
}

/// The input cell changes of one Metric. A coordinate is a member id (a string), and so is a member-type value.
/// A column holds, for each row, the index of the id in its table (`ids[j]`). A member-type value is the index of
/// the id in `value_ids`, as f64 like the other values. So a block of many rows does not keep one String for each cell.
#[derive(Clone, Debug, PartialEq)]
pub struct Changes {
    pub cols: Vec<Vec<u32>>,    // one column for each dimension: the index of the member id in ids[j]
    pub ids: Vec<Vec<String>>,  // one table for each dimension: the member ids that the column uses
    pub old: Vec<Option<f64>>,
    pub new: Vec<Option<f64>>,
    pub value_ids: Vec<String>, // the member ids of a member-type value (empty for the other kinds)
    pub kind: Value,
}

impl Changes {
    pub fn len(&self) -> usize {
        self.new.len()
    }

    pub fn is_empty(&self) -> bool {
        self.new.is_empty()
    }

    /// The member ids of the coordinates of row r.
    pub fn key(&self, r: usize) -> Vec<&str> {
        self.cols.iter().zip(&self.ids).map(|(c, t)| t[c[r] as usize].as_str()).collect()
    }

    /// The member id of a member-type value.
    pub fn value_id(&self, v: f64) -> &str {
        &self.value_ids[v as usize]
    }
}

/// A table of ids in the order of first use, with the index of each id.
#[derive(Default)]
pub struct Interner {
    pub table: Vec<String>,
    index: HashMap<String, u32>,
}

impl Interner {
    pub fn intern(&mut self, id: &str) -> u32 {
        if let Some(&i) = self.index.get(id) {
            return i;
        }
        let i = self.table.len() as u32;
        self.table.push(id.to_string());
        self.index.insert(id.to_string(), i);
        i
    }
}

fn id_array(col: &[u32], ids: &[String]) -> ArrayRef {
    Arc::new(StringArray::from_iter_values(col.iter().map(|&i| ids[i as usize].as_str())))
}

fn change_array(c: &Changes, xs: &[Option<f64>]) -> ArrayRef {
    match c.kind {
        Value::Num => Arc::new(Float64Array::from(xs.to_vec())),
        Value::Bool => Arc::new(xs.iter().map(|v| v.map(|x| x != 0.0)).collect::<BooleanArray>()),
        Value::Member => Arc::new(xs.iter().map(|v| v.map(|x| c.value_id(x))).collect::<StringArray>()),
    }
}

fn change_values(a: &dyn Array, out: &mut Vec<Option<f64>>, ids: &mut Interner) {
    if let Some(f) = a.as_any().downcast_ref::<Float64Array>() {
        out.extend(f.iter());
    } else if let Some(b) = a.as_any().downcast_ref::<BooleanArray>() {
        out.extend(b.iter().map(|v| v.map(|x| if x { 1.0 } else { 0.0 })));
    } else if let Some(s) = a.as_any().downcast_ref::<StringArray>() {
        out.extend(s.iter().map(|v| v.map(|x| ids.intern(x) as f64)));
    }
}

/// Write the changes to Parquet bytes. The columns are the dimensions (names, Utf8 member ids), old and new.
pub fn write_changes(names: &[String], c: &Changes, meta: &[(String, String)]) -> Result<Vec<u8>> {
    if names.len() != c.cols.len() {
        return Err("列の名前の数が軸の数と合わない".into());
    }
    let mut fields: Vec<Field> = names.iter().map(|n| Field::new(n, DataType::Utf8, false)).collect();
    fields.push(Field::new("old", c.kind.data_type(DataType::Utf8), true));
    fields.push(Field::new("new", c.kind.data_type(DataType::Utf8), true));
    let mut arrays: Vec<ArrayRef> = c.cols.iter().zip(&c.ids).map(|(col, ids)| id_array(col, ids)).collect();
    arrays.push(change_array(c, &c.old));
    arrays.push(change_array(c, &c.new));
    encode(fields, arrays, meta)
}

/// Read bytes that write_changes wrote, as the names of the dimension columns and the changes. The columns are
/// selected by name: old, new, and the other Utf8 columns are the dimensions (other columns are not read).
pub fn read_changes(data: Bytes) -> Result<(Vec<String>, Changes)> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(data).map_err(pq_err)?;
    let fields = builder.schema().fields().clone();
    let bad = || format!("Parquet の列が変更の形でない: [{}]", fields.iter().map(|f| format!("{}: {}", f.name(), f.data_type())).collect::<Vec<_>>().join(", "));
    let find = |n: &str| fields.iter().find(|f| f.name() == n).map(|f| f.data_type().clone());
    let (Some(old_t), Some(new_t)) = (find("old"), find("new")) else { return Err(bad()) };
    if old_t != new_t {
        return Err(bad());
    }
    let kind = [Value::Num, Value::Bool, Value::Member].into_iter().find(|&k| k.data_type(DataType::Utf8) == new_t).ok_or_else(bad)?;
    let names: Vec<String> = fields
        .iter()
        .filter(|f| f.name() != "old" && f.name() != "new" && *f.data_type() == DataType::Utf8)
        .map(|f| f.name().clone())
        .collect();
    let mut expected: Vec<(&str, DataType)> = names.iter().map(|n| (n.as_str(), DataType::Utf8)).collect();
    expected.push(("old", old_t.clone()));
    expected.push(("new", new_t.clone()));
    let (builder, at) = project(builder, &expected)?;
    let n = names.len();
    let rows = builder.metadata().file_metadata().num_rows() as usize;
    let mut cols: Vec<Vec<u32>> = names.iter().map(|_| Vec::with_capacity(rows)).collect();
    let mut tables: Vec<Interner> = names.iter().map(|_| Interner::default()).collect();
    let mut values = Interner::default();
    let (mut old, mut new) = (Vec::with_capacity(rows), Vec::with_capacity(rows));
    for batch in builder.build().map_err(pq_err)? {
        let batch = batch.map_err(pq_err)?;
        for j in 0..n {
            let a = batch.column(at[j]).as_any().downcast_ref::<StringArray>().unwrap();
            if a.null_count() > 0 {
                return Err("Parquet の軸の列に空の値がある".into());
            }
            cols[j].extend(a.iter().map(|s| tables[j].intern(s.unwrap())));
        }
        change_values(batch.column(at[n]).as_ref(), &mut old, &mut values);
        change_values(batch.column(at[n + 1]).as_ref(), &mut new, &mut values);
    }
    let ids = tables.into_iter().map(|t| t.table).collect();
    Ok((names, Changes { cols, ids, old, new, value_ids: values.table, kind }))
}

/// フッターのキーと値だけを読む（本体は読まない）。
pub fn metadata(data: Bytes) -> Result<Vec<(String, String)>> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(data).map_err(pq_err)?;
    let kv = builder.metadata().file_metadata().key_value_metadata();
    Ok(kv
        .map(|kv| {
            kv.iter()
                .filter(|e| e.key != "ARROW:schema") // Arrow の型を残すために書き手が足す
                .map(|e| (e.key.clone(), e.value.clone().unwrap_or_default()))
                .collect()
        })
        .unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow_array::Int64Array;

    fn names(n: usize) -> Vec<String> {
        (0..n).map(|i| format!("d{i}")).collect()
    }

    #[test]
    fn round_trip_each_value_kind() {
        let cols = vec![vec![0, 1, 2, 2], vec![3, 3, 0, 1]];
        for (value, vals) in [
            (Value::Num, vec![1.5, -2.0, 0.0, 1e300]),
            (Value::Bool, vec![1.0, 0.0, 0.0, 1.0]),
            (Value::Member, vec![0.0, 4.0, 2.0, 4.0]),
        ] {
            let meta = vec![("k".to_string(), "値".to_string())];
            let buf = write(&names(2), cols.clone(), vals.clone(), value, &meta).unwrap();
            let (c, v) = read(Bytes::from(buf.clone()), &names(2), value, &[3, 4]).unwrap();
            assert_eq!((c, v), (cols.clone(), vals));
            assert_eq!(metadata(Bytes::from(buf)).unwrap(), meta);
        }
    }

    #[test]
    fn columns_are_chosen_by_name() {
        // あとの版が列を足したり、並びを変えたりしても読める
        let schema = Arc::new(Schema::new(vec![
            Field::new("note", DataType::Int64, false),
            Field::new(VALUE, DataType::Float64, false),
            Field::new("d1", DataType::UInt32, false),
            Field::new("d0", DataType::UInt32, false),
        ]));
        let arrays: Vec<ArrayRef> = vec![
            Arc::new(Int64Array::from(vec![7, 8])),
            Arc::new(Float64Array::from(vec![1.5, 2.5])),
            Arc::new(UInt32Array::from(vec![3, 2])),
            Arc::new(UInt32Array::from(vec![0, 1])),
        ];
        let batch = RecordBatch::try_new(schema.clone(), arrays).unwrap();
        let mut buf = Vec::new();
        let mut w = ArrowWriter::try_new(&mut buf, schema, None).unwrap();
        w.write(&batch).unwrap();
        w.close().unwrap();
        let (cols, vals) = read(Bytes::from(buf.clone()), &names(2), Value::Num, &[2, 4]).unwrap();
        assert_eq!((cols, vals), (vec![vec![0, 1], vec![3, 2]], vec![1.5, 2.5]));
        let err = read(Bytes::from(buf), &names(3), Value::Num, &[2, 4, 4]).unwrap_err();
        assert!(err.contains("列が合わない"), "{err}");
    }

    fn strs(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn round_trip_changes() {
        for (kind, old, new) in [
            (Value::Num, vec![Some(1.5), None, Some(-3.0)], vec![Some(2.5), Some(0.0), None]),
            (Value::Bool, vec![Some(1.0), None, Some(0.0)], vec![Some(0.0), Some(1.0), None]),
            (Value::Member, vec![Some(0.0), None, Some(1.0)], vec![None, Some(2.0), Some(1.0)]),
        ] {
            // the ids are in the order of first use, as read_changes makes them
            let value_ids = if kind == Value::Member { strs(&["p", "q", "r"]) } else { Vec::new() };
            let c = Changes { cols: vec![vec![0, 1, 2], vec![0, 0, 1]], ids: vec![strs(&["a", "b", "c"]), strs(&["x", "y"])], old, new, value_ids, kind };
            let buf = write_changes(&["d3".into(), "d8".into()], &c, &[]).unwrap();
            assert_eq!(read_changes(Bytes::from(buf)).unwrap(), (vec!["d3".to_string(), "d8".to_string()], c));
        }
        let store = Bytes::from(write(&names(1), vec![vec![0]], vec![1.0], Value::Num, &[]).unwrap());
        assert!(read_changes(store).unwrap_err().contains("変更の形でない"));
    }

    #[test]
    fn empty_store() {
        let buf = write(&names(1), vec![vec![]], vec![], Value::Num, &[]).unwrap();
        let (c, v) = read(Bytes::from(buf), &names(1), Value::Num, &[0]).unwrap();
        assert_eq!((c, v), (vec![vec![]], vec![]));
    }

    #[test]
    fn rejects_mismatch() {
        let buf = Bytes::from(write(&names(2), vec![vec![0], vec![5]], vec![1.0], Value::Num, &[]).unwrap());
        let e = read(buf.clone(), &names(2), Value::Bool, &[1, 9]).unwrap_err();
        assert!(e.contains("列が合わない"), "{e}");
        let e = read(buf.clone(), &["d0".into(), "x".into()], Value::Num, &[1, 9]).unwrap_err();
        assert!(e.contains("列が合わない"), "{e}");
        let e = read(buf, &names(2), Value::Num, &[1, 5]).unwrap_err();
        assert!(e.contains("メンバー番号 5"), "{e}");
        assert!(read(Bytes::from_static(b"broken"), &names(2), Value::Num, &[1, 9]).is_err());
    }
}
