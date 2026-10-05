"""書き手のサーバーを止めても、確定を返した書き込みが失われず二重にもならないことを、2 つのサーバーのプロセスで確かめる。

    python -m tests.failover --signal TERM [--via-router]

同じモデルを 2 つのサーバーで開く。先に起こした A が書き手（leader）になり、B は待機系（standby）になる。
複数の送り手が A に書き込み続ける中で、A にシグナルを送る。送り手は確定を受け取るまで、同じ client_op_id の
まま再送する（接続できないときと 503 のときはもう一方のサーバーへ、421 のときは応答にある書き手へ送る）。
--via-router なら、送り手はルーター（router/）の /models/<モデルの ID>/writes にだけ送り、送り先を変えない。
書き手を探して送り直すのはルーターの役目なので、ルーターが 200 以外を返せば失敗として数える。
両方のサーバーを止めたあと、記録先の記録と開き直したモデルの値を、送り手が受け取った確定と突き合わせる。
リースの期限は短くして（LEASE_TTL、3 秒）、SIGKILL の引き継ぎもすぐ測れるようにする。
PostgreSQL（NANASHI_PG_DSN）を使う。
"""
from __future__ import annotations

import argparse
import collections
import http.client
import itertools
import math
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import psycopg

from sparse_engine import Model
from sparse_engine.pg_journal import PgJournal
from sparse_engine.rust_engine import RustEngine

from .journals import DSN
from .test_server import Client

ROOT = Path(__file__).resolve().parent.parent
ITEMS = [f"i{n:03d}" for n in range(200)]
CLIENTS = 4
WARMUP = 50            # A を止める前に受け取る確定の数
AFTER = 50             # A を止めたあとに受け取る確定の数
SETTLE = 2.0           # A を止めたあとの最初の確定から、さらに書き込み続ける秒数
GIVE_UP = 120.0        # 1 つの書き込みの確定を諦めるまでの秒数
CLIENT_TIMEOUT = 100.0  # サーバーが確定を待つ長さ（30 秒）とルーターが送り直す期限（90 秒）より長くする
BACKOFF = 0.1
LEASE_TTL = 3.0        # サーバーに渡すリースの期限（秒）。SIGKILL の引き継ぎはこの長さを待つ


@dataclass(frozen=True)
class Write:
    """送り手が 1 回だけ確定させたい書き込み。再送しても op_id は変えない。"""
    op_id: str
    item: str
    value: int


@dataclass(frozen=True)
class Ack:
    write: Write
    seq: int
    at: float      # 200 を受け取った time.monotonic()
    attempts: int


@dataclass
class Report:
    issued: list[Write] = field(default_factory=list)
    acks: list[Ack] = field(default_factory=list)
    failures: collections.Counter = field(default_factory=collections.Counter)  # 失敗した送信ごとに 1 つ
    killed_at: float = math.nan
    gap: float = math.nan  # A を止めてから、確定を受け取れなかった最も長い間
    committed: dict[str, int] = field(default_factory=dict)  # 記録先にある client_op_id と通し番号
    cells: dict[str, tuple] = field(default_factory=dict)    # 開き直したモデルの (Value, Double)


@dataclass
class Server:
    proc: subprocess.Popen
    url: str
    log: Path


def seed() -> Model:
    m = Model(engine=RustEngine())
    # The UUIDs are fixed strings, so the writes can name the objects without a read of GET /
    m.add_dimension("Item", ITEMS, id="dim-item", member_ids=ITEMS)
    m.add_input("Value", ["Item"], {(i,): 0 for i in ITEMS}, id="metric-value")
    m.add_formula("Double", ["Item"], "Value * 2", id="metric-double")
    return m


@contextmanager
def seeded_model() -> Iterator[tuple[str, Path]]:
    """種のモデルを記録先に置き、(モデルの ID, 作業用のディレクトリ) を渡す。抜けるときに記録を消す。"""
    with tempfile.TemporaryDirectory() as tmp:
        model_id = str(uuid.uuid4())  # The router takes only a canonical UUID as a model ID (docs/ids.md).
        journal = PgJournal(DSN, model_id, Path(tmp) / "objects", heartbeat=False)
        try:
            journal.start(seed())
            yield model_id, Path(tmp)
        finally:
            journal.drop()
            journal.close()


