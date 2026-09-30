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
"""
from __future__ import annotations

import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterator

from .core import Dimension
from .expr import Expr
from .parser import parse, to_formula

LOG_VERSION = 1


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

    added_dims, members = [], []
    for d in after.dimensions.values():
        o = old_dims.get(d.id)
        if o is None:
            added_dims.append({"id": d.id, "name": d.name, "ordered": d.ordered,
                               "members": [[i, n] for i, n in zip(d.ids, d.members)]})
        elif o.ids != d.ids or o.members != d.members:
            old_names = dict(zip(o.ids, o.members))
            members.append({"dim": d.id,
                            "removed": [i for i in o.ids if i not in d._by_id],
                            "added": [[i, n] for i, n in zip(d.ids, d.members) if i not in old_names],
                            "renamed": [[i, n] for i, n in zip(d.ids, d.members)
                                        if i in old_names and old_names[i] != n]})

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
        if rows:
            cells.append({"metric": m.id, "rows": rows})

    for key, value in (("dimensions", added_dims), ("members", members), ("properties", props),
                       ("metrics", defs), ("metrics_removed", removed), ("cells", cells)):
        if value:
            out[key] = value
    return out


def _value_dim(model, m):
    return model.dimension(m.kind.removeprefix("member:")) if m.kind.startswith("member:") else None


def _cell_changes(before, o, old_store, after, m, new_store) -> list:
    """入力 Metric m の、書き換わったセルの [座標の ID の列, 変更前, 変更後]。メンバー型の値は ID で持つ。"""
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
    if old_store is not None and o.dims == m.dims and o.kind == m.kind:
        # 位置が変わっていなければ（メンバーを足しただけなら）、エンジンに位置のまま比べてもらう
        olds = [before.dimension(d) for d in m.dims] + ([_value_dim(before, o)] if vdim is not None else [])
        news = dims + ([vdim] if vdim is not None else [])
        if all(a.ids == b.ids[:len(a.ids)] for a, b in zip(olds, news)):
            diff = eng.diff(old_store, new_store)
            if diff is not None:
                return [[[d.ids[p] for d, p in zip(dims, pos)], to_value(a), to_value(b)] for pos, a, b in diff]
    # 名前から ID に直して比べる（メンバーを消したときなど）
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

def apply(model, record: dict) -> None:
    """記録の結果を model に書き込む（計算し直さない。再生のあとで全体を計算し直す）。"""
    ch = record["changes"]
    dim_of = lambda i: next(d for d in model.dimensions.values() if d.id == i)
    metric_of = lambda i: next((m for m in model.metrics.values() if m.id == i), None)

    for d in ch.get("dimensions", []):
        model.dimensions[d["name"]] = Dimension(d["name"], [n for _, n in d["members"]], ordered=d["ordered"],
                                                ids=[i for i, _ in d["members"]], id=d["id"])
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

    for i in ch.get("metrics_removed", []):
        m = metric_of(i)
        model.metrics.pop(m.name)
        model._values.pop(m.name, None)
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

    for c in ch.get("cells", []):
        m = metric_of(c["metric"])
        dims = [model.dimension(d) for d in m.dims]
        vdim = _value_dim(model, m)
        store = model._values[m.name]
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


def _rename_metric_raw(model, old: str, new: str) -> None:
    m = model.metrics.pop(old)
    m.name = new
    model.metrics[new] = m
    if old in model._values:
        model._values[new] = model._values.pop(old)


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
        snapshots()                使えるスナップショットの (通し番号, 置き場所) を新しい順に
    """

    head: int = 0

    def append(self, record: dict) -> int:
        """記録を追記して、ディスクへの書き込みを確かめてから通し番号を返す。"""
        return self.append_many([record])[0]

    def cell_history(self, model, metric: str, **coords: str) -> list[dict]:
        """セルの変更の履歴（古い順）。メンバー型の値は今の名前に直す（消したメンバーは ID のまま）。"""
        m = model.metrics[metric]
        key = [model.dimension(d).id_of(coords[d]) for d in m.dims]
        out = []
        for rec in self.records():
            for c in rec["changes"].get("cells", []):
                if c["metric"] != m.id:
                    continue
                for ids, old, new in c["rows"]:
                    if list(ids) == key:
                        out.append({"seq": rec["seq"], "at": rec["at"], "user": rec["user"],
                                    "reason": rec["reason"], "old": old, "new": new})
        return _shown(model, m, out)

    def open(self, engine=None):
        """最新のスナップショットを読み、その後の記録を再生したモデル（記録先はこの Journal）。"""
        from .engine import default_engine
        from .model import Model
        from .storage import load
        engine = engine if engine is not None else default_engine()
        snaps = self.snapshots()
        if snaps:
            base, path = snaps[0]
            model = load(path, engine)
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


