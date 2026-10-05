"""Model の保存と読み込み。

ディレクトリに次の 2 種類を置く。
- model.json: 軸（番号の順のメンバー、ID、並び順、順序、プロパティ）と Metric（ID、軸、値の種類、分割軸、式の文字列）。
  Each dimension, member, property, and Metric also has its UUID ("uuid", "member_uuids"), and "tombstones" has
  the UUIDs of removed objects
- inputs.<Metric の ID>.parquet: 入力 Metric ごとに 1 つ。軸ごとのメンバー番号の列（d<軸の ID>）と、
  値の列 v（number は Float64、boolean は Boolean、メンバー型はメンバー番号の UInt32）

メンバー番号は model.json のメンバーの列での位置。メンバー型の値も番号で持つ。
位置ごとのメンバーの ID も model.json に持つので、番号から変わらない ID を引ける。
並び順が番号の順と違う軸は、並び順に並べた番号の列（member_order）も持つ。
計算 Metric の値は保存せず、読み込み後の最初の再計算で求め直す。
Parquet の読み書きは nanashi_core が行う（参照実装のエンジンでも）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .engine import Store, default_engine
from .parser import to_formula

FORMAT_VERSION = 5


def save(model, path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, data in dump(model).items():
        (path / name).write_bytes(data)


def dump(model) -> dict[str, bytes]:
    """save で置くファイルの名前 -> 中身（オブジェクトストレージなど、ディレクトリ以外に置くとき）。"""
    dims = []
    for d in model.dimensions.values():
        props = {prop: {"target": target, "mapping": mapping, "id": d.property_ids[prop],
                        "uuid": model.uuid_of(d.property_ids[prop])}
                 for prop, (target, mapping) in d.properties.items()}
        dims.append({"name": d.name, "id": d.id, "uuid": model.uuid_of(d.id), "members": d.members,
                     "member_ids": d.ids, "member_uuids": [model.uuid_of(i) for i in d.ids],
                     "ordered": d.ordered, "properties": props})
        if d.rank_table() is not None:
            dims[-1]["member_order"] = d.order()
    metrics = []
    files: dict[str, bytes] = {}
    for m in model.metrics.values():
        handle = model.ids[m.id]  # the format still names a Metric by its handle (unit 4 of issue 68 changes this)
        metrics.append({"name": m.name, "id": handle, "uuid": m.id, "dims": list(m.dims), "kind": m.kind,
                        "partition": m.partition,
                        "formula": None if m.written is None else to_formula(m.written, model),
                        "overridable": m.overridable})
        if m.formula is not None:
            continue
        # Rust なら、格納データから GIL を外して、Python のオブジェクトを作らずに書く
        files[input_file(handle)] = model.engine.to_parquet(
            model._values[m.id], m.dims, m.kind, model,
            {"nanashi": json.dumps({"format": FORMAT_VERSION, "metric": handle})})
    meta = {"format": FORMAT_VERSION, "next_id": model._next_id, "dimensions": dims, "metrics": metrics,
            "tombstones": sorted(model.tombstones),
            "options": {"auto_layout": model.auto_layout, "delta_aggregation": model.delta_aggregation,
                        "max_cells": model.max_cells}}
    return {"model.json": json.dumps(meta, ensure_ascii=False, indent=1).encode(), **files}


def input_file(metric_id: int) -> str:
    return f"inputs.{metric_id}.parquet"


def load(path, engine: Store | None = None):
    path = Path(path)
    return read(lambda name: (path / name).read_bytes(), engine)


def read(file: Callable[[str], bytes], engine: Store | None = None):
    """save の形式を、ファイルの名前から中身を返す file で読む（ディレクトリ以外に置いたとき）。"""
    from .model import Model

    meta = json.loads(file("model.json"))
    if meta.get("format") != FORMAT_VERSION:
        raise ValueError(f"対応していない保存形式: {meta.get('format')}")
    engine = engine if engine is not None else default_engine()
    m = Model(engine=engine, **meta["options"])
    for d in meta["dimensions"]:
        m.add_dimension(d["name"], d["members"], ordered=d["ordered"])
        # Parquet column names are dimension IDs, so restore the saved IDs before the inputs are read
        m.dimensions[d["name"]].id = d["id"]
        m.dimensions[d["name"]].set_ids(d["member_ids"])
        if "member_order" in d:
            m.dimensions[d["name"]].set_order(d["member_order"])
    for d in meta["dimensions"]:  # 参照先の軸がそろってからプロパティを付ける
        for prop, spec in d["properties"].items():
            m.add_property(d["name"], prop, spec["target"], spec["mapping"])
    formulas = [spec for spec in meta["metrics"] if spec["formula"] is not None]
    for spec in meta["metrics"]:
        if spec["formula"] is not None:
            continue
        dims, kind = tuple(spec["dims"]), spec["kind"]
        storage = engine.from_parquet(file(input_file(spec["id"])), dims, kind, m, spec["partition"])
        m.add_input(spec["name"], dims, kind=kind, storage=storage, partition=spec["partition"], id=spec["uuid"])
    for spec in formulas:  # an empty input first: a formula can refer to a Metric that comes later in the file
        m.add_input(spec["name"], spec["dims"], kind=spec["kind"], partition=spec["partition"], id=spec["uuid"])
    for spec in formulas:
        m.add_formula(spec["name"], spec["dims"], spec["formula"], kind=spec["kind"],
                      partition=spec["partition"], overridable=spec["overridable"], id=spec["uuid"])
    # Restore the UUIDs of the dimensions, members and properties, and the handles, in place of the ones that
    # the load made
    m.ids, m._uuids, m.tombstones = {}, {}, set(meta["tombstones"])
    for d in meta["dimensions"]:
        m._bind(d["uuid"], d["id"])
        for u, i in zip(d["member_uuids"], d["member_ids"]):
            m._bind(u, i)
        for prop, spec in d["properties"].items():
            m.dimensions[d["name"]].property_ids[prop] = spec["id"]
            m._bind(spec["uuid"], spec["id"])
    for spec in meta["metrics"]:
        m._bind(spec["uuid"], spec["id"])
    m._next_id = meta["next_id"]
    return m
