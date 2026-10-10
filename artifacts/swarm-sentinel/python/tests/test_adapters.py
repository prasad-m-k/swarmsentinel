"""Framework adapters for MCP proxy mode: connection builders, minting, and real SDK interop.

The SDK interop test runs LangChain's MCP adapter and the Claude Agent SDK's option types in their
own environment (they pin mcp 1.x; the engine uses 2.x). Point SWARM_ADAPTERS_PYTHON at that
interpreter, or create .venv-adapters at the repo root; otherwise the interop test is skipped.
"""
import json
import os
import subprocess
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import auth
import server
import test_access as access
from adapters.connect import (ProxyAgent, claude_agent_sdk_options, claude_code_mcp_json, langchain_connections,
                              list_tools, mint_agent, _post)
from live import SessionStore
from test_mcp_proxy import running_engine

ROOT = Path(__file__).resolve().parents[4]
PYTHON_DIR = Path(__file__).resolve().parents[1]


def adapters_python():
    candidate = os.environ.get("SWARM_ADAPTERS_PYTHON") or str(ROOT / ".venv-adapters" / "bin" / "python")
    return candidate if Path(candidate).exists() else None


class ConnectionBuilders(unittest.TestCase):
    agent = ProxyAgent("http://127.0.0.1:8000/", "writer", "ssa_test-credential")

    def test_claude_agent_sdk_options_confine_the_agent(self):
        options = claude_agent_sdk_options(self.agent, tools=["notes__read_note", "notes__write_note"])
        self.assertEqual(options["tools"], [])
        self.assertTrue(options["strict_mcp_config"])
        self.assertEqual(options["setting_sources"], [])
        self.assertEqual(options["mcp_servers"], {"swarm": {"type": "http", "url": "http://127.0.0.1:8000/api/swarm/mcp",
                                                            "headers": {"Authorization": "Bearer ssa_test-credential"}}})
        self.assertEqual(options["allowed_tools"], ["mcp__swarm__notes__read_note", "mcp__swarm__notes__write_note"])

    def test_langchain_and_mcp_json(self):
        self.assertEqual(langchain_connections(self.agent)["swarm"]["transport"], "streamable_http")
        self.assertEqual(claude_code_mcp_json(self.agent)["mcpServers"]["swarm"]["type"], "http")

    def test_credentials_stay_out_of_reprs_and_plain_http(self):
        self.assertNotIn("ssa_test-credential", repr(self.agent))
        with self.assertRaises(ValueError):
            _post("http://engine.example.com/api/swarm/mcp", {}, self.agent.headers)


class AdaptersAgainstEngine(unittest.TestCase):
    token = access.AccessControl.token

    @classmethod
    def setUpClass(cls):
        access.AccessControl.setUpClass.__func__(cls)
        cls.patchers = [
            patch.object(auth, "auth_config", return_value=(access.ISSUER, {access.ORIGIN})),
            patch.object(auth, "key_client", return_value=SimpleNamespace(
                get_signing_key_from_jwt=lambda token: SimpleNamespace(key=cls.key.public_key()))),
            patch.multiple(server, SESSIONS=SessionStore()),
        ]
        for patcher in cls.patchers:
            patcher.start()
        cls.engine = running_engine()
        cls.base = cls.engine.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.engine.__exit__(None, None, None)
        for patcher in reversed(cls.patchers):
            patcher.stop()

    def setUp(self):
        owner = self.token("owner")
        self.session_id = _post(f"{self.base}/api/swarm/sessions", {"policy": "mcp-demo"},
                                {"Authorization": f"Bearer {owner}"})["sessionId"]
        self.addCleanup(server.SESSIONS.delete, self.session_id)
        self.agent = mint_agent(self.base, owner, self.session_id, "writer", ["mcp:notes:*"])

    def test_mint_and_list_respect_scope(self):
        self.assertTrue(self.agent.credential.startswith("ssa_"))
        self.assertEqual(list_tools(self.agent), ["notes__read_note", "notes__write_note"])
        options = claude_agent_sdk_options(self.agent)
        self.assertEqual(options["allowed_tools"], ["mcp__swarm__notes__read_note", "mcp__swarm__notes__write_note"])

    @unittest.skipUnless(adapters_python(), "adapters environment not installed (see adapters/requirements.txt)")
    def test_langchain_and_agent_sdk_interop(self):
        script = textwrap.dedent("""
            import asyncio, json, os, sys
            sys.path.insert(0, os.environ["PYTHON_DIR"])
            from adapters.connect import ProxyAgent, claude_agent_sdk_options, langchain_connections
            from claude_agent_sdk import ClaudeAgentOptions
            from langchain_mcp_adapters.client import MultiServerMCPClient

            agent = ProxyAgent(os.environ["ENGINE"], "writer", os.environ["CREDENTIAL"])

            async def main():
                tools = {t.name: t for t in await MultiServerMCPClient(langchain_connections(agent)).get_tools()}
                saved = await tools["notes__write_note"].ainvoke({"name": "lc", "text": "via langchain"})
                read = await tools["notes__read_note"].ainvoke({"name": "lc"})
                refused = await tools["notes__write_note"].ainvoke({"name": "k", "text": "-----BEGIN PRIVATE KEY-----"})
                options = ClaudeAgentOptions(**claude_agent_sdk_options(agent))
                print(json.dumps({"tools": sorted(tools), "saved": str(saved), "read": str(read),
                                  "refused": str(refused), "allowed": options.allowed_tools,
                                  "builtins": options.tools, "strict": options.strict_mcp_config}))
            asyncio.run(main())
        """)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "PYTHON_DIR": str(PYTHON_DIR),
               "ENGINE": self.base, "CREDENTIAL": self.agent.credential}
        run = subprocess.run([adapters_python(), "-I", "-c", script], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        out = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(out["tools"], ["notes__read_note", "notes__write_note"])
        self.assertIn("via langchain", out["read"])
        self.assertIn("tools.deny_arguments", out["refused"])
        self.assertEqual(out["allowed"], ["mcp__swarm__notes__read_note", "mcp__swarm__notes__write_note"])
        self.assertEqual((out["builtins"], out["strict"]), ([], True))
        session = server.SESSIONS.get(self.session_id)
        rows = [(t.agentId, t.target, t.decision) for t in session.gateway.telemetry if t.action == "tool"]
        self.assertEqual(rows, [("writer", "mcp:notes:write", "allow"), ("writer", "mcp:notes:read", "allow"),
                                ("writer", "mcp:notes:write", "drop")])


if __name__ == "__main__":
    unittest.main()
