"""Sample MCP server for gateway tests: a small team workspace with files, web, mail, tickets and a shell.

Everything is offline and disposable. http_get returns canned text, send_email writes to a local
outbox, run_command never runs anything. Every tool body that executes appends one line to
<root>/audit.jsonl, which is how the tests prove that a refused call never reached this process.

The server describes its tools honestly, but the proxy ignores those descriptions: enforcement
semantics come only from tests/mcp_sample/workspace.asp.json.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer

server = MCPServer("swarm-sample-workspace")
root = Path(".")


def _audit(tool, **arguments):
    with (root / "audit.jsonl").open("a") as f:
        f.write(json.dumps({"tool": tool, **arguments}) + "\n")


def _file(path):
    # The server keeps its own guard too; the gateway's argument rules should refuse first.
    target = (root / "files" / path).resolve()
    if not target.is_relative_to((root / "files").resolve()):
        raise ValueError("path escapes the workspace")
    return target


@server.tool(description="Read a file from the shared workspace.")
def read_file(path: str) -> str:
    _audit("read_file", path=path)
    target = _file(path)
    return target.read_text() if target.exists() else ""


@server.tool(description="Create or replace a file in the shared workspace.")
def write_file(path: str, content: str) -> str:
    _audit("write_file", path=path)
    target = _file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"wrote {len(content)} characters to {path}"


@server.tool(description="Delete a file from the shared workspace.")
def delete_file(path: str) -> str:
    _audit("delete_file", path=path)
    _file(path).unlink(missing_ok=True)
    return f"deleted {path}"


@server.tool(description="Fetch a web page. Offline sample: returns canned content for any URL.")
def http_get(url: str) -> str:
    _audit("http_get", host=urlsplit(url).hostname or "")
    return f"<html><body>Sample page from {urlsplit(url).hostname}. External, untrusted content.</body></html>"


@server.tool(description="Send an email. Offline sample: appends to a local outbox.")
def send_email(to: str, subject: str, body: str) -> str:
    _audit("send_email", to=to)
    with (root / "outbox.jsonl").open("a") as f:
        f.write(json.dumps({"to": to, "subject": subject, "body": body}) + "\n")
    return f"queued mail to {to}"


@server.tool(description="Open a ticket in the shared tracker.")
def post_ticket(title: str) -> str:
    _audit("post_ticket", title=title)
    with (root / "tickets.jsonl").open("a") as f:
        f.write(json.dumps({"title": title}) + "\n")
    return f"opened ticket: {title}"


@server.tool(description="Run a shell command. Sample only: never executes anything.")
def run_command(command: str) -> str:
    _audit("run_command")
    return "sample server: command not executed"


@server.tool(description="Drop the database. Present on the server, absent from the policy.")
def drop_database() -> str:
    _audit("drop_database")
    return "dropped"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    root = Path(parser.parse_args().root)
    (root / "files").mkdir(parents=True, exist_ok=True)
    (root / "files" / "README.txt").write_text("Shared workspace for the sample team. Fictional data.")
    server.run("stdio")