def free_port() -> int:
    """Return a TCP port on 127.0.0.1 that is free now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serving(model_id: str, tmp: Path, name: str, *, role: str | None = None) -> Iterator[Server]:
    """サーバーのプロセスを起こし、/ready が 200 を返す（role を渡せば、その役割になる）まで待つ。
    抜けるときに残っていれば殺す。"""
    port = free_port()
    log = tmp / f"{name}.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "sparse_engine.server", str(tmp / "objects"), "--pg", DSN,
             "--model-id", model_id, "--port", str(port), "--engine", "rust", "--lease-ttl", str(LEASE_TTL)],
            cwd=ROOT, stdout=out, stderr=out)
    server = Server(proc, f"http://127.0.0.1:{port}", log)
    try:
        wait_ready(server, role=role)
        yield server
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@contextmanager
def routing(tmp: Path) -> Iterator[Server]:
    """ルーターをビルドして起こし、/healthz が 200 を返すまで待つ。抜けるときに止める。"""
    go = shutil.which("go")
    if go is None:
        raise RuntimeError("ルーターのビルドに Go が要る")
    binary = tmp / "nanashi-router"
    subprocess.run([go, "build", "-o", str(binary), "./cmd/nanashi-router"], cwd=ROOT.parent / "router", check=True)
    port = free_port()
    log = tmp / "router.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen([str(binary), "--pg", DSN, "--listen", f"127.0.0.1:{port}"], stdout=out, stderr=out)
    router = Server(proc, f"http://127.0.0.1:{port}", log)
    try:
        deadline = time.monotonic() + 30
        while not healthy(router.url, "/healthz"):
            if proc.poll() is not None:
                raise RuntimeError(f"ルーターが終了した（{proc.returncode}）:\n{log.read_text()}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{router.url} が 30 秒で起きなかった")
            time.sleep(0.05)
        yield router
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)


def wait_ready(server: Server, timeout: float = 60.0, role: str | None = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            raise RuntimeError(f"サーバーが終了した（{server.proc.returncode}）:\n{server.log.read_text()}")
        try:
            status, body = Client(server.url, None, timeout=2).get("/ready")
            if status == 200 and role in (None, body["role"]):
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"{server.url} が {timeout} 秒で要求を受けられる{role or ''}にならなかった")


def healthy(url: str, path: str) -> bool:
    try:
        return Client(url, None, timeout=2).get(path)[0] == 200
    except OSError:
        return False


def try_post(url: str, write: Write) -> tuple[int, dict]:
    """write を送り、(状態, JSON の本文) を返す（4xx、5xx でも本文を返す）。"""
    return Client(url, None, timeout=CLIENT_TIMEOUT).post("/writes", {
        "client_op_id": write.op_id,
        "ops": [{"op": "set_cell", "metric": "metric-value", "value": write.value, "coords": {"dim-item": write.item}}]})


def post(url: str, write: Write) -> int:
    """write を送って確定の通し番号を返す。200 でなければ HTTPError。"""
    status, body = try_post(url, write)
    if status != 200:
        raise urllib.error.HTTPError(url, status, body.get("message", ""), {}, None)
    return body["seq"]


def deliver(write: Write, urls: list[str], target: int, report: Report, lock: threading.Lock) -> tuple[Ack, int]:
    """確定を受け取るまで write を送り、(確定, 次に送る先) を返す。"""
    deadline = time.monotonic() + GIVE_UP
    for attempt in itertools.count(1):
        try:
            status, body = try_post(urls[target], write)
            if status == 200:
                return Ack(write, body["seq"], time.monotonic(), attempt), target
            failure = f"{status} {body.get('error')}"
            if status == 421:  # 待機系。応答にある書き手へ送る（分からなければ、次へ）
                target = urls.index(body["leader"]) if body.get("leader") in urls else (target + 1) % len(urls)
            elif status == 503:
                target = (target + 1) % len(urls)
            elif status < 500 and status != 429:
                raise AssertionError(f"{write}: {failure}")
        except (OSError, http.client.HTTPException) as e:  # 応答の途中で切れれば IncompleteRead
            failure, target = type(getattr(e, "reason", e)).__name__, (target + 1) % len(urls)
        with lock:
            report.failures[failure] += 1
        if time.monotonic() > deadline:
            raise TimeoutError(f"{write} の確定を {GIVE_UP} 秒受け取れなかった（最後は {failure}）")
        time.sleep(BACKOFF)


def client(k: int, urls: list[str], report: Report, lock: threading.Lock, stop: threading.Event) -> None:
    """自分の持ち分のメンバーに、値を増やしながら 1 つずつ書き込む。"""
    items = ITEMS[k::CLIENTS]
    target = 0
    for n in itertools.count():
        if stop.is_set():
            return
        write = Write(f"{k}-{n}", items[n % len(items)], n + 1)
        with lock:
            report.issued.append(write)
        ack, target = deliver(write, urls, target, report, lock)
        with lock:
            report.acks.append(ack)


def wait_until(cond, clients: list[Future], timeout: float = GIVE_UP + 30) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        for c in clients:
            if c.done():
                c.result()
        if time.monotonic() > deadline:
            raise TimeoutError(f"{timeout} 秒待っても揃わなかった")
        time.sleep(0.05)


def run(sig: signal.Signals, via_router: bool = False) -> Report:
    """A に書き込み続ける中で A に sig を送り、B に引き継がせる。via_router なら送り手はルーターにだけ送る。"""
    report, lock, stop = Report(), threading.Lock(), threading.Event()

    def acked(after: float) -> list[float]:
        with lock:
            return [a.at for a in report.acks if a.at >= after]

    def settled() -> bool:
        after = acked(report.killed_at)
        return len(after) >= AFTER and time.monotonic() >= min(after) + SETTLE

    with (seeded_model() as (model_id, tmp), serving(model_id, tmp, "a", role="leader") as a,
          serving(model_id, tmp, "b", role="standby") as b, ExitStack() as stack):
        urls = [a.url, b.url]
        if via_router:
            urls = [f"{stack.enter_context(routing(tmp)).url}/models/{model_id}"]
        with ThreadPoolExecutor(CLIENTS) as pool:
            clients = [pool.submit(client, k, urls, report, lock, stop) for k in range(CLIENTS)]
            try:
                wait_until(lambda: len(acked(0.0)) >= WARMUP, clients)
                report.killed_at = time.monotonic()
                a.proc.send_signal(sig)
                wait_until(settled, clients)
            finally:
                stop.set()
            for c in clients:
                c.result()
        b.proc.send_signal(signal.SIGINT)
        for s in (a, b):
            s.proc.wait(timeout=60)
        report.gap = longest_gap(report)
        with psycopg.connect(DSN) as conn:
            report.committed = dict(conn.execute(
                "select client_op_id, seq from nanashi_operation where model_id = %s and client_op_id is not null",
                (model_id,)).fetchall())
        journal = PgJournal(DSN, model_id, tmp / "objects", heartbeat=False)
        try:
            m = journal.open(RustEngine())
            report.cells = {i: (m.get("Value", Item=i), m.get("Double", Item=i)) for i in ITEMS}
        finally:
            journal.close()
    return report


def longest_gap(report: Report) -> float:
    times = [report.killed_at, *sorted(a.at for a in report.acks if a.at >= report.killed_at)]
    return max((b - a for a, b in zip(times, times[1:])), default=math.inf)


def violations(report: Report) -> list[str]:
    """確定を返した書き込みが記録先にちょうど 1 回あり、値が最後に確定したものになっているか。"""
    out = []
    acked = {a.write.op_id: a.seq for a in report.acks}
    unacked = [w.op_id for w in report.issued if w.op_id not in acked]
    if unacked:
        out.append(f"確定を受け取れなかった: {unacked}")
    lost = sorted(acked.keys() - report.committed.keys())
    if lost:
        out.append(f"確定を返したのに記録先にない: {lost}")
    extra = sorted(report.committed.keys() - acked.keys())
    if extra:
        out.append(f"確定を返していないのに記録先にある: {extra}")
    moved = sorted(k for k in acked.keys() & report.committed.keys() if acked[k] != report.committed[k])
    if moved:
        out.append(f"返した通し番号が記録先と違う: {[(k, acked[k], report.committed[k]) for k in moved]}")
    expected = {i: 0 for i in ITEMS}
    for a in sorted(report.acks, key=lambda a: a.seq):
        expected[a.write.item] = a.write.value
    wrong = {i: (report.cells.get(i), (v, 2 * v)) for i, v in expected.items() if report.cells.get(i) != (v, 2 * v)}
    if wrong:
        out.append(f"値が最後に確定したものと違う（実際, 期待）: {wrong}")
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="書き手のサーバーを止めて、もう一方のサーバーに引き継がせる")
    ap.add_argument("--signal", choices=["TERM", "KILL"], default="TERM", help="A に送るシグナル")
    ap.add_argument("--via-router", action="store_true", help="送り手はルーターにだけ送る（Go が要る）")
    args = ap.parse_args(argv)
    report = run(signal.Signals[f"SIG{args.signal}"], args.via_router)
    problems = violations(report)
    print(f"送った書き込み {len(report.issued)}、確定 {len(report.acks)}、"
          f"再送した書き込み {sum(a.attempts > 1 for a in report.acks)}")
    print(f"A を止めてから確定が途切れた最も長い間: {report.gap:.2f} 秒（リースの期限 {LEASE_TTL} 秒）")
    print(f"失敗した送信: {dict(report.failures.most_common())}")
    print("\n".join(problems) or "確定を返した書き込みはすべて 1 回だけ記録され、値も合っている")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
