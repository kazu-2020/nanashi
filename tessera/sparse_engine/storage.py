"""Save and load of a Model.

A directory holds 2 kinds of files.
- model.json: the dimensions (the members in number order, the handles, the order, ordered or not, the
  properties) and the Metrics (handle, dimensions, kind, partition dimension, formula text). The dimensions
  and the Metrics in a Metric definition are handles. Each dimension, member, property and Metric also has its
  UUID ("uuid", "member_uuids", the keys of "properties"), and "tombstones" has the UUIDs of removed objects.
  A property mapping is {member UUID: member UUID}.
- inputs.<Metric handle>.parquet: one for each input Metric. A column of member numbers for each dimension
  (d<dimension handle>) and the value column v (Float64 for number, Boolean for boolean, UInt32 member numbers
  for a member type).

A member number is the position in the member list of model.json. A member-type value is also a number.
model.json also has the member handle of each position, so a number gives the constant handle.
A dimension whose order differs from the number order also has the numbers in order (member_order).
The values of a formula Metric are not saved. The first recalculation after a load calculates them again.
nanashi_core reads and writes Parquet (also for the reference engine).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .engine import Store, default_engine
from .parser import to_formula

FORMAT_VERSION = 7


def save(model, path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, data in dump(model).items():
        (path / name).write_bytes(data)


def dump(model) -> dict[str, bytes]:
    """save で置くファイルの名前 -> 中身（オブジェクトストレージなど、ディレクトリ以外に置くとき）。"""
    dims = []
    for d in model.dimensions.values():
        # The dimension ids, "id" and the "target" of a property are handles; "uuid" and the property keys are
        # the UUIDs (unit 4 of issue 68 keeps only the UUIDs)
        props = {prop: {"name": d.property_names[prop], "target": model.ids[target], "mapping": mapping,
                        "id": model.ids[prop]}
                 for prop, (target, mapping) in d.properties.items()}
        dims.append({"name": d.name, "id": model.ids[d.id], "uuid": d.id, "members": d.members,
                     "member_ids": [model.ids[u] for u in d.ids], "member_uuids": d.ids,
                     "ordered": d.ordered, "properties": props})
        if d.rank_table() is not None:
            dims[-1]["member_order"] = d.order()
    metrics = []
    files: dict[str, bytes] = {}
    for m in model.metrics.values():
        handle = model.ids[m.id]  # the format still names a Metric by its handle (unit 4 of issue 68 changes this)
        metrics.append({"name": m.name, "id": handle, "uuid": m.id, "dims": [model.ids[d] for d in m.dims],
                        "kind": {"member": model.ids[m.kind.removeprefix("member:")]} if m.kind.startswith("member:") else m.kind,
                        "partition": None if m.partition is None else model.ids[m.partition],
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
    uuid_of = {}  # handle -> UUID of the dimensions and properties (the file names them by handle)
    for d in meta["dimensions"]:
        uuid_of[d["id"]] = d["uuid"]
        for prop, spec in d["properties"].items():
            uuid_of[spec["id"]] = prop
    for d in meta["dimensions"]:
        m.add_dimension(d["name"], d["members"], ordered=d["ordered"], id=d["uuid"])
        # Parquet column names are dimension handles, so restore the saved handles before the inputs are read
        m._bind(d["uuid"], d["id"])
        m.dimensions[d["uuid"]].set_ids(d["member_uuids"])
        if "member_order" in d:
            m.dimensions[d["uuid"]].set_order(d["member_order"])
    for d in meta["dimensions"]:  # the properties come after all the target dimensions
        for prop, spec in d["properties"].items():
            m.add_property(d["uuid"], spec["name"], uuid_of[spec["target"]], spec["mapping"], id=prop)
    dims_of = lambda spec: tuple(uuid_of[i] for i in spec["dims"])
    kind_of = lambda spec: "member:" + uuid_of[spec["kind"]["member"]] if isinstance(spec["kind"], dict) else spec["kind"]
    partition_of = lambda spec: None if spec["partition"] is None else uuid_of[spec["partition"]]
    formulas = [spec for spec in meta["metrics"] if spec["formula"] is not None]
    for spec in meta["metrics"]:
        if spec["formula"] is not None:
            continue
        dims, kind = dims_of(spec), kind_of(spec)
        storage = engine.from_parquet(file(input_file(spec["id"])), dims, kind, m, partition_of(spec))
        m.add_input(spec["name"], dims, kind=kind, storage=storage, partition=partition_of(spec), id=spec["uuid"])
    for spec in formulas:  # an empty input first: a formula can refer to a Metric that comes later in the file
        m.add_input(spec["name"], dims_of(spec), kind=kind_of(spec), partition=partition_of(spec), id=spec["uuid"])
    for spec in formulas:
        m.add_formula(spec["name"], dims_of(spec), spec["formula"], kind=kind_of(spec),
                      partition=partition_of(spec), overridable=spec["overridable"], id=spec["uuid"])
    # Restore the UUIDs of the members and the handles, in place of the ones that the load made
    m.ids, m._uuids, m.tombstones = {}, {}, set(meta["tombstones"])
    for d in meta["dimensions"]:
        m._bind(d["uuid"], d["id"])
        for u, i in zip(d["member_uuids"], d["member_ids"]):
            m._bind(u, i)
        for prop, spec in d["properties"].items():
            m._bind(prop, spec["id"])
    for spec in meta["metrics"]:
        m._bind(spec["uuid"], spec["id"])
    m._next_id = meta["next_id"]
    return m
