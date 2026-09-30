"""Model の保存と読み込み。

ディレクトリに次の 2 種類を置く。
- model.json: 軸（メンバーの並び、ID、順序、プロパティ）と Metric（ID、軸、値の種類、分割軸、式の文字列）
- inputs.npz: 入力 Metric ごとの、各軸のメンバー番号の配列と値の配列

メンバー番号は model.json のメンバーの並びでの位置。メンバー型の値も番号で持つ。
位置ごとのメンバーの ID も model.json に持つので、番号から変わらない ID を引ける。
計算 Metric の値は保存せず、読み込み後の最初の再計算で求め直す。

形式の版 1 は ID を持たない。読み込むと、ID を新しく振る。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .engine import Engine, default_engine
from .parser import to_formula

FORMAT_VERSION = 2
READABLE = (1, 2)


def save(model, path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    dims = []
    for d in model.dimensions.values():
        props = {prop: {"target": target, "mapping": mapping} for prop, (target, mapping) in d.properties.items()}
        dims.append({"name": d.name, "id": d.id, "members": d.members, "member_ids": d.ids,
                     "ordered": d.ordered, "properties": props})
    metrics = []
    arrays: dict[str, np.ndarray] = {}
    for i, m in enumerate(model.metrics.values()):
        metrics.append({"name": m.name, "id": m.id, "dims": list(m.dims), "kind": m.kind, "partition": m.partition,
                        "formula": None if m.written is None else to_formula(m.written),
                        "overridable": m.overridable})
        if m.formula is not None:
            continue
        # 軸ごとのメンバー番号と値の配列で取り出す（Rust なら GIL を外して、Python のオブジェクトを作らずに）
        cols = model.engine.to_arrays(model._values[m.name], model)
        for d in m.dims:
            arrays[f"{i}.{d}"] = np.ascontiguousarray(cols[d], dtype=np.uint32)
        arrays[f"{i}.__v"] = np.ascontiguousarray(cols["__v"], dtype=np.float64)
    meta = {"format": FORMAT_VERSION, "next_id": model._next_id, "dimensions": dims, "metrics": metrics,
            "options": {"auto_layout": model.auto_layout, "delta_aggregation": model.delta_aggregation,
                        "max_cells": model.max_cells}}
    (path / "model.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    np.savez(path / "inputs.npz", **arrays)


def load(path, engine: Engine | None = None):
    from .model import Model

    path = Path(path)
    meta = json.loads((path / "model.json").read_text())
    if meta.get("format") not in READABLE:
        raise ValueError(f"対応していない保存形式: {meta.get('format')}")
    engine = engine if engine is not None else default_engine()
    m = Model(engine=engine, **meta["options"])
    for d in meta["dimensions"]:
        m.add_dimension(d["name"], d["members"], ordered=d["ordered"])
    for d in meta["dimensions"]:  # 参照先の軸がそろってからプロパティを付ける
        for prop, spec in d["properties"].items():
            m.add_property(d["name"], prop, spec["target"], spec["mapping"])
    with np.load(path / "inputs.npz") as data:
        for i, spec in enumerate(meta["metrics"]):
            if spec["formula"] is not None:
                continue
            dims, kind = tuple(spec["dims"]), spec["kind"]
            cols = {d: data[f"{i}.{d}"] for d in dims} | {"__v": data[f"{i}.__v"]}
            if hasattr(engine, "from_arrays"):
                storage = engine.from_arrays(dims, kind, cols, m, spec["partition"])
            else:
                members = [m.dimension(d).members for d in dims]
                values = cols["__v"].tolist()
                if kind == "boolean":
                    values = [v != 0.0 for v in values]
                cells = {tuple(members[j][int(cols[d][r])] for j, d in enumerate(dims)): values[r]
                         for r in range(len(values))}
                storage = engine.from_cells(dims, kind, cells, m, spec["partition"])
            m.add_input(spec["name"], dims, kind=kind, storage=storage, partition=spec["partition"])
    for spec in meta["metrics"]:
        if spec["formula"] is not None:
            m.add_formula(spec["name"], spec["dims"], spec["formula"], kind=spec["kind"],
                          partition=spec["partition"], overridable=spec.get("overridable", False))
    if meta["format"] >= 2:  # 読み込みで振った ID を、保存した ID に戻す
        for d in meta["dimensions"]:
            m.dimensions[d["name"]].id = d["id"]
            m.dimensions[d["name"]].set_ids(d["member_ids"])
        for spec in meta["metrics"]:
            m.metrics[spec["name"]].id = spec["id"]
        m._next_id = meta["next_id"]
    return m
