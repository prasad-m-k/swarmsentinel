"""End-to-end: SwarmSentinel engine + SwarmSheriff binary, one agent, confidentiality labels across both paths.

Run from artifacts/swarm-sentinel/python with PYTHONPATH=$PWD:
    python e2e_egress.py SHERIFF_BINARY OUT.json

No traffic leaves the machine: model requests go to llm.lab.invalid, which never resolves, so a forwarded
request ends in the proxy's 502 and a refused one in its 403.
"""
import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd() / "tests"))
import auth  # noqa: E402
import server  # noqa: E402
import test_access as access  # noqa: E402
from asp_policy import ASPPolicy  # noqa: E402
from live import LiveSession, SessionInput, SessionStore  # noqa: E402
from test_mcp_proxy import running_engine  # noqa: E402

import httpx2  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

SHERIFF, OUT = sys.argv[1], Path(sys.argv[2])
SECRET = "e2e-egress-secret"
HOST = "llm.lab.invalid"
SHERIFF_POLICY = {
    "version": 1, "mode": "enforce",
    "destinations": {"approved-tenant": ["llm.company.invalid"], "public-api": ["api.anthropic.com", "api.openai.com"]},
    "classes": [{"id": "credentials", "description": "Secrets an agent read through its tools", "owner": "security",
                 "detectors": {"source_labels": ["asp:credentials"]},
                 "actions": {"approved-tenant": "block", "public-api": "block"}}],
}
BODY = json.dumps({"model": "lab-model", "messages": [{"role": "user", "content": "summarise the deployment log"}]}).encode()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lab_policy():
    tests = Path.cwd() / "tests"
    data = json.loads((tests / "mcp_sample" / "workspace.asp.json").read_text().replace("{tests}", str(tests)))
    data["content-trust"]["taint-scope"] = "task"
    data["swarm"]["step-budget"] = 0
    data["confidentiality"] = {"classes": {"credentials": {"arguments": [r"secrets/"]}}}
    return ASPPolicy.model_validate(data)