def write_snapshot(model, final: Path, *, fsync: bool) -> dict:
    """model のスナップショット（Model.save の形式と、ファイルのハッシュを持つ meta.json）を、
    一時ディレクトリに書いてから名前を変えて final に置く（途中のものは見えない）。meta を返す。"""
    tmp = final.parent / f".tmp-{final.name}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    from .storage import save
    save(model, tmp)
    meta = {"seq": model.seq, "files": {p.name: _sha256(p) for p in sorted(tmp.iterdir())}}
    (tmp / "meta.json").write_text(json.dumps(meta))
    if fsync:
        for p in tmp.iterdir():
            with open(p, "rb") as f:
                _sync(f.fileno())
    shutil.rmtree(final, ignore_errors=True)
    os.rename(tmp, final)
    if fsync:
        _fsync_dir(final.parent)
    return meta


def snapshot_ok(path: Path, meta: dict) -> bool:
    """スナップショットのファイルがそろっていて、ハッシュが合うか。"""
    return all((path / name).exists() and _sha256(path / name) == h for name, h in meta["files"].items())


class FileJournal(Journal):
    """ディレクトリに記録とスナップショットを置く。

        path/log.jsonl                 1 行 1 トランザクションの記録。追記して fsync する
        path/snapshots/<通し番号>/     その時点のモデル（Model.save の形式）と meta.json（通し番号、ハッシュ）

    最後の行が途中で切れていれば（書いている途中で落ちた）、開くときに捨てる。
    スナップショットは一時ディレクトリに書いてから名前を変えるので、途中のものは見えない。
    """

    def __init__(self, path, *, fsync: bool = True):
        self.path = Path(path)
        self.fsync = fsync
        (self.path / "snapshots").mkdir(parents=True, exist_ok=True)
        self.log_path = self.path / "log.jsonl"
        self.head = 0  # 最後の記録の通し番号
        self._by_client_op: dict[str, int] = {}
        self._scan()

    # ------------------------------------------------ 記録

    def _scan(self) -> None:
        if not self.log_path.exists():
            return
        good = 0
        with open(self.log_path, "rb") as f:
            data = f.read()
        for line in data.splitlines(keepends=True):
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("途中で切れた行")
                rec = json.loads(line)
            except ValueError:
                if good + len(line) < len(data):
                    raise ValueError(f"{self.log_path}: {good} バイト目の記録が壊れている") from None
                break  # 最後の行だけが壊れているなら、書いている途中で落ちた。捨てる
            if rec["seq"] != self.head + 1:
                raise ValueError(f"{self.log_path}: 通し番号が {self.head} の次でなく {rec['seq']}")
            self.head = rec["seq"]
            if rec.get("client_op_id") is not None:
                self._by_client_op[rec["client_op_id"]] = rec["seq"]
            good += len(line)
        if good < len(data):
            with open(self.log_path, "r+b") as f:
                f.truncate(good)

    def append_many(self, records: list[dict]) -> list[int]:
        """複数の記録を追記して、1 回の書き出しでまとめて確定する（グループコミット）。通し番号の列を返す。"""
        seqs = list(range(self.head + 1, self.head + 1 + len(records)))
        lines = "".join(json.dumps({**r, "seq": q}, ensure_ascii=False, separators=(",", ":")) + "\n"
                        for r, q in zip(records, seqs))
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(lines)
            f.flush()
            if self.fsync:
                _sync(f.fileno())
        if seqs:
            self.head = seqs[-1]
        for r, q in zip(records, seqs):
            if r.get("client_op_id") is not None:
                self._by_client_op[r["client_op_id"]] = q
        return seqs

    def seq_of(self, client_op_id: str) -> int | None:
        return self._by_client_op.get(client_op_id)

    def records(self, after: int = 0) -> Iterator[dict]:
        if not self.log_path.exists():
            return
        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec["seq"] > after:
                    yield rec

    # ------------------------------------------------ スナップショット

    def save_snapshot(self, model) -> Path:
        """model（通し番号 model.seq の時点）のスナップショットを原子的に置く。"""
        final = self.path / "snapshots" / f"{model.seq:020d}"
        write_snapshot(model, final, fsync=self.fsync)
        return final

    def snapshots(self) -> list[tuple[int, Path]]:
        """壊れていないスナップショット（通し番号が記録の最後以下のもの）を新しい順に。"""
        out = []
        for p in (self.path / "snapshots").iterdir():
            if p.name.startswith(".") or not (p / "meta.json").exists():
                continue
            meta = json.loads((p / "meta.json").read_text())
            if meta["seq"] > self.head:
                continue  # 記録より新しい（記録を過去に戻したとき）ものは使わない
            if snapshot_ok(p, meta):
                out.append((meta["seq"], p))
        return sorted(out, reverse=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
