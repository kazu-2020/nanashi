"""トランザクションの記録（操作ログ）と、スナップショットと記録の再生による復元。

確定したトランザクションごとに、記録を 1 件残す。記録は次の 2 つを持つ。

- 意図（ops）: 呼んだ操作と引数（按分の合計、式の文字列など）。監査のために残す。
- 結果（changes）: トランザクションの前後のモデルの差。軸とメンバーの追加・削除・名前の変更、
  プロパティ、Metric の定義、入力セルの変更前後の値。軸、メンバー、Metric は変わらない ID で表す。

復元では結果だけを再生する。操作を再生すると、按分の浮動小数点の値などがエンジンの版によって
ずれうるが、結果なら書き込むだけで同じ状態に戻る。計算 Metric の値は記録せず、再生のあとで
全体を 1 回だけ計算し直す。

結果は操作を 1 つずつ記録するのでなく、前後のモデルを比べて求める。そのため、按分やメンバーの
削除のように多くのセルを書き換える操作の結果も漏れなく残る。Rust のエンジンでは、書き込んでいない
Metric の格納データは複製前と同じものを指しているので比べずに済み、書き込んだ Metric も差分の木の
違う部分だけを比べる。

書き換えた入力セルは、Metric ごとに {"metric": ID, "dims": 軸の ID の列, "rows": 行} で持つ。rows は
[座標の ID の列, 変更前, 変更後] の列で、Rust のエンジンで BLOCK_MIN 件以上なら、同じ行を順に返す
変更の塊（nanashi_core.CellBlock）のまま持つ（Python のオブジェクトにしない）。JSON に書くときは行に直す。
"""
from __future__ import annotations

import collections
import dataclasses
import datetime
import fcntl
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from .core import Dimension
from .engine import native, parquet_value

BLOCK_MIN = 1000  # 書き換えたセルがこれ以上なら、行の列でなく変更の塊で持つ（Rust のエンジン）
from .expr import Expr
from .objects import LocalObjects
from .parser import parse, to_formula

LOG_VERSION = 1

log = logging.getLogger(__name__)


class Stale(Exception):
    """手元のモデルが記録先より古い（別のプロセスが書き込んだ）。開き直せば続けられる。"""


class Fenced(Stale):
    """書き込みの権利（FileJournal のロック、PgJournal のリース）を取れないか、失った。"""


class BrokenSnapshot(Exception):
    """スナップショットのファイルが欠けているか、ハッシュが合わない。"""


class Snapshot(NamedTuple):
    uri: str               # ファイルの置き場所のキーの接頭辞（<uri>/<ファイルの名前>）
    files: dict[str, str]  # ファイルの名前 -> SHA-256


class AlreadyCommitted(Exception):
    """同じ client_op_id のトランザクションは確定済み（再送されたとき）。seq はその記録の通し番号。"""

    def __init__(self, seq: int):
        super().__init__(f"この操作は確定済み（通し番号 {seq}）")
        self.seq = seq


@dataclasses.dataclass
class Transaction:
    user: str | None = None
    reason: str | None = None
    client_op_id: str | None = None
    ops: list[dict] = dataclasses.field(default_factory=list)  # 意図（利用者が呼んだ操作）
    depth: int = 0  # 操作の中から呼んだ操作は、意図として記録しない
    record: dict | None = None  # 確定した記録
    seq: int | None = None  # 確定した記録の通し番号（記録先がなければ None）


# ---------------------------------------------------------------- 意図

