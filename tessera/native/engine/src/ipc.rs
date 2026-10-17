//! The Arrow IPC stream of a `POST /query` result (docs/server.md). It is one table with the columns `metric`,
//! one column for each dimension of the query (named by the dimension id), and `value`.

use crate::Result;
use arrow_array::builder::StringDictionaryBuilder;
use arrow_array::types::Int32Type;
use arrow_array::{ArrayRef, DictionaryArray, Float64Array, Int32Array, RecordBatch, StringArray};
use arrow_ipc::writer::StreamWriter;
use arrow_schema::{DataType, Field, Schema};
use std::collections::HashMap;
use std::sync::Arc;

/// The rows of one Metric. `coords[j]` has the member ids of dimension j of the query, or is None if the Metric
/// does not have that dimension. Then the column of dimension j is null for these rows.
pub struct Part<S> {
    pub metric: String,
    pub coords: Vec<Option<Vec<S>>>,
    pub values: Vec<f64>,
}

fn ipc_err(e: impl std::fmt::Display) -> String {
    format!("Arrow IPC: {e}")
}

/// Encode the parts as one record batch, with `seq` in the schema metadata.
pub fn query_stream<S: AsRef<str>>(seq: u64, dims: &[String], parts: &[Part<S>]) -> Result<Vec<u8>> {
    for p in parts {
        if p.coords.len() != dims.len() || p.coords.iter().flatten().any(|c| c.len() != p.values.len()) {
            return Err("クエリの結果の列がそろっていない".into());
        }
    }
    let text = || DataType::Dictionary(Box::new(DataType::Int32), Box::new(DataType::Utf8));
    let mut fields = vec![Field::new("metric", text(), false)];
    fields.extend(dims.iter().map(|d| Field::new(d, text(), true)));
    fields.push(Field::new("value", DataType::Float64, false));
    let schema = Arc::new(Schema::new_with_metadata(fields, HashMap::from([("seq".to_string(), seq.to_string())])));

    let keys = Int32Array::from_iter_values(parts.iter().enumerate().flat_map(|(i, p)| std::iter::repeat_n(i as i32, p.values.len())));
    let names = StringArray::from_iter_values(parts.iter().map(|p| &p.metric));
    let mut columns: Vec<ArrayRef> = vec![Arc::new(DictionaryArray::try_new(keys, Arc::new(names)).map_err(ipc_err)?)];
    for j in 0..dims.len() {
        let mut b = StringDictionaryBuilder::<Int32Type>::new();
        for p in parts {
            match &p.coords[j] {
                Some(c) => c.iter().for_each(|x| b.append_value(x.as_ref())),
                None => b.append_nulls(p.values.len()),
            }
        }
        columns.push(Arc::new(b.finish()));
    }
    columns.push(Arc::new(Float64Array::from_iter_values(parts.iter().flat_map(|p| p.values.iter().copied()))));

    let batch = RecordBatch::try_new(schema.clone(), columns).map_err(ipc_err)?;
    let mut w = StreamWriter::try_new(Vec::new(), &schema).map_err(ipc_err)?;
    w.write(&batch).map_err(ipc_err)?;
    w.into_inner().map_err(ipc_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow_array::cast::AsArray;
    use arrow_array::Array;
    use arrow_ipc::reader::StreamReader;

    /// The text columns of a row (metric and the dimensions) and its value.
    type Row = (Vec<Option<String>>, f64);

    /// Decode a stream into (metadata, column names, rows).
    fn decode(data: &[u8]) -> (HashMap<String, String>, Vec<String>, Vec<Row>) {
        let r = StreamReader::try_new(data, None).unwrap();
        let schema = r.schema();
        let batches: Vec<RecordBatch> = r.map(|b| b.unwrap()).collect();
        assert_eq!(batches.len(), 1);
        let b = &batches[0];
        let last = b.num_columns() - 1;
        let rows = (0..b.num_rows())
            .map(|i| {
                let text = (0..last)
                    .map(|j| {
                        let d = b.column(j).as_dictionary::<Int32Type>();
                        d.is_valid(i).then(|| d.values().as_string::<i32>().value(d.keys().value(i) as usize).to_string())
                    })
                    .collect();
                (text, b.column(last).as_primitive::<arrow_array::types::Float64Type>().value(i))
            })
            .collect();
        (schema.metadata().clone().into(), schema.fields().iter().map(|f| f.name().clone()).collect(), rows)
    }

    #[test]
    fn two_metrics_and_a_missing_dimension() {
        let dims = ["d-month".to_string(), "d-product".to_string()];
        let parts = [
            Part { metric: "m-stock".into(), coords: vec![Some(vec!["jan", "feb"]), Some(vec!["p1", "p1"])], values: vec![1.5, f64::NAN] },
            Part { metric: "m-total".into(), coords: vec![Some(vec!["jan"]), None], values: vec![8000.0] },
        ];
        let (meta, names, rows) = decode(&query_stream(12, &dims, &parts).unwrap());
        assert_eq!(meta, HashMap::from([("seq".to_string(), "12".to_string())]));
        assert_eq!(names, ["metric", "d-month", "d-product", "value"]);
        let s = |x: &str| Some(x.to_string());
        assert_eq!(rows[0], (vec![s("m-stock"), s("jan"), s("p1")], 1.5));
        assert_eq!(rows[1].0, vec![s("m-stock"), s("feb"), s("p1")]);
        assert!(rows[1].1.is_nan());
        assert_eq!(rows[2], (vec![s("m-total"), s("jan"), None], 8000.0));
        assert_eq!(rows.len(), 3);
    }

    #[test]
    fn no_metrics() {
        let (meta, names, rows) = decode(&query_stream::<&str>(0, &[], &[]).unwrap());
        assert_eq!((meta["seq"].as_str(), names, rows.len()), ("0", vec!["metric".to_string(), "value".to_string()], 0));
    }

    #[test]
    fn columns_of_different_lengths_are_an_error() {
        let parts = [Part { metric: "m".into(), coords: vec![Some(vec!["a"])], values: vec![1.0, 2.0] }];
        assert!(query_stream(0, &["d".to_string()], &parts).is_err());
    }
}
