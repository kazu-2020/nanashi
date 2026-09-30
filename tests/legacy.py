"""旧い保存形式（numpy の npz）を、numpy なしで作る（旧形式を読めることのテスト用）。"""
import io
import json
import zipfile
from array import array
from pathlib import Path

import nanashi_core

from sparse_engine.engine import parquet_columns, parquet_value

CODES = {"<u4": "I", "<i8": "q", "<f8": "d", "|b1": "B"}


def npy(descr: str, values: list, shape: tuple | None = None) -> bytes:
    """numpy.save と同じ形（版 1.0）の 1 つの配列。"""
    shape = (len(values),) if shape is None else shape
    header = repr({"descr": descr, "fortran_order": False, "shape": shape}).encode("latin1")
    header += b" " * (63 - (10 + len(header)) % 64) + b"\n"  # numpy と同じく 64 バイト境界にそろえる
    body = array(CODES[descr], [int(v) if descr == "|b1" else v for v in values]).tobytes()
    return b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header + body


def write_npz(path, arrays: dict[str, bytes]) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in arrays.items():
            z.writestr(f"{name}.npy", data)
    Path(path).write_bytes(buf.getvalue())


def to_format2(path, model) -> None:
    """model を保存したディレクトリ path（版 3）を、版 2 の形（inputs.npz）に書き直す。"""
    path = Path(path)
    meta = json.loads((path / "model.json").read_text())
    arrays = {}
    for i, spec in enumerate(meta["metrics"]):
        if spec["formula"] is not None:
            continue
        f = path / f"inputs.{spec['id']}.parquet"
        dims = tuple(spec["dims"])
        sizes = [len(model.dimension(d).members) for d in dims]
        cols, values = nanashi_core.read_parquet(f.read_bytes(), parquet_columns(dims, model),
                                                 parquet_value(spec["kind"]), sizes)
        for d, c in zip(dims, cols):
            arrays[f"{i}.{d}"] = npy("<u4", c)
        arrays[f"{i}.__v"] = npy("<f8", values)
        f.unlink()
    write_npz(path / "inputs.npz", arrays)
    meta["format"] = 2
    (path / "model.json").write_text(json.dumps(meta))
