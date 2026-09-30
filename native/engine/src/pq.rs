//! 軸ごとのメンバー番号の列と値の列を、Parquet のバイト列にする・戻す（保存と読み込み用）。
//!
//! 列は、軸ごとのメンバー番号（UInt32、呼び出し側が決めた名前）と、値の列 v。
//! v の型は値の種類に合わせる（number は Float64、boolean は Boolean、member はメンバー番号の UInt32）。
//! どの列も null を持たない。ファイルの置き場所は呼び出し側が決め、ここではバイト列だけを扱う。

use crate::Result;
use arrow_array::{Array, ArrayRef, BooleanArray, Float64Array, Int64Array, RecordBatch, UInt32Array};
use arrow_schema::{DataType, Field, Schema};
use bytes::Bytes;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::{ArrowWriter, ProjectionMask};
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;
use std::sync::Arc;

pub const VALUE: &str = "v";

/// 値の列の型。
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

    fn data_type(self) -> DataType {
        match self {
            Value::Num => DataType::Float64,
            Value::Bool => DataType::Boolean,
            Value::Member => DataType::UInt32,
        }
    }
}

fn pq_err(e: impl std::fmt::Display) -> String {
    format!("Parquet: {e}")
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
    fields.push(Field::new(VALUE, value.data_type(), false));
    let mut arrays: Vec<ArrayRef> = cols.into_iter().map(|c| Arc::new(UInt32Array::from(c)) as ArrayRef).collect();
    arrays.push(match value {
        Value::Num => Arc::new(Float64Array::from(values)),
        Value::Bool => Arc::new(values.iter().map(|v| Some(*v != 0.0)).collect::<BooleanArray>()),
        Value::Member => Arc::new(values.iter().map(|v| *v as u32).collect::<UInt32Array>()),
    });
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
        .chain(std::iter::once((VALUE, value.data_type())))
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

/// 入力セルの変更の値の型。変更は、軸ごとのメンバーの ID の列（Int64）と、変更前 old と変更後 new
/// （空は null）で持つ。メンバー型の値はメンバーの ID（Int64）。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Change {
    Num,
    Bool,
    Int,
}

impl Change {
    pub fn parse(s: &str) -> Result<Change> {
        match s {
            "number" => Ok(Change::Num),
            "boolean" => Ok(Change::Bool),
            "member" => Ok(Change::Int),
            _ => Err(format!("値の種類は number、boolean、member のどれか（{s}）")),
        }
    }

    fn data_type(self) -> DataType {
        match self {
            Change::Num => DataType::Float64,
            Change::Bool => DataType::Boolean,
            Change::Int => DataType::Int64,
        }
    }

    fn of(t: &DataType) -> Option<Change> {
        [Change::Num, Change::Bool, Change::Int].into_iter().find(|c| c.data_type() == *t)
    }
}

/// 入力セルの変更（1 つの Metric の分）。値は f64 で持つ（真偽値は 0 と 1、メンバーは ID）。
#[derive(Clone, Debug, PartialEq)]
pub struct Changes {
    pub ids: Vec<Vec<i64>>, // 軸ごと
    pub old: Vec<Option<f64>>,
    pub new: Vec<Option<f64>>,
    pub kind: Change,
}

impl Changes {
    pub fn len(&self) -> usize {
        self.new.len()
    }

    pub fn is_empty(&self) -> bool {
        self.new.is_empty()
    }
}

fn change_array(kind: Change, xs: &[Option<f64>]) -> ArrayRef {
    match kind {
        Change::Num => Arc::new(Float64Array::from(xs.to_vec())),
        Change::Bool => Arc::new(xs.iter().map(|v| v.map(|x| x != 0.0)).collect::<BooleanArray>()),
        Change::Int => Arc::new(xs.iter().map(|v| v.map(|x| x as i64)).collect::<Int64Array>()),
    }
}

fn change_values(a: &dyn Array, out: &mut Vec<Option<f64>>) {
    if let Some(f) = a.as_any().downcast_ref::<Float64Array>() {
        out.extend(f.iter());
    } else if let Some(b) = a.as_any().downcast_ref::<BooleanArray>() {
        out.extend(b.iter().map(|v| v.map(|x| if x { 1.0 } else { 0.0 })));
    } else if let Some(i) = a.as_any().downcast_ref::<Int64Array>() {
        out.extend(i.iter().map(|v| v.map(|x| x as f64)));
    }
}

