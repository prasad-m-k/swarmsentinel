"""Latency of the ASP gateway on four paths, for the TAISAP manuscript.

Run from artifacts/swarm-sentinel/python:
    ../../../.venv/bin/python <this file> --calls 5000 --mcp-calls 1000

1. In-process guard (Guard.local): Algorithm 1 + Sentinel, no transport.
2. HTTP guard (Guard.remote) over loopback with a locally signed RS256 session JWT verified per request
   (same test-key pattern as tests/test_access.py; no real identity provider or account).
3. MCP proxy: a real MCP client -> engine /api/swarm/mcp -> gateway -> stdio sample server -> back.
4. Direct MCP baseline: the same client and sample server over stdio with no engine in between.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path.cwd()
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

import auth  # noqa: E402
import server  # noqa: E402
import test_access as access  # noqa: E402
from asp_policy import ASPPolicy  # noqa: E402
from live import LiveSession, SessionInput, SessionStore  # noqa: E402
from sdk.guard import Guard  # noqa: E402
from test_mcp_proxy import running_engine  # noqa: E402

import httpx2  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402


def q(timings):
    p = statistics.quantiles(timings, n=100)
    return {"n": len(timings), "p50": p[49], "p95": p[94], "p99": p[98], "mean": statistics.fmean(timings)}


def guard_workload(guard, calls, rules=None):
    root = guard.root()
    agents = [root.spawn(f"worker-{i}", intent=f"worker {i}") for i in range(20)]
    clock = datetime.now(timezone.utc)
    out = []
    for n in range(calls):
        agent, at = agents[n % 20], (clock + timedelta(milliseconds=100 * n)).isoformat()
        t0 = time.perf_counter()
        if n % 5 == 4:
            peer = agents[(n + 1) % 20].name
            d = agent.check("message", peer, f"status update {n}", mentions=[peer], timestamp=at)
        else:
            d = agent.check("tool", "mock:search.read", f"look up record {n}", timestamp=at)
        out.append((time.perf_counter() - t0) * 1000)
        if rules is not None:
            rules[d.rule] += 1
    return out


def mcp_policy():
    tests = HERE / "tests"
    text = (tests / "mcp_sample" / "workspace.asp.json").read_text().replace("{tests}", str(tests))
    data = json.loads(text)
    # Benchmark settings: no step budget or write cap so steady allowed traffic is measured.
    data["swarm"]["step-budget"] = 0
    data["swarm"]["write-cap"] = 10 ** 9
    return ASPPolicy.model_validate(data)


async def mcp_calls(client, n, deny_every=0, prefix=""):
    allowed, denied = [], []
    for i in range(n):
        name, args = (prefix + "read_file", {"path": "notes.txt"})
        refused = deny_every and i % deny_every == deny_every - 1
        if refused:
            name, args = (prefix + "run_command", {"command": "true"})
        t0 = time.perf_counter()
        result = await client.call_tool(name, args)
        (denied if refused else allowed).append((time.perf_counter() - t0) * 1000)
        if refused != bool(result.is_error):
            raise RuntimeError(f"unexpected result for {name}: {result}")
    return allowed, denied


async def seed(client, prefix=""):
    result = await client.call_tool(prefix + "write_file", {"path": "notes.txt", "content": "benchmark fixture"})
    if result.is_error:
        raise RuntimeError(f"seed failed: {result}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=5000)
    ap.add_argument("--mcp-calls", type=int, default=1000)
    args = ap.parse_args()
    results = {}

    rules = Counter()
    results["in_process_guard"] = q(guard_workload(Guard.local(feedback=False), args.calls, rules))
    results["in_process_rules"] = dict(rules)

    access.AccessControl.setUpClass.__func__(access.AccessControl)
    key = access.AccessControl.key
    token = lambda: access.AccessControl.token(access.AccessControl)  # noqa: E731
    patchers = [patch.object(auth, "auth_config", return_value=(access.ISSUER, {access.ORIGIN})),
                patch.object(auth, "key_client", return_value=SimpleNamespace(
                    get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key()))),
                patch.multiple(server, SESSIONS=SessionStore())]
    for p in patchers:
        p.start()
    engine = running_engine()
    base = engine.__enter__()
    try:
        guard_workload(Guard.remote(base, token_provider=token, feedback=False), 200)  # warm up
        results["http_guard_loopback_jwt"] = q(guard_workload(Guard.remote(base, token_provider=token, feedback=False), args.calls))

        session = LiveSession(SessionInput(policy="mcp-demo", label="bench"), owner="owner", policy=mcp_policy())
        server.SESSIONS.sessions[session.id] = session
        http = httpx2.Client(base_url=base, headers={"Authorization": f"Bearer {token()}"}, timeout=30)
        cred = http.post(f"/api/swarm/sessions/{session.id}/mcp/agents",
                         json={"agentId": "bench", "scope": ["mcp:files:*", "mcp:shell:*"], "parentId": ""})
        cred.raise_for_status()
        credential = cred.json()["credential"]

        async def via_proxy():
            h = httpx2.AsyncClient(headers={"Authorization": f"Bearer {credential}"}, timeout=30)
            async with h, Client(streamable_http_client(f"{base}/api/swarm/mcp", http_client=h)) as c:
                await seed(c, "workspace__")
                await mcp_calls(c, 50, prefix="workspace__")  # warm up
                return await mcp_calls(c, args.mcp_calls, deny_every=10, prefix="workspace__")
        allowed, denied = asyncio.run(via_proxy())
        results["mcp_proxy_allowed"] = q(allowed)
        results["mcp_proxy_refused"] = q(denied)
        rows = [t for t in session.gateway.telemetry]
        results["mcp_proxy_recorder_rules"] = dict(Counter(t.rule for t in rows))
        http.close()
    finally:
        engine.__exit__(None, None, None)
        for p in reversed(patchers):
            p.stop()

    async def direct():
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            params = StdioServerParameters(command=sys.executable,
                                           args=[str(HERE / "tests" / "mcp_sample" / "workspace_server.py"), "--root", root])
            async with stdio_client(params) as (r, w):
                from mcp import ClientSession
                async with ClientSession(r, w) as c:
                    await c.initialize()
                    await seed(c)
                    await mcp_calls(c, 50)
                    allowed, _ = await mcp_calls(c, args.mcp_calls)
                    return allowed
    results["mcp_direct_stdio_baseline"] = q(asyncio.run(direct()))
    results["env"] = {"python": sys.version.split()[0], "platform": sys.platform, "cpu_count": os.cpu_count()}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