def jsonable(x: Any) -> Any:
    """操作の引数を JSON にできる形にする（大きな入力データは件数だけにする）。"""
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    if isinstance(x, Expr):
        return to_formula(x)
    if isinstance(x, dict):
        if len(x) > 1000:
            return {"cells": len(x)}
        return [[jsonable(k), jsonable(v)] for k, v in x.items()] if any(
            not isinstance(k, str) for k in x) else {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return f"<{type(x).__name__}>"  # エンジンの格納データなど（結果の側に値が残る）


# ---------------------------------------------------------------- 結果（前後の差）

def _kind(model, kind: str) -> Any:
    if kind.startswith("member:"):
        return {"member": model.dimension(kind.removeprefix("member:")).id}
    return kind


def _definition(model, m) -> dict:
    return {"id": m.id, "name": m.name, "dims": [model.dimension(d).id for d in m.dims],
            "kind": _kind(model, m.kind), "formula": None if m.written is None else to_formula(m.written),
            "partition": None if m.partition is None else model.dimension(m.partition).id,
            "overridable": m.overridable}


def changes(before, after) -> dict:
    """before（トランザクション前の複製）から after（確定するモデル）への変化。"""
    out: dict[str, Any] = {"next_id": after._next_id}
    old_dims = {d.id: d for d in before.dimensions.values()}

    added_dims, members, orders = [], [], []
    for d in after.dimensions.values():
        o = old_dims.get(d.id)
        if o is None:
            added_dims.append({"id": d.id, "name": d.name, "ordered": d.ordered,
                               "members": [[i, n] for i, n in zip(d.ids, d.members)]})
            if d.rank_table() is not None:
                orders.append({"dim": d.id, "order": _ids_in_order(d)})
            continue
        if o.ids != d.ids or o.members != d.members:
            old_names = dict(zip(o.ids, o.members))
            members.append({"dim": d.id,
                            "removed": [i for i in o.ids if i not in d._by_id],
                            "added": [[i, n] for i, n in zip(d.ids, d.members) if i not in old_names],
                            "renamed": [[i, n] for i, n in zip(d.ids, d.members)
                                        if i in old_names and old_names[i] != n]})
        # 並び順は、消したメンバーを除き、足したメンバーを最後に並べただけなら記録しない（再生で同じになる）
        expected = [i for i in _ids_in_order(o) if i in d._by_id] + [i for i in d.ids if i not in o._by_id]
        if _ids_in_order(d) != expected:
            orders.append({"dim": d.id, "order": _ids_in_order(d)})

    props = []
    for d in after.dimensions.values():
        o = old_dims.get(d.id)
        for prop, (target, mapping) in d.properties.items():
            old = None if o is None else o.properties.get(prop)
            if old is not None and old[1] is mapping and old[0] == target:
                continue
            t = after.dimension(target)
            new_ids = {d.id_of(k): t.id_of(v) for k, v in mapping.items()}
            old_ids = {} if old is None else {
                o.id_of(k): before.dimension(old[0]).id_of(v) for k, v in old[1].items()}
            set_ = [[k, v] for k, v in new_ids.items() if old_ids.get(k) != v]
            unset = [k for k in old_ids if k not in new_ids]
            if old is None or set_ or unset:
                props.append({"dim": d.id, "prop": prop, "target": t.id, "set": set_, "unset": unset})

    old_metrics = {m.id: m for m in before.metrics.values()}
    defs = []
    for m in after.metrics.values():
        o = old_metrics.get(m.id)
        if o is not None and (o.name, o.dims, o.kind, o.partition, o.overridable) == \
                (m.name, m.dims, m.kind, m.partition, m.overridable) and o.written is m.written:
            continue
        d = _definition(after, m)
        if o is None or d != _definition(before, o):
            defs.append(d)
    alive = {m.id for m in after.metrics.values()}
    removed = [i for i in old_metrics if i not in alive]

    cells = []
    for m in after.metrics.values():
        if m.formula is not None:
            continue
        o = old_metrics.get(m.id)
        old_store = before._values.get(o.name) if o is not None and o.formula is None else None
        rows = _cell_changes(before, o, old_store, after, m, after._values[m.name])
        if len(rows):
            cells.append({"metric": m.id, "dims": [after.dimension(d).id for d in m.dims], "rows": rows})

    for key, value in (("dimensions", added_dims), ("members", members), ("member_order", orders),
                       ("properties", props),
                       ("metrics", defs), ("metrics_removed", removed), ("cells", cells)):
        if value:
            out[key] = value
    return out


def _ids_in_order(d) -> list[int]:
    """軸 d のメンバーの ID を並び順に並べたもの。"""
    return [d.ids[i] for i in d.order()]


def _apply_orders(model, orders: list, dim_of) -> None:
    """記録の並び順（並び順に並べた ID の列）を軸に書き込む。番号もセルも変えないので計算し直さない。"""
    for e in orders:
        d = dim_of(e["dim"])
        d.set_order([d._by_id[i] for i in e["order"]])


def _value_dim(model, m):
    return model.dimension(m.kind.removeprefix("member:")) if m.kind.startswith("member:") else None


def _cell_changes(before, o, old_store, after, m, new_store) -> Any:
    """入力 Metric m の、書き換わったセルの [座標の ID の列, 変更前, 変更後] の列（多ければ変更の塊）。
    メンバー型の値は ID で持つ。"""
    eng = after.engine
    if old_store is not None and eng.same(old_store, new_store):
        return []
    dims = [after.dimension(d) for d in m.dims]
    vdim = _value_dim(after, m)
    if vdim is not None:
        to_value = lambda v: None if v is None else vdim.ids[int(v)]
    elif m.kind == "boolean":
        to_value = lambda v: None if v is None else bool(v)
    else:
        to_value = lambda v: v
    comparable = False
    if old_store is not None and o.dims == m.dims and o.kind == m.kind:
        # 位置が変わっていなければ（メンバーを足しただけなら）、エンジンに位置のまま比べてもらう
        olds = [before.dimension(d) for d in m.dims] + ([_value_dim(before, o)] if vdim is not None else [])
        news = dims + ([vdim] if vdim is not None else [])
        comparable = all(a.ids == b.ids[:len(a.ids)] for a, b in zip(olds, news))
    diff = eng.diff_block(old_store, new_store) if old_store is None or comparable else None
    if diff is not None:
        if len(diff) >= BLOCK_MIN:
            return diff.to_block([d.ids for d in dims], None if vdim is None else vdim.ids, parquet_value(m.kind))
        return [[[d.ids[p] for d, p in zip(dims, pos)], to_value(a), to_value(b)] for pos, a, b in diff.rows()]
    # 名前から ID に直して比べる（メンバーを消したときや、位置で比べられないエンジン）
    new = _by_id(after, m, new_store)
    old = {} if old_store is None else _by_id(before, o, old_store)
    return [[list(k), old.get(k), new.get(k)] for k in old.keys() | new.keys() if old.get(k) != new.get(k)]


def _by_id(model, m, store) -> dict:
    cube = model.engine.to_cube(store, model)
    dims = [model.dimension(d) for d in cube.dims]
    order = [cube.dims.index(d) for d in m.dims]  # 格納データの軸の順を、Metric の宣言の順に直す
    vdim = _value_dim(model, m)
    out = {}
    for k, v in cube.cells.items():
        ids = [dims[i].id_of(k[i]) for i in range(len(k))]
        out[tuple(ids[i] for i in order)] = vdim.ids[int(v)] if vdim is not None else v
    return out


# ---------------------------------------------------------------- 再生

STRUCTURAL = ("dimensions", "members", "properties", "metrics", "metrics_removed")  # 並び順（member_order）は含まない


def apply(model, record: dict, *, incremental: bool = False) -> None:
    """記録の結果を model に書き込む（計算し直さない）。

    incremental でなければ、再生のあとで全体を計算し直す（開くときに多くの記録を再生する）。incremental なら、
    入力セルだけを変えた記録は入力の変更として書き込み、次の recalc は影響範囲だけを計算し直す（ほかの
    プロセスの書き込みに追いつくとき）。軸、メンバー、プロパティ、Metric の定義を変えた記録は、どちらでも
    全体を計算し直す。"""
    ch = record["changes"]
    dims_by_id = model.dimensions_by_id()  # 軸はこの記録で足すものもあるので、足したら入れる
    dim_of = lambda i: dims_by_id[i] if i in dims_by_id else next(d for d in model.dimensions.values() if d.id == i)
    if incremental and model._plan is not None and not any(k in ch for k in STRUCTURAL):
        _apply_orders(model, ch.get("member_order", []), dim_of)
        _apply_cells(model, ch.get("cells", []))
        model._next_id = ch["next_id"]
        return
    metric_of = lambda i: model.metrics_by_id().get(i)

    for d in ch.get("dimensions", []):
        model.dimensions[d["name"]] = dims_by_id[d["id"]] = Dimension(
            d["name"], [n for _, n in d["members"]], ordered=d["ordered"], ids=[i for i, _ in d["members"]], id=d["id"])
    for e in ch.get("members", []):
        d = dim_of(e["dim"])
        for i in e["removed"]:
            model._drop_member(d.name, d.member_of(i))
        # 名前を入れ替える変更もあるので、一度仮の名前にしてから付け直す
        for i, _ in e["renamed"]:
            model._rename_member_raw(d.name, d.member_of(i), f"\0{i}")
        for i, n in e["renamed"]:
            model._rename_member_raw(d.name, d.member_of(i), n)
        for i, n in e["added"]:
            d.add_member(n, i)
        if e["added"]:
            model._member_added(d.name)
    _apply_orders(model, ch.get("member_order", []), dim_of)

    for i in ch.get("metrics_removed", []):
        m = metric_of(i)
        model.metrics.pop(m.name)
        model._state.pop(m.name, None)
    defs = ch.get("metrics", [])
    for spec in defs:  # 名前を入れ替える変更もあるので、一度仮の名前にする
        m = metric_of(spec["id"])
        if m is not None and m.name != spec["name"]:
            _rename_metric_raw(model, m.name, f"\0{spec['id']}")
    for spec in defs:
        _define(model, spec)

    for p in ch.get("properties", []):
        d, t = dim_of(p["dim"]), dim_of(p["target"])
        mapping = dict(d.properties.get(p["prop"], (t.name, {}))[1])
        for i in p["unset"]:
            if i in d._by_id:
                mapping.pop(d.member_of(i), None)
        for i, j in p["set"]:
            mapping[d.member_of(i)] = t.member_of(j)
        d.properties[p["prop"]] = (t.name, mapping)
        model.engine.dimension_changed(model, d.name)

    by_id = model.metrics_by_id()
    for c in ch.get("cells", []):
        m = by_id[c["metric"]]
        dims = [model.dimension(d) for d in m.dims]
        vdim = _value_dim(model, m)
        store = model._values[m.name]
        if not isinstance(c["rows"], list):  # 変更の塊は、書けるエンジンならまとめて書く
            written = model.engine.apply_block(store, c["rows"], [d.ids for d in dims],
                                               None if vdim is None else vdim.ids)
            if written is not None:
                model._values[m.name] = written
                continue
        for ids, _, new in c["rows"]:
            if len(ids) != len(dims) or not all(i in d._by_id for d, i in zip(dims, ids)):
                continue  # このトランザクションで消したメンバーのセル（メンバーと一緒に消えている）
            key = tuple(d.member_of(i) for d, i in zip(dims, ids))
            if new is not None and vdim is not None:
                new = float(vdim._by_id[new])
            elif new is not None and m.kind == "boolean":
                new = bool(new)
            store = model.engine.write(store, key, new, model)
        model._values[m.name] = store

    model._next_id = ch["next_id"]
    model._invalidate()


def _apply_cells(model, cells: list[dict]) -> None:
    """記録のセルの変更を、入力の変更として書き込む（変更範囲を Model の Pending に積む）。"""
    by_id = model.metrics_by_id()
    for c in cells:
        m = by_id[c["metric"]]
        dims = [model.dimension(d) for d in m.dims]
        vdim = _value_dim(model, m)
        rows = c["rows"]
        if not isinstance(rows, list):
            if model._plan is not None and m.name in model._delta_sources():
                model._keep_old(m.name)  # 差分集計には変更前の値が要る
            written = model.engine.apply_block(model._values[m.name], rows, [d.ids for d in dims],
                                               None if vdim is None else vdim.ids)
            if written is not None:
                model._values[m.name] = written
                model._pending.changed[m.name] = {}  # 変わったセルを数えずに、Metric 全体を変わったとする
                continue
        cols: list[list[int]] = [[] for _ in dims]
        values = []
        for ids, _, new in rows:
            if len(ids) != len(dims) or not all(i in d._by_id for d, i in zip(dims, ids)):
                continue
            for col, d, i in zip(cols, dims, ids):
                col.append(d._index[d.member_of(i)])
            if new is not None and vdim is not None:
                new = float(vdim._index[vdim.member_of(int(new))])
            elif new is not None and m.kind == "boolean":
                new = bool(new)
            values.append(new)
        if values:
            model._write_many(m.name, cols, values)


def _rename_metric_raw(model, old: str, new: str) -> None:
    m = model.metrics.pop(old)
    m.name = new
    model.metrics[new] = m
    if old in model._state:
        model._state[new] = model._state.pop(old)


def _define(model, spec: dict) -> None:
    """記録にある Metric の定義にする（新しい Metric なら作る）。"""
    dims = tuple(next(d.name for d in model.dimensions.values() if d.id == i) for i in spec["dims"])
    kind = spec["kind"]
    if isinstance(kind, dict):
        kind = "member:" + next(d.name for d in model.dimensions.values() if d.id == kind["member"])
    partition = None if spec["partition"] is None else next(
        d.name for d in model.dimensions.values() if d.id == spec["partition"])
    name = spec["name"]
    old = next((m for m in model.metrics.values() if m.id == spec["id"]), None)
    if old is not None and old.name != name:
        _rename_metric_raw(model, old.name, name)
    formula = None if spec["formula"] is None else parse(spec["formula"], self_name=name)
    from .model import Metric
    model.metrics[name] = Metric(name, dims, kind, formula, partition, formula, spec["overridable"], id=spec["id"])
    if formula is not None:
        model._values.pop(name, None)  # 計算 Metric の値は再生のあとで計算し直す
    elif (old is None or old.formula is not None or name not in model._values
          or (old.dims, old.kind) != (dims, kind)):
        model._values[name] = model.engine.from_cells(dims, kind, {}, model, partition)


# ---------------------------------------------------------------- ファイルへの記録

class Journal:
    """記録先の共通部分。記録の追記と読み出し、スナップショットの保存と一覧は、記録先ごとに実装する。

        head                       最後の記録の通し番号
        append_many(records)       記録を追記して確定し、通し番号の列を返す（まとめて 1 回で確定する）
        seq_of(client_op_id)       その ID の記録の通し番号（なければ None）
        records(after)             通し番号が after より後の記録（古い順）
        save_snapshot(model)       model（通し番号 model.seq の時点）のスナップショットを置く
        snapshots()                スナップショットの (通し番号, 置き場所) を新しい順に
        load_snapshot(place)       置き場所のスナップショットを読む。壊れていれば BrokenSnapshot
                                   （open は 1 つ前のスナップショットから記録を多く再生する）
        acquire()                  書き込みの権利（FileJournal のロック、PgJournal のリース）を取る
        take()                     書き込みの権利を、待たずに取れるなら取る（待機系が使う）
        leader()                   書き込みの権利を持っているプロセスが公開している番地
        release()                  書き込みの権利を手放す。次の書き手が待たずに済む
        refresh()                  記録先の最新の通し番号を読み直す
        wait(timeout)              記録が増えたかもしれないときまで待つ
    """

    head: int = 0

    def acquire(self) -> None:
        """書き込みの権利を取る。取れなければ Fenced。"""

    def take(self) -> bool:
        """書き込みの権利を、待たずに取れるなら取って True。別のプロセスが持っているか、読み込んだあとに
        書き込まれていれば False。"""
        try:
            self.acquire()
        except Fenced:
            return False
        return True

    def leader(self) -> str | None:
        """書き込みの権利を持っているプロセスが公開している番地（分からない記録先は None）。"""
        return None

    def lease(self) -> dict:
        """書き込みの権利の状態（held、残りの秒数、最後に延長できなかった理由）。"""
        return {"held": False, "expires_in": None, "error": None}

    def refresh(self) -> int:
        """記録先の最新の通し番号を読み直して head にする（ほかのプロセスの書き込みに追いつくとき）。"""
        raise NotImplementedError

    def wait(self, timeout: float) -> None:
        """記録が増えたかもしれないときか、timeout 秒たったときに戻る。"""
        raise NotImplementedError

    def catch_up(self, model) -> bool:
        """model（通し番号 model.seq の時点）を、記録先の最新の状態まで進める。入力セルだけを変えた記録は
        入力の変更として書き込むので、次の recalc は影響範囲だけを計算し直す。進めたら True。"""
        self.refresh()
        if self.head <= model.seq:
            return False
        for rec in self.records(after=model.seq):
            apply(model, rec, incremental=True)
            model.seq = rec["seq"]
        return True

    def release(self) -> None:
        """書き込みの権利を手放す。"""

    def append(self, record: dict) -> int:
        """記録を追記して、ディスクへの書き込みを確かめてから通し番号を返す。"""
        return self.append_many([record])[0]

    def seq_of_many(self, client_op_ids: list[str]) -> dict[str, int]:
        """確定済みの client_op_id -> 通し番号（まとめて 1 回で引ける記録先はそうする）。"""
        out = {}
        for i in client_op_ids:
            if (seq := self.seq_of(i)) is not None:
                out[i] = seq
        return out

    def cell_history(self, model, metric: str, **coords: str) -> list[dict]:
        """セルの変更の履歴（古い順）。メンバー型の値は今の名前に直す（消したメンバーは ID のまま）。"""
        m = model.metrics[metric]
        key = [model.dimension(d).id_of(coords[d]) for d in m.dims]
        out = []
        for rec in self.records():
            for c in rec["changes"].get("cells", []):
                if c["metric"] != m.id:
                    continue
                rows = c["rows"]
                found = ([(old, new) for ids, old, new in rows if list(ids) == key] if isinstance(rows, list)
                         else rows.find(key))  # 変更の塊は、全行を Python にせずに探す
                for old, new in found:
                    out.append({"seq": rec["seq"], "at": rec["at"], "user": rec["user"],
                                "reason": rec["reason"], "old": old, "new": new})
        return _shown(model, m, out)

    def open(self, engine=None):
        """最新のスナップショットを読み、その後の記録を再生したモデル（記録先はこの Journal）。"""
        from .engine import default_engine
        from .model import Model
        engine = engine if engine is not None else default_engine()
        for base, place in self.snapshots():
            try:
                model = self.load_snapshot(place, engine)
                break
            except BrokenSnapshot as e:
                log.warning("スナップショット %d が壊れている（1 つ前から開く）: %s", base, e)
        else:
            base, model = 0, Model(engine=engine)
        for rec in self.records(after=base):
            apply(model, rec)
        model.seq = self.head
        model.journal = self
        return model

    def start(self, model) -> None:
        """記録のない新しい記録先に、model の今の状態を最初のスナップショットとして置き、
        以後の変更を記録する。"""
        if self.head != 0 or self.snapshots():
            raise ValueError("この記録先にはすでに記録がある（open で開く）")
        model.recalc()
        model.seq = 0
        self.save_snapshot(model)
        model.journal = self


def _shown(model, m, history: list[dict]) -> list[dict]:
    """履歴の値を見せる形にする（メンバー型は今の名前、真偽値は bool）。"""
    vdim = _value_dim(model, m)
    for h in history:
        for k in ("old", "new"):
            v = h[k]
            if v is None:
                continue
            if vdim is not None:
                h[k] = vdim.member_of(int(v)) if int(v) in vdim._by_id else int(v)
            elif m.kind == "boolean":
                h[k] = bool(v)
    return history


MANIFEST = "manifest.json"


def put_snapshot(blobs, prefix: str, model) -> dict:
    """model のスナップショット（Model.save の形式）を blobs の prefix/ に置き、manifest（通し番号と
    ファイルのハッシュ）を返す。各ファイルを置いてから最後に manifest.json を置くので、manifest がある
    スナップショットはファイルがそろっている（途中で落ちれば、manifest のないファイルが残るだけ）。"""
    from .storage import dump
    files = {}
    for name, data in dump(model).items():
        blobs.put(f"{prefix}/{name}", data)
        files[name] = hashlib.sha256(data).hexdigest()
    manifest = {"seq": model.seq, "files": files}
    blobs.put(f"{prefix}/{MANIFEST}", json.dumps(manifest).encode())
    return manifest


def read_snapshot(blobs, place: Snapshot, engine):
    """put_snapshot で置いたスナップショットを読む。ファイルが欠けているかハッシュが合わなければ BrokenSnapshot。"""
    from .storage import read

    def file(name: str) -> bytes:
        if name not in place.files:
            raise BrokenSnapshot(f"{place.uri}: {name} が登録されていない")
        try:
            data = blobs.get(f"{place.uri}/{name}")
        except FileNotFoundError:
            raise BrokenSnapshot(f"{place.uri}: {name} がない") from None
        if hashlib.sha256(data).hexdigest() != place.files[name]:
            raise BrokenSnapshot(f"{place.uri}: {name} のハッシュが合わない")
        return data
    return read(file, engine)


class FileJournal(Journal):
    """ディレクトリに記録とスナップショットを置く。

        path/log/<最初の通し番号>.jsonl   1 行 1 トランザクションの記録（区切り）。追記して fsync する
        path/cells/<乱数>-<Metric>.parquet   bulk_cells を超えるセルを書き換えた記録の、セルの変更
        path/snapshots/<通し番号>-<乱数>/   その時点のモデル（Model.save の形式）と manifest.json（通し番号、ハッシュ）

    記録はスナップショットを置いたあと（か、区切りが segment_bytes を超えたら）、次の追記から新しい区切りに
    書く。開くときは最後の区切りと、client_op_id を覚えておく範囲（最後の op_window 件）だけを読み、
    記録の再生はスナップショットより後の区切りだけを読む。prune で、古いスナップショットと、それより前の
    区切りと大量の変更のファイルを消せる。以前の版の path/log.jsonl は、最初の区切りとして読む。

    書き込むプロセスは 1 つに限る。最初に追記するときに path/lock の排他ロックを取り、release まで持つ。
    最後の行が途中で切れていれば（書いている途中で落ちた）、読むときは無視し、ロックを取ったときに捨てる。
    追記に失敗すれば、ファイルを追記の前の長さに戻す。
    ファイルは置き場所（objects.LocalObjects）に置く。スナップショットは各ファイルを置いてから最後に
    manifest.json を置くので、manifest のないもの（途中で落ちたもの）は使わない。ハッシュは読むときに
    確かめ、合わなければ 1 つ前のスナップショットから開く。
    大量のセルの変更は、JSON の行にせず Metric ごとの Parquet に書き（PgJournal と同じ形式）、記録の行には
    ファイルの名前とハッシュだけを入れる。ファイルを書き出してから行を追記するので、確定した記録の
    ファイルは必ずそろっている（行を書く前に落ちれば、参照されないファイルが残るだけ）。

    client_op_id は最後の op_window 件の記録の分だけ覚える（再送しても二重に確定しないと保証する範囲）。
    """

    def __init__(self, path, *, fsync: bool = True, bulk_cells: int = 10_000, op_window: int = 100_000,
                 segment_bytes: int = 256 << 20):
        self.path = Path(path)
        self.fsync = fsync
        self.bulk_cells = bulk_cells
        self.op_window = op_window
        self.segment_bytes = segment_bytes
        self.path.mkdir(parents=True, exist_ok=True)
        self.objects = LocalObjects(self.path, fsync=fsync)
        self._lock_file = None  # 書き込みの権利（path/lock の排他ロック）。最初に書くときに取る
        self._broken: BaseException | None = None  # 追記の失敗を取り消せなかった（以後は書かない）
        self._rotate = False  # 次の追記から新しい区切りに書く（スナップショットを置いた）
        self._scan()

    @property
    def log_path(self) -> Path:
        """今追記している区切りのファイル。"""
        return self._segments[-1][1]

    # ------------------------------------------------ 書き込みの権利

    def acquire(self) -> None:
        """書き込みの権利（path/lock の排他ロック）を取る。別のプロセスが持っていれば Fenced。
        読み込んだあとに別のプロセスが書き込んでいたら、手元が古いので Fenced（開き直せば続けられる）。"""
        if self._lock_file is not None:
            return
        f = open(self.path / "lock", "a+b")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise Fenced(f"{self.path}: 別のプロセスが書き込み中") from None
        head = self.head
        self._scan(repair=True)  # ロックを取ってから読み直す（途中で切れた最後の行は、ここで捨てる）
        if self.head != head:
            f.close()
            raise Fenced(f"{self.path}: 読み込んだあとに別のプロセスが書き込んだ（{head} → {self.head}）。開き直す")
        self._lock_file = f

    def lease(self) -> dict:
        return {"held": self._lock_file is not None, "expires_in": None, "error": None}

    def release(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()  # 閉じればロックも外れる
            self._lock_file = None

    # ------------------------------------------------ 記録

    def _list_segments(self) -> list[tuple[int, Path]]:
        """記録の区切り（最初の通し番号, ファイル）を古い順に。まだなければ最初の区切りを 1 つ返す。"""
        segs = [(int(p.stem), p) for p in (self.path / "log").glob("*.jsonl") if p.stem.isdigit()]
        legacy = self.path / "log.jsonl"
        if legacy.exists():
            segs.append((1, legacy))
        segs.sort()
        return segs or [(1, self.path / "log" / f"{1:020d}.jsonl")]

    def _read_segment(self, first: int, path: Path, start: int = 0, *, strict: bool = False):
        """区切りの start バイト目からの完全な記録を読み、(記録, 行のバイト数) を順に返す。最後の行が途中で
        切れていれば読まない。strict なら、途中の壊れた行を ValueError にする。"""
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            f.seek(start)
            pos = start
            for line in f:
                try:
                    if not line.endswith(b"\n"):
                        raise ValueError("途中で切れた行")
                    rec = json.loads(line)
                except ValueError:
                    if strict and pos + len(line) < size:
                        raise ValueError(f"{path}: {pos} バイト目の記録が壊れている") from None
                    return
                pos += len(line)
                yield rec, len(line)

    def _remember(self, client_op_id: str | None, seq: int) -> None:
        if client_op_id is not None:
            self._by_client_op[client_op_id] = seq
        oldest = self.head - self.op_window
        while self._by_client_op:  # 覚えておく範囲より古い client_op_id は捨てる（古い順に並んでいる）
            k, q = next(iter(self._by_client_op.items()))
            if q > oldest:
                break
            del self._by_client_op[k]

    def _scan(self, repair: bool = False) -> None:
        """最後の区切りを読んで最後の通し番号を求め、client_op_id を覚えておく範囲の記録を読む（ログ全体を
        読まない）。最後の行が途中で切れていれば、書いている途中で落ちたか、別のプロセスが書いている途中なので
        読まない。repair（書き込みの権利を持っているとき）なら、その行をファイルから切り捨てる。"""
        self._segments = self._list_segments()
        self._by_client_op: collections.OrderedDict[str, int] = collections.OrderedDict()
        first, path = self._segments[-1]
        self.head, self._size = first - 1, 0
        for rec, n in self._read_segment(first, path, strict=True):
            if rec["seq"] != self.head + 1:
                raise ValueError(f"{path}: 通し番号が {self.head} の次でなく {rec['seq']}")
            self.head = rec["seq"]
            self._size += n
        # client_op_id を覚えておく範囲が前の区切りにかかれば、そこから読む
        oldest = self.head - self.op_window
        start = len(self._segments) - 1
        while start > 0 and self._segments[start][0] > oldest + 1:
            start -= 1
        for i in range(start, len(self._segments)):
            for rec, _ in self._read_segment(*self._segments[i]):
                if rec["seq"] > oldest and rec.get("client_op_id") is not None:
                    self._by_client_op[rec["client_op_id"]] = rec["seq"]
        if repair and path.exists() and path.stat().st_size > self._size:
            with open(path, "r+b") as f:
                f.truncate(self._size)
                if self.fsync:
                    _sync(f.fileno())

    def refresh(self) -> int:
        """最後に読んだところより後に追記された記録を読む（全体を読み直さない）。別のプロセスが新しい区切りに
        書き始めていれば、そちらも読む。"""
        while True:
            first, path = self._segments[-1]
            for rec, n in self._read_segment(first, path, self._size):
                if rec["seq"] != self.head + 1:
                    raise ValueError(f"{path}: 通し番号が {self.head} の次でなく {rec['seq']}")
                self.head = rec["seq"]
                self._size += n
                self._remember(rec.get("client_op_id"), rec["seq"])
            nxt = self.path / "log" / f"{self.head + 1:020d}.jsonl"
            if self.head + 1 == first or not nxt.exists():
                return self.head
            self._segments.append((self.head + 1, nxt))
            self._size = 0

    def wait(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.log_path.stat().st_size > self._size:
                    return
            except FileNotFoundError:
                pass
            if (self.path / "log" / f"{self.head + 1:020d}.jsonl").exists() and self.head + 1 != self._segments[-1][0]:
                return
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def append_many(self, records: list[dict]) -> list[int]:
        """複数の記録を追記して、1 回の書き出しでまとめて確定する（グループコミット）。通し番号の列を返す。
        書き出しか fsync に失敗したら、ファイルを追記の前の長さに戻してから例外を投げる（書きかけの行が
        残ると、次の記録と通し番号が重なって開けなくなる）。戻せなければ、以後の書き込みを拒否する。"""
        if self._broken is not None:
            raise OSError(f"{self.log_path}: 以前の追記の失敗を取り消せなかったので、書き込まない"
                          "（ファイルを確かめてから開き直す）") from self._broken
        if not records:
            return []
        self.acquire()
        if self._size > 0 and (self._rotate or self._size >= self.segment_bytes):
            self._segments.append((self.head + 1, self.path / "log" / f"{self.head + 1:020d}.jsonl"))
            self._size = 0
        self._rotate = False
        seqs = list(range(self.head + 1, self.head + 1 + len(records)))
        lines = []
        for r, q in zip(records, seqs):
            line = {**r, "seq": q}
            if cell_count(r) > self.bulk_cells:  # 大量のセルは、先にファイルへ書き出す
                line["changes"] = {k: v for k, v in r["changes"].items() if k != "cells"}
                line["cells_blob"] = self._write_cells(r)
            lines.append(json.dumps(line, ensure_ascii=False, separators=(",", ":"), default=_json_rows) + "\n")
        data = "".join(lines).encode("utf-8")
        path = self.log_path
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            try:
                _write_all(fd, data)
                if self.fsync:
                    _sync(fd)
                    if new:
                        _fsync_dir(path.parent)  # 新しい区切りのファイルの名前もディスクへ
            except BaseException as e:
                try:
                    os.ftruncate(fd, self._size)
                    if self.fsync:
                        _sync(fd)
                except BaseException as undo:
                    self._broken = undo
                    log.critical("%s: 追記の失敗を取り消せなかった", path, exc_info=True)
                raise e
        finally:
            os.close(fd)
        self._size += len(data)
        self.head = seqs[-1]
        for r, q in zip(records, seqs):
            self._remember(r.get("client_op_id"), q)
        return seqs

    def seq_of(self, client_op_id: str) -> int | None:
        return self._by_client_op.get(client_op_id)

    def seq_of_many(self, client_op_ids: list[str]) -> dict[str, int]:
        return {i: self._by_client_op[i] for i in client_op_ids if i in self._by_client_op}

    def _write_cells(self, record: dict) -> dict:
        """記録のセルの変更を cells/ の Parquet に置き、記録の行に入れる参照（キーは path からの相対）を返す。"""
        prefix = f"cells/{uuid.uuid4().hex}"

        def put(name: str, data: bytes) -> str:
            self.objects.put(f"{prefix}-{name}", data)
            return f"{prefix}-{name}"
        return {"format": "parquet", "files": write_cell_files(record, put), "cells": cell_count(record)}

    def records(self, after: int = 0) -> Iterator[dict]:
        segs = self._list_segments()
        for i, (first, path) in enumerate(segs):
            if i + 1 < len(segs) and segs[i + 1][0] <= after + 1:
                continue  # この区切りの記録は、すべて after 以前
            if first > after + 1 and i == 0 and first > 1:
                raise ValueError(f"{self.path}: 通し番号 {after + 1} からの記録は prune で消した")
            for rec, _ in self._read_segment(first, path):
                if rec["seq"] > self.head:
                    return  # 読み込んだあとに別のプロセスが書いた記録（開き直すまで見ない）
                if rec["seq"] > after:
                    if "cells_blob" in rec:  # 大量のセルはファイルから、変更の塊として読む
                        rec["changes"]["cells"] = read_cell_files(rec.pop("cells_blob")["files"],
                                                                   self.objects.get)
                    yield rec

    def prune(self, keep: int = 2) -> dict:
        """新しいほうから keep 個のスナップショットを残し、それより古いスナップショットと、残す一番古い
        スナップショットより前の記録の区切りと、その記録の大量の変更のファイルを消す。消した数を返す。
        記録を消すので、残すスナップショットより前へは戻れなくなる（セルの履歴もそこから後だけになる）。"""
        self.acquire()
        snaps = self.snapshots()
        if len(snaps) <= keep:
            return {"snapshots": 0, "segments": 0, "cells": 0}
        oldest = snaps[keep - 1][0]
        out = {"snapshots": 0, "segments": 0, "cells": 0}
        for _, place in snaps[keep:]:
            for key in self.objects.list(place.uri + "/"):
                self.objects.delete(key)
            out["snapshots"] += 1
        segs = self._list_segments()
        for i, (first, path) in enumerate(segs[:-1]):
            if segs[i + 1][0] > oldest + 1:
                break  # この区切りには、残すスナップショットより後の記録がある
            for rec, _ in self._read_segment(first, path):
                for f in rec.get("cells_blob", {}).get("files", []):
                    self.objects.delete(f["uri"])
                    out["cells"] += 1
            path.unlink()
            out["segments"] += 1
        return out

    # ------------------------------------------------ スナップショット

    def save_snapshot(self, model) -> str:
        """model（通し番号 model.seq の時点）のスナップショットを置く。同じ通し番号で取り直しても、置いた
        ファイルを書き換えないよう、置き場所ごとに乱数を付ける。次の追記から、記録を新しい区切りに書く。"""
        prefix = f"snapshots/{model.seq:020d}-{uuid.uuid4().hex[:8]}"
        put_snapshot(self.objects, prefix, model)
        self._rotate = True
        return prefix

    def snapshots(self) -> list[tuple[int, Snapshot]]:
        """置き終えたスナップショット（通し番号が記録の最後以下のもの）を新しい順に。ハッシュは読むときに
        確かめる（開くたびにすべてのファイルを読まない）。以前の版の snapshots/<通し番号>/meta.json も読む。"""
        out = []
        for key in self.objects.list("snapshots/"):
            prefix, _, name = key.rpartition("/")
            if name not in (MANIFEST, "meta.json"):
                continue
            meta = json.loads(self.objects.get(key))
            if meta["seq"] <= self.head:  # 記録より新しい（記録を過去に戻したとき）ものは使わない
                out.append((meta["seq"], Snapshot(prefix, meta["files"])))
        return sorted(out, key=lambda x: (x[0], x[1].uri), reverse=True)

    def load_snapshot(self, place: Snapshot, engine):
        return read_snapshot(self.objects, place, engine)


def cell_count(record: dict) -> int:
    """記録の中の、書き換えた入力セルの数。"""
    return sum(len(c["rows"]) for c in record["changes"].get("cells", []))


def as_block(core, rows):
    """行の列なら変更の塊にする（変更の塊ならそのまま）。"""
    return core.CellBlock.from_rows(rows) if isinstance(rows, list) else rows


def write_cell_files(record: dict, put: Callable[[str, bytes], str]) -> list[dict]:
    """記録のセルの変更を Metric ごとの Parquet にし、put(<Metric の ID>.parquet, 中身) で置いて、
    [{"metric", "uri"（put が返した置き場所）, "sha256", "cells"}] を返す。"""
    core = native()
    files = []
    for c in record["changes"].get("cells", []):
        block = as_block(core, c["rows"])
        # 列の名前は軸の ID（記録に軸がなければ c0、c1、…）
        names = [f"d{i}" for i in c["dims"]] if "dims" in c else [f"c{j}" for j in range(block.width)]
        data = block.to_parquet(names, [("nanashi", json.dumps({"metric": c["metric"]}))])
        files.append({"metric": c["metric"], "uri": put(f"{c['metric']}.parquet", data),
                      "sha256": hashlib.sha256(data).hexdigest(), "cells": len(block)})
    return files


def read_cell_files(files: list[dict], get: Callable[[str], bytes]) -> list[dict]:
    """write_cell_files で置いたセルの変更を、get(置き場所) で読み、Metric ごとの変更の塊で返す。"""
    core = native()
    cells = []
    for f in files:
        data = get(f["uri"])
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ValueError(f"{f['uri']}: セルの変更のファイルが壊れている")
        cells.append({"metric": f["metric"], "rows": core.CellBlock.from_parquet(data)})
    return cells


def _json_rows(x: Any) -> list:
    """変更の塊を、JSON に書ける行の列にする。"""
    if hasattr(x, "rows"):
        return x.rows()
    raise TypeError(f"JSON にできない値: {type(x).__name__}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _sync(fd: int) -> None:
    """ファイルの中身をディスクまで書き出す。macOS の fsync は装置の書き込みキャッシュまでは
    書き出さないので、F_FULLFSYNC が使えればそれを使う。"""
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass  # 対応しないファイルシステムでは fsync に戻る
    os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        _sync(fd)
    finally:
        os.close(fd)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
