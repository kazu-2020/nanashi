"""Save and load of a Model.

A directory holds 2 kinds of files.
- model.json: "options" (the Model options), and "changes": the structure of the model as the journal records it
  from an empty model (journal.structure): the dimensions with their members, the member order, the properties,
  the Metric definitions (a formula is the id AST as a JSON tree) and the tombstones. Every object is named by
  its id (the UUID).
- inputs.<Metric id>.parquet: one for each input Metric. A column of member numbers for each dimension
  (d<dimension id>) and the value column v (Float64 for number, Boolean for boolean, UInt32 member numbers
  for a member type).

A member number is the position in the member list of model.json. A member-type value is also a number.
A dimension whose order differs from the number order also has the numbers in order (member_order).
The values of a formula Metric are not saved. The first recalculation after a load calculates them again.
nanashi_core reads and writes Parquet (also for the reference engine).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .engine import Store, default_engine
from .journal import apply, structure

FORMAT_VERSION = 8


def save(model, path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, data in dump(model).items():
        (path / name).write_bytes(data)


def dump(model) -> dict[str, bytes]:
    """The files that save puts, as file name -> content (for a place that is not a directory, such as object
    storage)."""
    from .model import Model
    meta = {"format": FORMAT_VERSION,
            "options": {"auto_layout": model.auto_layout, "delta_aggregation": model.delta_aggregation,
                        "max_cells": model.max_cells},
            "changes": structure(Model(engine=model.engine), model)}
    files = {"model.json": json.dumps(meta, ensure_ascii=False, indent=1).encode()}
    for m in model.metrics.values():
        if m.formula is None:
            # The Rust engine writes from the stored data without the GIL and without Python objects
            files[input_file(m.id)] = model.engine.to_parquet(
                model._values[m.id], m.dims, m.kind, model,
                {"nanashi": json.dumps({"format": FORMAT_VERSION, "metric": m.id})})
    return files


def input_file(metric_id: str) -> str:
    return f"inputs.{metric_id}.parquet"


def load(path, engine: Store | None = None):
    path = Path(path)
    return read(lambda name: (path / name).read_bytes(), engine)


def read(file: Callable[[str], bytes], engine: Store | None = None):
    """Read the format of save with file (a file name -> its content), for a place that is not a directory."""
    from .model import Model

    meta = json.loads(file("model.json"))
    if meta.get("format") != FORMAT_VERSION:
        raise ValueError(f"対応していない保存形式: {meta.get('format')}")
    engine = engine if engine is not None else default_engine()
    m = Model(engine=engine, **meta["options"])
    apply(m, {"changes": meta["changes"]})  # the structure, as a replay of one record
    for spec in meta["changes"].get("metrics", []):
        if spec["formula"] is None:
            x = m.metrics[spec["id"]]
            m._values[x.id] = engine.from_parquet(file(input_file(x.id)), x.dims, x.kind, m, x.partition)
    return m
