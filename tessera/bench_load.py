"""Measure the HTTP server of the engine under many clients, and many engines on 1 host.

    python bench_load.py clients --size large --clients 1,10,100,1000      # 1 engine, closed loop
    python bench_load.py engines --size medium --engines 10,50 --rate 1400  # N engines, open loop

clients: N clients send reads to 1 engine with no pause. It records the reads per second, the latency
percentiles, and the number of 503 (busy) answers and of failed connections.
engines: N engine processes start on this host. It records the RSS of each engine, the CPU time when no
request comes, and the read latency at a total rate split over all engines. With --writer, engine 0 also
receives writes, so that its recalculation runs at the same time (the noisy neighbor).
The engines have no journal. The load generator runs on the same host and uses some of the CPU.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import random
import subprocess
import sys
import time

SECOND_HALF = 0.5  # the share of each run that is measured; the first part warms up


# ---------------------------------------------------------------- calculations

def percentiles(xs: list[float], ps=(50, 95, 99)) -> dict[str, float]:
    """Return the nearest-rank percentiles of xs in ms. An empty list gives NaN."""
    s = sorted(xs)
    return {f"p{p}": (s[min(len(s) - 1, len(s) * p // 100)] * 1e3 if s else float("nan")) for p in ps}


def summarize(results: list[dict], seconds: float) -> dict:
    """Merge the results of the client processes: {"lat": [s], "status": {code: n}}."""
    lat = [x for r in results for x in r["lat"]]
    status: dict[str, int] = {}
    for r in results:
        for k, n in r["status"].items():
            status[k] = status.get(k, 0) + n
    return {"rps": status.get("200", 0) / seconds, **percentiles(lat), "status": status}


def request(path: str, host: str) -> bytes:
    return f"GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode()


# ---------------------------------------------------------------- actions

def engine_main(size: str, port: int) -> None:
    """Child process: build the profit and loss plan, start the server, print the read paths and the write."""
    from examples.fpa import SIZES, build
    from sparse_engine.rust_engine import RustEngine
    from sparse_engine.server import Server
    from sparse_engine.workspace import Workspace

    sys.setswitchinterval(0.0005)  # the same as the default of sparse_engine.server
    m = build(RustEngine(), *SIZES[size])
    m.recalc()
    did, xid, mid = m.dimension_id, m.member_id, (lambda n: m.metric(n).id)
    q = lambda **c: "&".join(f"{did(d)}={xid(d, x)}" for d, x in c.items())
    emp, prod = m.dimension("Employee").members[1], m.dimension("Product").members[1]
    paths = [f"/metrics/{mid('PayrollByEmployee')}/cell?" + q(Employee=emp, Version="予算", Month="m01"),
             f"/metrics/{mid('Revenue')}/slice?" + q(Product=prod, Version="予算"),
             f"/metrics/{mid('Revenue')}/summary?keep={did('Month')}&" + q(Version="予算")]
    write = {"op": "set_cell", "metric": mid("Salary"),
             "coords": {did("Employee"): xid("Employee", emp), did("Version"): xid("Version", "予算")}}
    server = Server(Workspace(m.model), "127.0.0.1", port)
    print(json.dumps({"paths": paths, "write": write}), flush=True)
    server.serve_forever()


async def _get(host: str, port: int, req: bytes, status: dict, lat: list, t0: float, keep: bool) -> None:
    try:
        r, w = await asyncio.open_connection(host, port)
        w.write(req)
        data = await r.read()
        w.close()
        code = data[9:12].decode() or "empty"
    except OSError as e:
        code = type(e).__name__
    status[code] = status.get(code, 0) + 1
    if keep and code == "200":
        lat.append(time.perf_counter() - t0)


async def _closed(port: int, paths: list[str], clients: int, seconds: float) -> dict:
    status: dict[str, int] = {}
    lat: list[float] = []
    reqs = [request(p, "127.0.0.1") for p in paths]
    start = time.perf_counter()
    end, measure_from = start + seconds, start + seconds * (1 - SECOND_HALF)

    async def client(i: int) -> None:
        rng = random.Random(i)
        while (t := time.perf_counter()) < end:
            keep = t >= measure_from
            await _get("127.0.0.1", port, rng.choice(reqs), status if keep else {}, lat, t, keep)

    await asyncio.gather(*(client(i) for i in range(clients)))
    return {"lat": lat, "status": status}


async def _open(ports: list[int], paths: dict[int, list[str]], rate: float, seconds: float, seed: int) -> dict:
    """Send reads at Poisson times. The latency starts at the planned time, so a slow server cannot hide it."""
    status: dict[str, int] = {}
    lat: list[float] = []
    rng = random.Random(seed)
    tasks, start = [], time.perf_counter()
    measure_from, t = start + seconds * (1 - SECOND_HALF), start
    while (t := t + rng.expovariate(rate)) < start + seconds:
        await asyncio.sleep(max(0.0, t - time.perf_counter()))
        port = rng.choice(ports)
        keep = t >= measure_from
        tasks.append(asyncio.create_task(_get("127.0.0.1", port, request(rng.choice(paths[port]), "127.0.0.1"),
                                              status if keep else {}, lat, t, keep)))
    await asyncio.gather(*tasks)
    return {"lat": lat, "status": status}


def _closed_worker(args) -> dict:
    return asyncio.run(_closed(*args))


def _open_worker(args) -> dict:
    return asyncio.run(_open(*args))


def start_engines(n: int, size: str, base_port: int, rayon: str | None) -> list[tuple[subprocess.Popen, int, dict]]:
    env = dict(os.environ)
    if rayon:
        env["RAYON_NUM_THREADS"] = rayon
    procs = []
    for i in range(n):  # one at a time: a parallel build of 50 models needs 50 times the build memory
        p = subprocess.Popen([sys.executable, __file__, "engine", size, str(base_port + i)],
                             stdout=subprocess.PIPE, text=True, env=env)
        procs.append((p, base_port + i, json.loads(p.stdout.readline())))
    time.sleep(0.5)  # serve_forever starts after the print
    return procs


def rss_mb(pid: int) -> float:
    with open(f"/proc/{pid}/status") as f:
        return next(int(l.split()[1]) for l in f if l.startswith("VmRSS")) / 1024


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def writer_loop(port: int, write: dict, stop: float) -> int:
    """Send 1 write after the other to engine 0 until the time stop. Return the number of writes."""
    import urllib.request
    n = 0
    while time.perf_counter() < stop:
        body = {"client_op_id": f"w{time.perf_counter_ns()}", "ops": [{**write, "value": float(n % 1000)}]}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/writes", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        n += 1
    return n


def run_clients(args) -> None:
    [(proc, port, info)] = start_engines(1, args.size, args.port, args.rayon)
    try:
        for n in map(int, args.clients.split(",")):
            procs = min(args.procs, n)
            shares = [n // procs + (i < n % procs) for i in range(procs)]
            with multiprocessing.Pool(procs) as pool:
                res = pool.map(_closed_worker, [(port, info["paths"], s, args.seconds) for s in shares])
            r = summarize(res, args.seconds * SECOND_HALF)
            print(json.dumps({"clients": n, **r, "rss_mb": rss_mb(proc.pid)}, ensure_ascii=False), flush=True)
    finally:
        proc.kill()


def run_engines(args) -> None:
    for n in map(int, args.engines.split(",")):
        engines = start_engines(n, args.size, args.port, args.rayon)
        try:
            rss = [rss_mb(p.pid) for p, _, _ in engines]
            c0 = [cpu_seconds(p.pid) for p, _, _ in engines]
            time.sleep(args.idle)
            idle = sum(cpu_seconds(p.pid) - c for (p, _, _), c in zip(engines, c0)) / args.idle
            ports = [port for _, port, _ in engines]
            paths = {port: info["paths"] for _, port, info in engines}
            rate = args.rate / args.procs
            with multiprocessing.Pool(args.procs + 1) as pool:
                writes = None
                if args.writer:
                    writes = pool.apply_async(writer_loop, (ports[0], engines[0][2]["write"],
                                                            time.perf_counter() + args.seconds))
                others = ports[1:] if args.writer and n > 1 else ports
                res = pool.map(_open_worker, [(others, paths, rate, args.seconds, i) for i in range(args.procs)])
                writes = writes.get() if writes else 0
            r = summarize(res, args.seconds * SECOND_HALF)
            print(json.dumps({"engines": n, "rss_mb_each": round(sum(rss) / n), "rss_gb_total": round(sum(rss) / 1024, 2),
                              "idle_cores": round(idle, 3), "rate": args.rate, "writes": writes, **r},
                             ensure_ascii=False), flush=True)
        finally:
            for p, _, _ in engines:
                p.kill()
                p.wait()


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
    sub.choices["engines"].add_argument("--engines", default="10,50")
    sub.choices["engines"].add_argument("--rate", type=float, default=1400, help="reads per second, all engines")
    sub.choices["engines"].add_argument("--idle", type=float, default=5, help="seconds with no request")
    sub.choices["engines"].add_argument("--writer", action="store_true")
    args = ap.parse_args()
    if args.cmd == "engine":
        engine_main(args.size, args.port)
    elif args.cmd == "clients":
        run_clients(args)
    else:
        run_engines(args)


if __name__ == "__main__":
    main()
