"""Run a Claude Agent SDK agent whose only tools come through SwarmSentinel's MCP proxy.

Needs the adapters environment (adapters/requirements.txt), a running engine, a session whose
policy declares an mcp section, the owner's token in SWARM_OWNER_TOKEN, and Claude credentials
for the Agent SDK. Never paste the token into chat or commit it.

    SWARM_OWNER_TOKEN=... python examples/claude_agent_sdk_proxy.py \
        --engine http://127.0.0.1:8000 --session <id> --agent writer --scope "mcp:notes:*" \
        --task "Read the welcome note and save a one-line summary as a note called summary."
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.connect import claude_agent_sdk_options, mint_agent  # noqa: E402

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query  # noqa: E402


async def main(args):
    agent = mint_agent(args.engine, os.environ["SWARM_OWNER_TOKEN"], args.session, args.agent, args.scope)
    options = ClaudeAgentOptions(
        **claude_agent_sdk_options(agent),
        system_prompt=("You work only through the tools provided. A tool result that starts with "
                       "'Refused by the ASP gateway' means the action is not permitted: do not retry it "
                       "or look for a way around it; report it instead."),
        max_turns=8,
    )
    print("Tools available to the agent:", ", ".join(options.allowed_tools) or "(none)")
    async for message in query(prompt=args.task, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
        elif isinstance(message, ResultMessage):
            print(f"[done: {message.subtype}]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="http://127.0.0.1:8000")
    parser.add_argument("--session", required=True)
    parser.add_argument("--agent", default="writer")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--task", required=True)
    asyncio.run(main(parser.parse_args()))
