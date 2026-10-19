//! A segment write that fails (the directory is gone) leaves the store with its old base and delta.

use nanashi_engine::{Catalog, Config, DimInfo, Kind, Store, Tier};

#[test]
fn a_failed_segment_write_keeps_the_old_store() {
    let dir = std::env::temp_dir().join(format!("nanashi-write-failure-{}", std::process::id()));
    let tier = Tier::install(&dir, 1 << 16, 2).unwrap();
    let cfg = Config { compact_min: 4, delta_max: 4, seg_rows: 16, tier: Some(tier), ..Config::default() };
    let dims = vec![DimInfo { size: 64, ordered: false, name: "d".into() }];
    let cat = Catalog { dims, maps: Vec::new(), cfg };
    let cols: Vec<u32> = (0..64).collect();
    let values: Vec<f64> = cols.iter().map(|&m| m as f64).collect();
    let mut s = Store::new(&[0], Some(0), Kind::Num, &cat).unwrap().with_rows(&[&cols], &values).unwrap();
    assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 4, "4 segments of 16 rows");
    // The open files of the old segments stay readable. The next segment file cannot be made
    std::fs::remove_dir_all(&dir).unwrap();
    let mut want = values.clone();
    let failed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        for m in 0..8u32 {
            want[m as usize] = -1.0;
            s.write(&[m], Some(-1.0));
        }
    }))
    .unwrap_err();
    let msg = failed.downcast_ref::<String>().cloned().unwrap_or_default();
    assert!(msg.contains("セグメントファイルを作れない"), "{msg}");
    // The limit of the delta is 4 rows, so the 5th write went into the delta and started the compaction that failed
    assert_eq!(want[4..6], [-1.0, 5.0]);
    assert_eq!(s.rows(), (vec![cols.clone()], want.clone()), "the old base with the delta of the writes");
    // The store works again when the directory is back
    std::fs::create_dir_all(&dir).unwrap();
    s.write(&[9], Some(-2.0));
    want[9] = -2.0;
    assert_eq!(s.rows(), (vec![cols], want));
    drop(s);
    assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 0);
    std::fs::remove_dir(&dir).unwrap();
}
