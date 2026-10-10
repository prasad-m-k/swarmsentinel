"""Load SwarmSentinel's MCP proxy tools into LangChain and call them.

Needs the adapters environment (adapters/requirements.txt), a running engine, a session whose
policy declares an mcp section, and the owner's token in SWARM_OWNER_TOKEN. No model is needed:
the tools are invoked directly to show that the gateway decides each call. Hand the same tools to
any LangChain agent to put a model in the loop.

    SWARM_OWNER_TOKEN=... python examples/langchain_mcp_proxy.py --engine http://127.0.0.1:8000 --session <id>
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.connect import langchain_connections, mint_agent  # noqa: E402

from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402


async def main(args):
    agent = mint_agent(args.engine, os.environ["SWARM_OWNER_TOKEN"], args.session, args.agent, args.scope)
    client = MultiServerMCPClient(langchain_connections(agent))
    tools = {tool.name: tool for tool in await client.get_tools()}
    print("Tools:", ", ".join(sorted(tools)))
    print(await tools["notes__write_note"].ainvoke({"name": "langchain", "text": "written through the proxy"}))
    print(await tools["notes__read_note"].ainvoke({"name": "langchain"}))
    # A private key in the arguments is refused by the gateway before the server sees it.
    print(await tools["notes__write_note"].ainvoke({"name": "key", "text": "-----BEGIN PRIVATE KEY-----"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="http://127.0.0.1:8000")
    parser.add_argument("--session", required=True)
    parser.add_argument("--agent", default="langchain-writer")
    parser.add_argument("--scope", action="append", default=["mcp:notes:*"])
    asyncio.run(main(parser.parse_args()))
