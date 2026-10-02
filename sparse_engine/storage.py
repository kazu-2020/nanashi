"""Model の保存と読み込み。

ディレクトリに次の 2 種類を置く。
- model.json: 軸（番号の順のメンバー、ID、並び順、順序、プロパティ）と Metric（ID、軸、値の種類、分割軸、式の文字列）
- inputs.<Metric の ID>.parquet: 入力 Metric ごとに 1 つ。軸ごとのメンバー番号の列（d<軸の ID>）と、
  値の列 v（number は Float64、boolean は Boolean、メンバー型はメンバー番号の UInt32）

メンバー番号は model.json のメンバーの列での位置。メンバー型の値も番号で持つ。
位置ごとのメンバーの ID も model.json に持つので、番号から変わらない ID を引ける。
並び順が番号の順と違う軸は、並び順に並べた番号の列（member_order）も持つ（版 4 から）。
Model.save は計算 Metric の値を保存せず、読み込み後の最初の再計算で求め直す。
Parquet と平らな形式の読み書きは nanashi_core が行う（参照実装のエンジンでも）。

記録先のスナップショット（dump(model, snapshot=True)。版 5）は、入力を Parquet でなく格納データの本体と同じ
平らな形式（inputs.<ID>.cells。nanashi_core の flat.rs）で置き、全体を計算し終えたモデルなら計算 Metric の値
（values.<ID>.cells）と差分集計の件数（counts.<ID>.cells）も置く。model.json の各 Metric はファイルの名前
（file、values、counts）と、そのときの分割軸（layout）を持ち、computed に計算したエンジンの名前と計算の版を持つ。
開くときは、同じエンジンで同じ計算の版なら値を読み込んで、以後の記録の変更範囲だけを差分で計算し直す。
違えば、入力だけを読んで全体を計算し直す。

形式の版 1 と 2 は、値を inputs.npz（numpy の形式）に持つ。読み込みだけできる（numpy は要らない）。
版 1 は ID を持たない。読み込むと、ID を新しく振る。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Callable

from .engine import COMPUTE_VERSION, Store, default_engine, native, parquet_columns, parquet_value
from .parser import to_formula

FORMAT_VERSION = 4  # Model.save の形式（入力は Parquet）
SNAPSHOT_VERSION = 5  # 記録先のスナップショットの形式（入力は平らな形式。計算 Metric の値も持つ）
READABLE = (1, 2, 3, 4, 5)


def save(model, path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, data in dump(model).items():
        (path / name).write_bytes(data)


def computed(model) -> bool:
    """model が計算し終えていて（ためている変更も、計画に反映していない定義の変更もなく）、計算 Metric の値を
    保存できるか。"""
    return model._plan is not None and model._pending.empty() and not model._pending.dirty


def dump(model, *, snapshot: bool = False) -> dict[str, bytes]:
    """save で置くファイルの名前 -> 中身（オブジェクトストレージなど、ディレクトリ以外に置くとき）。

    snapshot なら記録先のスナップショットの形式（版 5）で、入力を平らな形式で置き、計算し終えたモデルなら
    計算 Metric の値と差分集計の件数も置く。
    """
    dims = []
    for d in model.dimensions.values():
        props = {prop: {"target": target, "mapping": mapping} for prop, (target, mapping) in d.properties.items()}
        dims.append({"name": d.name, "id": d.id, "members": d.members, "member_ids": d.ids,
                     "ordered": d.ordered, "properties": props})
        if d.rank_table() is not None:
            dims[-1]["member_order"] = d.order()
    metrics = []
    files: dict[str, bytes] = {}
    with_values = snapshot and computed(model)
    for m in model.metrics.values():
        spec = {"name": m.name, "id": m.id, "dims": list(m.dims), "kind": m.kind, "partition": m.partition,
                "formula": None if m.written is None else to_formula(m.written), "overridable": m.overridable}
        metrics.append(spec)
        if snapshot:
            spec["layout"] = model.layout.get(m.name, m.partition)  # 格納データの分割軸（平らな形式の詰め方）
        # Rust なら、格納データから GIL を外して、Python のオブジェクトを作らずに書く
        if m.formula is None:
            if snapshot:
                spec["file"] = name = f"inputs.{m.id}.cells"
                files[name] = model.engine.to_flat(model._values[m.name], m.dims, m.kind, model)
            else:
                files[input_file(m.id)] = model.engine.to_parquet(
                    model._values[m.name], m.dims, m.kind, model,
                    {"nanashi": json.dumps({"format": FORMAT_VERSION, "metric": m.id})})
        elif with_values:
            spec["values"] = name = f"values.{m.id}.cells"
            files[name] = model.engine.to_flat(model._values[m.name], m.dims, m.kind, model)
            if m.name in model._counts:
                spec["counts"] = name = f"counts.{m.id}.cells"
                files[name] = model.engine.to_flat(model._counts[m.name], m.dims, "number", model)
    meta = {"format": SNAPSHOT_VERSION if snapshot else FORMAT_VERSION, "next_id": model._next_id,
            "dimensions": dims, "metrics": metrics,
            "options": {"auto_layout": model.auto_layout, "delta_aggregation": model.delta_aggregation,
                        "max_cells": model.max_cells}}
    if with_values:
        meta["computed"] = {"engine": model.engine.name, "compute": COMPUTE_VERSION}
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
        if meta["format"] >= 5:
            data = file(spec["file"])
            storage = engine.from_flat(data, dims, kind, m, spec.get("layout", spec["partition"]))
        else:
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
    if meta.get("computed") == {"engine": engine.name, "compute": COMPUTE_VERSION}:
        # 同じエンジンで同じ計算の版が計算した値なので、読み込んで差分の再計算から始める
        values, counts = {}, {}
        for spec in meta["metrics"]:
            if "values" in spec:
                dims, kind = tuple(spec["dims"]), spec["kind"]
                values[spec["name"]] = engine.from_flat(file(spec["values"]), dims, kind, m, spec["layout"])
                if "counts" in spec:
                    counts[spec["name"]] = engine.from_flat(file(spec["counts"]), dims, "number", m, spec["layout"])
        m._install_computed(values, counts)
    return m
