"""Engine for the confinement lab. Runs inside the engine container only.

Starts the real engine (server:app) with one MCP session on the sample workspace policy, under
content-trust taint-scope "task". Identity provider calls are replaced by a lab-only signing key that
never leaves this container, so the owner API works for the operator and nobody else. One agent is
minted; its credential (and nothing else) is written to the volume shared with the agent container.
"""
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa

APP = Path("/app/repo/artifacts/swarm-sentinel/python")
sys.path.insert(0, str(APP))

import auth  # noqa: E402
import server  # noqa: E402
from asp_policy import ASPPolicy  # noqa: E402
from live import LiveSession, SessionInput  # noqa: E402

ISSUER, ORIGIN = "https://lab-issuer.invalid", "https://lab-operator.invalid"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SECRETS = Path(os.environ.get("AGENT_SECRETS", "/run/agent-secrets"))
OPERATOR = Path(os.environ.get("OPERATOR_SECRETS", "/run/operator"))


def owner_token():
    now = int(time.time())
    return jwt.encode(dict(sub="operator", sid="lab", iss=ISSUER, azp=ORIGIN, iat=now, nbf=now - 1, exp=now + 3600),
                      KEY, algorithm="RS256", headers={"kid": "lab"})


def lab_policy():
    text = (APP / "tests" / "mcp_sample" / "workspace.asp.json").read_text().replace("{tests}", str(APP / "tests"))
    data = json.loads(text)
    data["content-trust"]["taint-scope"] = "task"
    return ASPPolicy.model_validate(data)


def main():
    patch.object(auth, "auth_config", return_value=(ISSUER, {ORIGIN})).start()
    patch.object(auth, "key_client", return_value=SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=KEY.public_key()))).start()
    session = LiveSession(SessionInput(policy="mcp-demo", label="confinement lab"), owner="operator", policy=lab_policy())
    server.SESSIONS.sessions[session.id] = session
    decision, credential = session.mcp.mint("assistant", session.spec.root,
                                            ["mcp:web:get", "mcp:mail:send", "mcp:files:*", "mcp:shell:*"])
    assert credential, decision
    SECRETS.mkdir(parents=True, exist_ok=True)
    (SECRETS / "credential").write_text(credential)
    (SECRETS / "session").write_text(session.id)
    # The operator token stays in a volume the agent container does not mount.
    OPERATOR.mkdir(parents=True, exist_ok=True)
    (OPERATOR / "owner-token").write_text(owner_token())
    print(f"lab engine ready: session {session.id}", flush=True)
    uvicorn.run(server.app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
