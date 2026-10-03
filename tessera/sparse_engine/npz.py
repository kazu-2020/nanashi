"""旧い保存形式（numpy の npz）を、numpy なしで読む。

保存形式の版 1 と 2 のスナップショット（inputs.npz）と、PostgreSQL の記録先の大量のセルの変更
（cells/*.npz）がこの形式だった。どちらも numpy.savez（圧縮なし）で書き、型は <u4、<i8、<f8、|b1 だけ。
"""
from __future__ import annotations

import ast
import sys
import zipfile
from array import array

TYPES = {"<u4": "I", "<i8": "q", "<f8": "d", "|b1": "B"}


def load(path) -> dict[str, list]:
    """配列の名前 -> 値の列（2 次元なら行の列）。"""
    with zipfile.ZipFile(path) as z:
        return {name.removesuffix(".npy"): _npy(z.read(name)) for name in z.namelist()}


def _npy(data: bytes) -> list:
    if data[:6] != b"\x93NUMPY":
        raise ValueError("npy の形式でない")
    size, start = (2, 8) if data[6] == 1 else (4, 8)
    n = int.from_bytes(data[start:start + size], "little")
    start += size
    header = ast.literal_eval(data[start:start + n].decode("latin1"))
    code = TYPES.get(header["descr"])
    if code is None or header["fortran_order"]:
        raise ValueError(f"対応していない npy の配列: {header}")
    a = array(code)
    if a.itemsize != int(header["descr"][2:]):
        raise ValueError(f"この環境では {header['descr']} を読めない")
    a.frombytes(data[start + n:])
    if sys.byteorder == "big" and a.itemsize > 1:
        a.byteswap()
    values = [v != 0 for v in a] if code == "B" else a.tolist()
    shape = header["shape"]
    if len(shape) == 2:
        w = shape[1]
        return [values[i * w:(i + 1) * w] for i in range(shape[0])]
    return values
