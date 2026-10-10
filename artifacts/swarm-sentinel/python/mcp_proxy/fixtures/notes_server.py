"""Disposable MCP fixture server for proxy mode. Offline only: fetch_page never touches the network.

Every tool body that runs appends one line to <root>/calls.jsonl, so tests can prove a refused
call never reached this process.
"""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer

NAME = re.compile(r"^[a-z0-9-]{1,40}$")
server = MCPServer("swarm-notes-fixture")
root = Path(".")


def _record(tool, **arguments):
    with (root / "calls.jsonl").open("a") as f:
        f.write(json.dumps({"tool": tool, **arguments}) + "\n")


def _note(name):
    if not NAME.match(name):
        raise ValueError("note names use lowercase letters, digits and hyphens")
    return root / "notes" / f"{name}.txt"


@server.tool(description="Read a note by name.")
def read_note(name: str) -> str:
    _record("read_note", name=name)
    path = _note(name)
    return path.read_text() if path.exists() else ""


@server.tool(description="Create or replace a note.")
def write_note(name: str, text: str) -> str:
    _record("write_note", name=name)
    path = _note(name)
    path.write_text(text)
    return f"saved {name}"


@server.tool(description="Fetch a web page (offline fixture content).")
def fetch_page(url: str) -> str:
    _record("fetch_page", host=urlsplit(url).hostname or "")
    return f"Fixture page for {urlsplit(url).hostname}. External content, offline copy."


@server.tool(description="Delete every note. Not declared in any policy, so the proxy never exposes it.")
def delete_all() -> str:
    _record("delete_all")
    for path in (root / "notes").glob("*.txt"):
        path.unlink()
    return "deleted"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    root = Path(parser.parse_args().root)
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "welcome.txt").write_text("Shared team notes. Fictional fixture data.")
    server.run("stdio")
