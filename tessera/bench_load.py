"""Measure the HTTP server of the engine under many clients, and many engines on 1 host.

    python bench_load.py clients --size large --clients 1,10,100,1000      # 1 engine, closed loop
    python bench_load.py clients --keep-alive --router 8090 --model-id <uuid>  # HTTP/1.1, through the router
    python bench_load.py engines --size medium --engines 10,50 --rate 1400  # N engines, open loop
    python bench_load.py engines --size large --engines 10 --rate 600 --writer cutoff --writer-size largex4 --shares 128

clients: N clients send reads to 1 engine with no pause. It records the reads per second, the latency
percentiles, and the number of 503 (busy) answers and of failed connections.
engines: N engine processes start on this host. It records the RSS of each engine, the CPU time when no
request comes, and the read latency at a total rate split over all engines. With --writer, engine 0 also
receives writes, so that its recalculation runs at the same time (the noisy neighbor): "salary" is a small
set_cell, "cutoff" moves the Cutoff month and recalculates most of the plan. It also records the latency of
each write and the CPU of engine 0. --writer-size gives engine 0 a bigger plan ("largex4" is large with 4
times the employees and products). --rayon0 and --shares limit only engine 0 (RAYON_NUM_THREADS, or a cgroup
v1 with that cpu.shares; the other engines keep 1024). --shares needs root.
The engines have no journal. The load generator runs on the same host and uses some of the CPU.
With --router, the clients send to the router on that port under /models/<--model-id>.
The router finds the engine in nanashi_model. Before you use --router, add the row:
    insert into nanashi_model (model_id, lease_expires, lease_endpoint)
    values ('<uuid>', now() + interval '1 day', 'http://127.0.0.1:18000');  -- 18000 is --port
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import multiprocessing
import os
import random
import re
import subprocess
import sys
import time

MEASURED_SHARE = 0.5  # the first part of each run warms up


def percentiles(xs: list[float], ps=(50, 95, 99)) -> dict[str, float]:
    """Return the nearest-rank percentiles of xs in ms. An empty list gives NaN."""
    s = sorted(xs)
    return {f"p{p}": (s[max(0, math.ceil(len(s) * p / 100) - 1)] * 1e3 if s else float("nan")) for p in ps}


def summarize(results: list[dict], seconds: float) -> dict:
    """Merge the results of the client processes: {"lat": [s], "status": {code: n}}."""
    lat = [x for r in results for x in r["lat"]]
    status: dict[str, int] = {}
    for r in results:
        for k, n in r["status"].items():
            status[k] = status.get(k, 0) + n
    return {"rps": status.get("200", 0) / seconds, **percentiles(lat), "status": status}


def request(path: str, keep_alive: bool = False) -> bytes:
    return f"GET {path} HTTP/1.{int(keep_alive)}\r\nHost: 127.0.0.1\r\n\r\n".encode()


def parse_head(head: bytes) -> tuple[str, int | None, bool]:
    """Return the status code, the Content-Length, and whether the server keeps the connection open.
    shortcut: no chunked bodies; the engine and the router always send Content-Length."""
    m = re.search(rb"\r\ncontent-length: *(\d+)", head, re.I)
    length = int(m.group(1)) if m else None
    keep = length is not None and head.startswith(b"HTTP/1.1") and b"connection: close" not in head.lower()
    return head[9:12].decode(), length, keep


def engine_main(size: str, port: int) -> None:
    """Child process: build the profit and loss plan, start the server, print the read paths and the write."""
    from examples.fpa import SIZES, build
    from sparse_engine.rust_engine import RustEngine
    from sparse_engine.server import Server
    from sparse_engine.workspace import Workspace

    sys.setswitchinterval(0.0005)  # the same as the default of sparse_engine.server
    name, _, scale = size.partition("x")
    employees, products, months = SIZES[name]
    m = build(RustEngine(), employees * int(scale or 1), products * int(scale or 1), months)
    m.recalc()
    did, xid, mid = m.dimension_id, m.member_id, (lambda n: m.metric(n).id)
    q = lambda **c: "&".join(f"{did(d)}={xid(d, x)}" for d, x in c.items())
    emp, prod = m.dimension("Employee").members[1], m.dimension("Product").members[1]
    paths = [f"/metrics/{mid('PayrollByEmployee')}/cell?" + q(Employee=emp, Version="予算", Month="m01"),
             f"/metrics/{mid('Revenue')}/slice?" + q(Product=prod, Version="予算"),
             f"/metrics/{mid('Revenue')}/summary?keep={did('Month')}&" + q(Version="予算")]
    month_ids = [xid("Month", x) for x in m.dimension("Month").members[:-1]]  # as examples.fpa._advance_cutoff
    writes = {"salary": {"op": "set_cell", "metric": mid("Salary"), "values": [float(v) for v in range(1000)],
                         "coords": {did("Employee"): xid("Employee", emp), did("Version"): xid("Version", "予算")}},
              "cutoff": {"op": "set_cell", "metric": mid("Cutoff"), "values": month_ids, "coords": {}}}
    server = Server(Workspace(m.model), "127.0.0.1", port)
    print(json.dumps({"paths": paths, "writes": writes}), flush=True)
    server.serve_forever()


async def _get(port: int, req: bytes, status: dict, lat: list, t0: float, measured: bool, conn=None):
    """Send 1 request. If measured, count its status, and its latency when it is 200.
    Return the open connection for the next request, or None."""
    w = None
    try:
        r, w = conn or await asyncio.open_connection("127.0.0.1", port)
        w.write(req)
        code, length, keep_open = parse_head(await r.readuntil(b"\r\n\r\n"))
        await (r.readexactly(length) if length is not None else r.read())
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
        code, keep_open = type(e).__name__, False
    if not keep_open and w is not None:
        w.close()
    if measured:
        status[code] = status.get(code, 0) + 1
        if code == "200":
            lat.append(time.perf_counter() - t0)
    return (r, w) if keep_open else None


async def _closed(port: int, paths: list[str], clients: int, seconds: float, keep_alive: bool) -> dict:
    status: dict[str, int] = {}
    lat: list[float] = []
    reqs = [request(p, keep_alive) for p in paths]
    start = time.perf_counter()
    end, measure_from = start + seconds, start + seconds * (1 - MEASURED_SHARE)

    async def client(i: int) -> None:
        rng, conn = random.Random(i), None
        while (t := time.perf_counter()) < end:
            conn = await _get(port, rng.choice(reqs), status, lat, t, t >= measure_from, conn)
        if conn:
            conn[1].close()

    await asyncio.gather(*(client(i) for i in range(clients)))
    return {"lat": lat, "status": status}


async def _open(ports: list[int], paths: dict[int, list[str]], rate: float, seconds: float, seed: int) -> dict:
    """Send reads at Poisson times. The latency starts at the planned time, so a slow server cannot hide it."""
    status: dict[str, int] = {}
    lat: list[float] = []
    rng = random.Random(seed)
    tasks, start = [], time.perf_counter()
    measure_from, t = start + seconds * (1 - MEASURED_SHARE), start
    while (t := t + rng.expovariate(rate)) < start + seconds:
        await asyncio.sleep(max(0.0, t - time.perf_counter()))
        port = rng.choice(ports)
        tasks.append(asyncio.create_task(_get(port, request(rng.choice(paths[port])), status, lat, t, t >= measure_from)))
    await asyncio.gather(*tasks)
    return {"lat": lat, "status": status}


def _closed_worker(args) -> dict:
    return asyncio.run(_closed(*args))


def _open_worker(args) -> dict:
    return asyncio.run(_open(*args))


def start_engines(n: int, size: str, base_port: int, rayon: str | None, size0: str | None = None,
                  rayon0: str | None = None) -> list[tuple[subprocess.Popen, int, dict]]:
    """Start n engines. Engine 0 gets size0 and rayon0 when they are given."""
    env = dict(os.environ)
    if rayon:
        env["RAYON_NUM_THREADS"] = rayon
    env0 = {**env, "RAYON_NUM_THREADS": rayon0} if rayon0 else env
    procs: list[subprocess.Popen] = []
    out = []
    try:
        for i in range(n):  # one at a time: a parallel build of 50 models needs 50 times the build memory
            procs.append(subprocess.Popen([sys.executable, __file__, "engine", size0 if i == 0 and size0 else size,
                                           str(base_port + i)], stdout=subprocess.PIPE, text=True, env=env0 if i == 0 else env))
            out.append((procs[-1], base_port + i, json.loads(procs[-1].stdout.readline())))
    except BaseException:
        for p in procs:
            p.kill()
        raise
    return out


def rss_mb(pid: int) -> float:
    with open(f"/proc/{pid}/status") as f:
        return next(int(l.split()[1]) for l in f if l.startswith("VmRSS")) / 1024


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


CGROUP = "/sys/fs/cgroup/cpu/bench_load"


def put_in_cgroup(pid: int, shares: int) -> None:
    """Make the cgroup (cgroup v1, cpu controller), set its cpu.shares, and move the process into it."""
    os.makedirs(CGROUP, exist_ok=True)
    with open(f"{CGROUP}/cpu.shares", "w") as f:
        f.write(str(shares))
    with open(f"{CGROUP}/cgroup.procs", "w") as f:
        f.write(str(pid))


def writer_loop(port: int, write: dict, stop: float) -> list[float]:
    """Send 1 write after the other to engine 0 until the time stop. The values of the write go around
    in a cycle. Return the latency of each write in seconds."""
    import urllib.request
    values, lat = write.pop("values"), []
    while time.perf_counter() < stop:
        body = {"client_op_id": f"w{time.perf_counter_ns()}", "ops": [{**write, "value": values[len(lat) % len(values)]}]}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/writes", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=600) as r:
            r.read()
        lat.append(time.perf_counter() - t0)
    return lat


def run_clients(args) -> None:
    [(proc, port, info)] = start_engines(1, args.size, args.port, args.rayon)
    if args.router:
        port, info["paths"] = args.router, [f"/models/{args.model_id}{p}" for p in info["paths"]]
    try:
        for kind in args.only.split(",") if args.only else ["mix"]:
            for n in map(int, args.clients.split(",")):
                paths = [p for p in info["paths"] if kind == "mix" or f"/{kind}?" in p]
                procs = min(args.procs, n)
                shares = [n // procs + (i < n % procs) for i in range(procs)]
                with multiprocessing.Pool(procs) as pool:
                    res = pool.map(_closed_worker, [(port, paths, s, args.seconds, args.keep_alive) for s in shares])
                r = summarize(res, args.seconds * MEASURED_SHARE)
                print(json.dumps({"kind": kind, "clients": n, **r, "rss_mb": rss_mb(proc.pid)}, ensure_ascii=False),
                      flush=True)
    finally:
        proc.kill()


def run_engines(args) -> None:
    for n in map(int, args.engines.split(",")):
        engines = start_engines(n, args.size, args.port, args.rayon, args.writer_size, args.rayon0)
        try:
            if args.shares:
                put_in_cgroup(engines[0][0].pid, args.shares)
            rss = [rss_mb(p.pid) for p, _, _ in engines]
            c0 = [cpu_seconds(p.pid) for p, _, _ in engines]
            time.sleep(args.idle)
            idle = sum(cpu_seconds(p.pid) - c for (p, _, _), c in zip(engines, c0)) / args.idle
            ports = [port for _, port, _ in engines]
            paths = {port: info["paths"] for _, port, info in engines}
            rate = args.rate / args.procs
            w0 = cpu_seconds(engines[0][0].pid)
            with multiprocessing.Pool(args.procs + 1) as pool:
                writes = None
                if args.writer:
                    writes = pool.apply_async(writer_loop, (ports[0], engines[0][2]["writes"][args.writer],
                                                            time.perf_counter() + args.seconds))
                others = ports[1:] if (args.writer or args.writer_size) and n > 1 else ports  # engine 0 is the neighbor
                res = pool.map(_open_worker, [(others, paths, rate, args.seconds, i) for i in range(args.procs)])
                writes = writes.get() if writes else []
            r = summarize(res, args.seconds * MEASURED_SHARE)
            w = {"writes": len(writes), **{"write_" + k: round(v, 1) for k, v in percentiles(writes).items()}}
            print(json.dumps({"engines": n, "rss_mb_each": round(sum(rss) / n), "rss_gb_total": round(sum(rss) / 1024, 2),
                              "idle_cores": round(idle, 3), "rate": args.rate, **w,
                              "engine0_cores": round((cpu_seconds(engines[0][0].pid) - w0) / args.seconds, 2),
                              "engine0_rss_mb": round(rss_mb(engines[0][0].pid)), **r}, ensure_ascii=False), flush=True)
        finally:
            for p, _, _ in engines:
                p.kill()
                p.wait()
            if args.shares:
                os.rmdir(CGROUP)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("engine")
    e.add_argument("size")
    e.add_argument("port", type=int)
    for name in ("clients", "engines"):
        s = sub.add_parser(name)
        s.add_argument("--size", default="large" if name == "clients" else "medium")
        s.add_argument("--port", type=int, default=18000)
        s.add_argument("--seconds", type=float, default=10)
        s.add_argument("--procs", type=int, default=2, help="the number of load generator processes")
        s.add_argument("--rayon", help="RAYON_NUM_THREADS of the engines")
    sub.choices["clients"].add_argument("--clients", default="1,10,100,1000")
    sub.choices["clients"].add_argument("--only", help="cell, slice, or summary (comma list); the default mixes the 3")
    sub.choices["clients"].add_argument("--keep-alive", action="store_true", help="HTTP/1.1, 1 connection per client, kept open for the next request")
    sub.choices["clients"].add_argument("--router", type=int, metavar="PORT", help="send to the router on this port")
    sub.choices["clients"].add_argument("--model-id", help="the model ID in the path (with --router)")
    sub.choices["engines"].add_argument("--engines", default="10,50")
    sub.choices["engines"].add_argument("--rate", type=float, default=1400, help="reads per second, all engines")
    sub.choices["engines"].add_argument("--idle", type=float, default=5, help="seconds with no request")
    sub.choices["engines"].add_argument("--writer", nargs="?", const="salary", choices=["salary", "cutoff"],
                                        help="send writes to engine 0: a small set_cell, or the Cutoff month")
    sub.choices["engines"].add_argument("--writer-size", help="the size of engine 0, for example largex4")
    sub.choices["engines"].add_argument("--rayon0", help="RAYON_NUM_THREADS of engine 0 only")
    sub.choices["engines"].add_argument("--shares", type=int, help="put engine 0 in a cgroup with this cpu.shares")
    args = ap.parse_args()
    if args.cmd == "clients" and args.router and not args.model_id:
        ap.error("--router には --model-id が要る")
    if args.cmd == "clients" and args.only and set(args.only.split(",")) - {"cell", "slice", "summary"}:
        ap.error(f"--only は cell、slice、summary のいずれか（{args.only}）")
    if args.cmd == "engine":
        engine_main(args.size, args.port)
    elif args.cmd == "clients":
        run_clients(args)
    else:
        run_engines(args)


if __name__ == "__main__":
    main()
