"""Model の保存と読み込み。

ディレクトリに次の 2 種類を置く。
- model.json: 軸（番号の順のメンバー、ID、並び順、順序、プロパティ）と Metric（ID、軸、値の種類、分割軸、式の文字列）
- inputs.<Metric の ID>.parquet: 入力 Metric ごとに 1 つ。軸ごとのメンバー番号の列（d<軸の ID>）と、
  値の列 v（number は Float64、boolean は Boolean、メンバー型はメンバー番号の UInt32）

メンバー番号は model.json のメンバーの列での位置。メンバー型の値も番号で持つ。
位置ごとのメンバーの ID も model.json に持つので、番号から変わらない ID を引ける。
並び順が番号の順と違う軸は、並び順に並べた番号の列（member_order）も持つ（版 4 から）。
計算 Metric の値は保存せず、読み込み後の最初の再計算で求め直す。
Parquet の読み書きは nanashi_core が行う（参照実装のエンジンでも）。

形式の版 1 と 2 は、値を inputs.npz（numpy の形式）に持つ。読み込みだけできる（numpy は要らない）。
版 1 は ID を持たない。読み込むと、ID を新しく振る。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Callable

from .engine import Store, default_engine, native, parquet_columns, parquet_value
from .parser import to_formula

FORMAT_VERSION = 4
READABLE = (1, 2, 3, 4)


def save(model, path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, data in dump(model).items():
        (path / name).write_bytes(data)


def dump(model) -> dict[str, bytes]:
    """save で置くファイルの名前 -> 中身（オブジェクトストレージなど、ディレクトリ以外に置くとき）。"""
    dims = []
    for d in model.dimensions.values():
        props = {prop: {"target": target, "mapping": mapping} for prop, (target, mapping) in d.properties.items()}
        dims.append({"name": d.name, "id": d.id, "members": d.members, "member_ids": d.ids,
                     "ordered": d.ordered, "properties": props})
        if d.rank_table() is not None:
            dims[-1]["member_order"] = d.order()
    metrics = []
    files: dict[str, bytes] = {}
    for m in model.metrics.values():
        metrics.append({"name": m.name, "id": m.id, "dims": list(m.dims), "kind": m.kind, "partition": m.partition,
                        "formula": None if m.written is None else to_formula(m.written),
                        "overridable": m.overridable})
        if m.formula is not None:
            continue
        # Rust なら、格納データから GIL を外して、Python のオブジェクトを作らずに書く
        files[input_file(m.id)] = model.engine.to_parquet(
            model._values[m.name], m.dims, m.kind, model,
            {"nanashi": json.dumps({"format": FORMAT_VERSION, "metric": m.id})})
    meta = {"format": FORMAT_VERSION, "next_id": model._next_id, "dimensions": dims, "metrics": metrics,
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
    if meta.get("format") not in READABLE:
        raise ValueError(f"対応していない保存形式: {meta.get('format')}")
    engine = engine if engine is not None else default_engine()
    m = Model(engine=engine, **meta["options"])
    for d in meta["dimensions"]:
        m.add_dimension(d["name"], d["members"], ordered=d["ordered"])
        if meta["format"] >= 2:  # Parquet の列の名前が軸の ID なので、入力を読む前に保存した ID に戻す
            m.dimensions[d["name"]].id = d["id"]
            m.dimensions[d["name"]].set_ids(d["member_ids"])
        if "member_order" in d:
            m.dimensions[d["name"]].set_order(d["member_order"])
    for d in meta["dimensions"]:  # 参照先の軸がそろってからプロパティを付ける
        for prop, spec in d["properties"].items():
            m.add_property(d["name"], prop, spec["target"], spec["mapping"])
    legacy = None
    if meta["format"] < 3:
        from . import npz
        legacy = npz.load(io.BytesIO(file("inputs.npz")))
    for i, spec in enumerate(meta["metrics"]):
        if spec["formula"] is not None:
            continue
        dims, kind = tuple(spec["dims"]), spec["kind"]
        if legacy is None:
            data = file(input_file(spec["id"]))
        else:  # 旧い形式の配列を、同じ Parquet の形にしてから読む
            data = native().write_parquet(parquet_columns(dims, m), [legacy[f"{i}.{d}"] for d in dims],
                                          legacy[f"{i}.__v"], parquet_value(kind), [])
        storage = engine.from_parquet(data, dims, kind, m, spec["partition"])
        m.add_input(spec["name"], dims, kind=kind, storage=storage, partition=spec["partition"])
    for spec in meta["metrics"]:
        if spec["formula"] is not None:
            m.add_formula(spec["name"], spec["dims"], spec["formula"], kind=spec["kind"],
                          partition=spec["partition"], overridable=spec.get("overridable", False))
    if meta["format"] >= 2:  # 読み込みで振った ID を、保存した ID に戻す
        for spec in meta["metrics"]:
            m.metrics[spec["name"]].id = spec["id"]
        m._next_id = meta["next_id"]
    return m
