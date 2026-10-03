"""Measure guard overhead per agent action, in-process and over HTTP.

    cd artifacts/swarm-sentinel/python
    python examples/bench_latency.py [--calls 5000]

Starts a local uvicorn engine for the HTTP measurement. The workload is allowed traffic (the
common case): 20 agents making distinct tool calls and messaging each other. Every call goes
through the full gateway (Algorithm 1 checks) and the Sentinel (graph update and tripwires).
Feedback is off so the fan-out and consensus tripwires this traffic trips raise alerts without
revoking agents mid-measurement; detection cost is still included.
"""
import argparse
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sdk.guard import Guard  # noqa: E402


def workload(guard, calls):
    orchestrator = guard.root()
    agents = [orchestrator.spawn(f"worker-{i}", intent=f"worker {i}") for i in range(20)]
    timings = []
    for n in range(calls):
        agent = agents[n % len(agents)]
        started = time.perf_counter()
        if n % 5 == 4:
            agent.check("message", agents[(n + 1) % len(agents)].name, f"status update {n}",
                        mentions=[agents[(n + 1) % len(agents)].name])
        else:
            agent.check("tool", "mock:search.read", f"look up record {n}")
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def summary(label, timings):
    q = statistics.quantiles(timings, n=100)
    return f"{label:<26} p50 {q[49]:6.3f} ms   p95 {q[94]:6.3f} ms   p99 {q[98]:6.3f} ms   ({len(timings)} calls)"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=5000)
    args = parser.parse_args()

    local = workload(Guard.local(feedback=False), args.calls)
    port = free_port()
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--app-dir", str(ROOT),
                               "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"])
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        workload(Guard.remote(f"http://127.0.0.1:{port}", feedback=False), 200)   # warm up
        remote = workload(Guard.remote(f"http://127.0.0.1:{port}", feedback=False), args.calls)
    finally:
        server.terminate()
        server.wait(timeout=10)
    print(summary("in-process guard", local))
    print(summary("HTTP guard (localhost)", remote))


if __name__ == "__main__":
    main()