/// 変更を Parquet のバイト列にする（列は names の軸、old、new）。
pub fn write_changes(names: &[String], c: &Changes, meta: &[(String, String)]) -> Result<Vec<u8>> {
    if names.len() != c.ids.len() {
        return Err("列の名前の数が軸の数と合わない".into());
    }
    let mut fields: Vec<Field> = names.iter().map(|n| Field::new(n, DataType::Int64, false)).collect();
    fields.push(Field::new("old", c.kind.data_type(), true));
    fields.push(Field::new("new", c.kind.data_type(), true));
    let mut arrays: Vec<ArrayRef> = c.ids.iter().map(|x| Arc::new(Int64Array::from(x.clone())) as ArrayRef).collect();
    arrays.push(change_array(c.kind, &c.old));
    arrays.push(change_array(c.kind, &c.new));
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

/// 軸の ID の列の名前か（d<軸の ID>、軸の ID を持たない記録は c<位置>）。
fn is_axis_column(name: &str) -> bool {
    let rest = name.strip_prefix('d').or_else(|| name.strip_prefix('c'));
    rest.is_some_and(|r| !r.is_empty() && r.bytes().all(|b| b.is_ascii_digit()))
}

/// write_changes で書いたバイト列を、軸の列の名前と変更に戻す。列は名前で選ぶ（軸の列、old、new。
/// ほかの列は読まない）。
pub fn read_changes(data: Bytes) -> Result<(Vec<String>, Changes)> {
    let builder = ParquetRecordBatchReaderBuilder::try_new(data).map_err(pq_err)?;
    let fields = builder.schema().fields().clone();
    let bad = || format!("Parquet の列が変更の形でない: [{}]", fields.iter().map(|f| format!("{}: {}", f.name(), f.data_type())).collect::<Vec<_>>().join(", "));
    let find = |n: &str| fields.iter().find(|f| f.name() == n).map(|f| f.data_type().clone());
    let (Some(old_t), Some(new_t)) = (find("old"), find("new")) else { return Err(bad()) };
    if old_t != new_t {
        return Err(bad());
    }
    let kind = Change::of(&new_t).ok_or_else(bad)?;
    let names: Vec<String> = fields.iter().filter(|f| is_axis_column(f.name())).map(|f| f.name().clone()).collect();
    let mut expected: Vec<(&str, DataType)> = names.iter().map(|n| (n.as_str(), DataType::Int64)).collect();
    expected.push(("old", old_t.clone()));
    expected.push(("new", new_t.clone()));
    let (builder, at) = project(builder, &expected)?;
    let n = names.len();
    let rows = builder.metadata().file_metadata().num_rows() as usize;
    let mut c = Changes {
        ids: names.iter().map(|_| Vec::with_capacity(rows)).collect(),
        old: Vec::with_capacity(rows),
        new: Vec::with_capacity(rows),
        kind,
    };
    for batch in builder.build().map_err(pq_err)? {
        let batch = batch.map_err(pq_err)?;
        for (j, col) in c.ids.iter_mut().enumerate() {
            let a = batch.column(at[j]);
            if a.null_count() > 0 {
                return Err("Parquet の軸の列に空の値がある".into());
            }
            col.extend_from_slice(a.as_any().downcast_ref::<Int64Array>().unwrap().values());
        }
        change_values(batch.column(at[n]).as_ref(), &mut c.old);
        change_values(batch.column(at[n + 1]).as_ref(), &mut c.new);
    }
    Ok((names, c))
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

    #[test]
    fn round_trip_changes() {
        for (kind, old, new) in [
            (Change::Num, vec![Some(1.5), None, Some(-3.0)], vec![Some(2.5), Some(0.0), None]),
            (Change::Bool, vec![Some(1.0), None, Some(0.0)], vec![Some(0.0), Some(1.0), None]),
            (Change::Int, vec![Some(7.0), None, Some(1e12)], vec![None, Some(3.0), Some(9.0)]),
        ] {
            let c = Changes { ids: vec![vec![10, 11, 12], vec![5, 5, 6]], old, new, kind };
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