def model_request(proxy, credential=None, labels=None):
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["X-Swarmsheriff-Credential"] = credential
    if labels:
        headers["X-Swarmsheriff-Labels"] = labels
    req = urllib.request.Request(f"http://{proxy}/{HOST}/v1/messages", data=BODY, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, text = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode()
    return status, text, (time.perf_counter() - t0) * 1000


async def mcp(base, credential, calls):
    h = httpx2.AsyncClient(headers={"Authorization": f"Bearer {credential}"}, timeout=30)
    async with h, Client(streamable_http_client(f"{base}/api/swarm/mcp", http_client=h)) as c:
        return [await c.call_tool(n, a) for n, a in calls]


def start_sheriff(port, engine, secret_file, policy_file, with_sentinel=True, require=True):
    args = [SHERIFF, "-proxy", f"127.0.0.1:{port}", "-policy", str(policy_file), "-quiet", "-metrics=false",
            "-hooks=false", "-dashboard", "", "-browser=false", "-ai-interval", "0", "-log-content=false",
            "-log", str(Path(secret_file).parent / f"sheriff-{port}.log"), "-interval", "1h"]
    if with_sentinel:
        args += ["-sentinel", engine, "-sentinel-secret-file", str(secret_file)] + (["-require-credential"] if require else [])
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("swarmsheriff did not start")


def outcome(status, text):
    if status == 403 and "swarmsheriff" in text:
        return "refused"
    if status == 502 and "swarmsheriff: upstream request failed" in text:
        return "forwarded"
    return f"unexpected {status}"


def main():
    os.environ["SWARM_EGRESS_SECRET"] = SECRET
    access.AccessControl.setUpClass.__func__(access.AccessControl)
    key = access.AccessControl.key
    owner = lambda: {"Authorization": f"Bearer {access.AccessControl.token(access.AccessControl)}"}  # noqa: E731
    for p in [patch.object(auth, "auth_config", return_value=(access.ISSUER, {access.ORIGIN})),
              patch.object(auth, "key_client", return_value=SimpleNamespace(
                  get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key()))),
              patch.multiple(server, SESSIONS=SessionStore())]:
        p.start()
    engine_ctx = running_engine()
    base = engine_ctx.__enter__()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "secret").write_text(SECRET)
    (tmp / "policy.json").write_text(json.dumps(SHERIFF_POLICY))
    session = LiveSession(SessionInput(policy="mcp-demo", label="e2e"), owner="owner", policy=lab_policy())
    server.SESSIONS.sessions[session.id] = session
    http = httpx2.Client(base_url=base, headers=owner(), timeout=30)
    mint = lambda agent, scope, parent="": http.post(f"/api/swarm/sessions/{session.id}/mcp/agents",  # noqa: E731
                                                      json={"agentId": agent, "scope": scope, "parentId": parent}).json()["credential"]
    p1, p2 = free_port(), free_port()
    sheriff = start_sheriff(p1, base, tmp / "secret", tmp / "policy.json")
    baseline = start_sheriff(p2, base, tmp / "secret", tmp / "policy.json", with_sentinel=False)
    proxy = f"127.0.0.1:{p1}"
    steps = []

    def step(sid, what, expected, status_text):
        status, text, _ = status_text
        got = outcome(status, text)
        steps.append(dict(id=sid, step=what, expected=expected, observed=got, ok=got == expected))
        print(f"{sid:3} {'ok ' if got == expected else 'BAD'} {what}: {got}", flush=True)

    try:
        analyst = mint("analyst", ["mcp:files:*"])
        step("E1", "clean agent sends a model request", "forwarded", model_request(proxy, analyst))
        asyncio.run(mcp(base, analyst, [("workspace__write_file", {"path": "secrets/deploy-token.txt", "content": "lab"}),
                                        ("workspace__read_file", {"path": "secrets/deploy-token.txt"})]))
        labels = session.gateway.labels_for("analyst")
        steps.append(dict(id="E2", step="agent reads a secret through the MCP proxy; engine labels its task",
                          expected=["credentials"], observed=sorted(labels), ok=labels == {"credentials"}))
        print(f"E2  {'ok ' if labels == {'credentials'} else 'BAD'} labels after secret read: {sorted(labels)}")
        step("E3", "same agent's next model request", "refused", model_request(proxy, analyst))
        step("E4", "model request without a credential", "refused", model_request(proxy))
        step("E5", "model request with a forged credential", "refused", model_request(proxy, "ssa_" + "B" * 43))
        child = mint("summarizer", ["mcp:files:*"], parent="analyst")
        step("E6", "child spawned from the labelled task sends a model request", "refused", model_request(proxy, child))
        rotated = http.post(f"/api/swarm/sessions/{session.id}/mcp/agents/analyst/task").json()["credential"]
        step("E7", "owner opens a new task for the agent (declassify); new credential", "forwarded", model_request(proxy, rotated))
        step("E8", "retired credential of the labelled task", "refused", model_request(proxy, analyst))
        session.gateway.revoke(["analyst"])
        step("E9", "SwarmSentinel revokes the agent; its model request", "refused", model_request(proxy, rotated))
        time.sleep(0.5)  # let the asynchronous feed drain
        egress_rows = [t for t in session.gateway.telemetry if t.source == "egress"]
        recorded = dict(rows=len(egress_rows), refused=sum(1 for t in egress_rows if t.violations and t.violations[-1].enforced),
                        provenance=sorted({t.executionProvenance for t in egress_rows}))
        steps.append(dict(id="E10", step="engine recorder holds one egress row per identified decision", expected=5,
                          observed=recorded, ok=recorded["rows"] == 5))
        print(f"E10 {'ok ' if recorded["rows"] == 5 else 'BAD'} recorder egress rows: {recorded}")

        # Latency: refused-by-label path, with credential resolution vs labels in a header (no engine call).
        fresh = mint("bench", ["mcp:files:*"])
        asyncio.run(mcp(base, fresh, [("workspace__read_file", {"path": "secrets/deploy-token.txt"})]))
        for _ in range(50):
            model_request(proxy, fresh), model_request(f"127.0.0.1:{p2}", labels="asp:credentials")
        with_resolve = [model_request(proxy, fresh)[2] for _ in range(1000)]
        header_only = [model_request(f"127.0.0.1:{p2}", labels="asp:credentials")[2] for _ in range(1000)]
        q = lambda xs: dict(p50=statistics.quantiles(xs, n=100)[49], p95=statistics.quantiles(xs, n=100)[94],  # noqa: E731
                            p99=statistics.quantiles(xs, n=100)[98], n=len(xs))
        latency = dict(with_engine_resolution=q(with_resolve), labels_in_header=q(header_only))
        print("latency", json.dumps(latency))
    finally:
        sheriff.terminate()
        baseline.terminate()
        engine_ctx.__exit__(None, None, None)
    OUT.write_text(json.dumps(dict(steps=steps, latency=latency), indent=2))
    print(f"{sum(s['ok'] for s in steps)}/{len(steps)} steps as expected")


if __name__ == "__main__":
    main()
