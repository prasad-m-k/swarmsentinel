"""Map AI Village dataset rows (SCHEMA.md, export 2026-09-20) onto SwarmSentinel events.

Every event keeps a sourceRef back to its table row. Agent narration (chat text, bash comments)
is carried as the event intent, which is a claim made by the agent, not verified ground truth.
"""
import re
from datetime import datetime, timezone
from functools import cache
from zoneinfo import ZoneInfo
from simulator import intent_vector

ROOT = "village"
HUMAN = "human"
PERMA_COMPUTER_USE = "2026-03-24"   # CHANGELOG: biggest structural regime change
AUTO_NUDGER = "2026-02-10"          # CHANGELOG: scaffold-generated nudges appear in chat after this

URL = re.compile(r"https?://([A-Za-z0-9.-]+)(?::\d+)?(/[^\s\"'<>)\]]*)?")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(\+?\d[\d ().-]{8,}\d)(?!\d)")
SECRET = re.compile(r"\b(gh[pousr]_\w{20,}|glpat-[\w-]{16,}|sk-[\w-]{20,}|AKIA[0-9A-Z]{16}|xox[abpr]-[\w-]{10,}|eyJ[\w-]{20,}\.[\w-]+\.[\w-]+)|(?i:bearer\s+)[\w.-]{20,}")
COMMENT = re.compile(r"^\s*#+\s*(.+)$", re.M)
MUTATING_CLI = re.compile(r"\b(gh|glab)\s+(issue|pr|mr|release|repo|variable|label|snippet)\s+(create|edit|comment|merge|close|note|update|delete|set|fork)\b")
MUTATING_API = re.compile(r"\b(gh|glab)\s+api\b[^\n]*(-X\s*(POST|PUT|PATCH|DELETE)|\s-[fF]\s|--field|--raw-field|--input)")
CURL_MUTATE = re.compile(r"\bcurl\b[^\n|;]*(-X\s*(POST|PUT|PATCH|DELETE)|\s--data\S*\s|\s-d\s|\s-F\s|--form\s|--upload-file|-T\s)")
FETCH = re.compile(r"\b(curl|wget|http|lynx)\b")
REPO_URL = re.compile(r"(?:gitlab\.com|github\.com)[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/\s\"']|$)")
REPO_FLAG = re.compile(r"(?:-R|--repo)\s+([\w.-]+)/([\w.-]+)")
REPO_API = re.compile(r"(?:repos|projects)/([\w.-]+)(?:/|%2F)([\w.-]+)", re.I)
CD = re.compile(r"\bcd\s+([^\s;&|]+)")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}


def iso(ts):
    """Dataset timestamps are naive UTC strings with microseconds."""
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).isoformat()


@cache
def _pacific():
    # Resolved lazily so the engine starts on hosts without a timezone database (synthetic-only deployments).
    return ZoneInfo("America/Los_Angeles")


def pacific_day(ts):
    """The village's calendar day; screenshot tars and summaries use Pacific time."""
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).astimezone(_pacific()).date().isoformat()


def regime(ts):
    return "perma-computer-use" if ts[:10] >= PERMA_COMPUTER_USE else "discrete-sessions"


def scrub(text, limit):
    text = SECRET.sub("[REDACTED]", text or "")
    text = EMAIL.sub("[email]", text)
    text = PHONE.sub("[phone]", text)
    return text[:limit]


def one_line(text, limit=160):
    return scrub(" ".join((text or "").split()), limit)


def _repo_name(name):
    name = re.sub(r"[^\w.-]", "", name.rstrip("/").rsplit("/", 1)[-1]).lower().removesuffix(".git")
    return f"repo:{name}" if name else ""


def _repo(match):
    return _repo_name(match.group(2))


def analyze_bash(command):
    """Classify a shell command: network destination, untrusted reads, and writes to shared state."""
    cmd = command or ""
    host = next((m.group(1).lower() for m in URL.finditer(cmd)), "")
    comment = COMMENT.search(cmd)
    out = dict(intent=one_line(comment.group(1) if comment else cmd.strip().splitlines()[0] if cmd.strip() else "bash"),
               network=host, reads="", write=False, resource="")
    repo = REPO_FLAG.search(cmd) or REPO_API.search(cmd) or REPO_URL.search(cmd)
    if re.search(r"\bgit\s+push\b", cmd):
        dirs = CD.findall(cmd)
        out.update(write=True, resource=_repo(repo) if repo else _repo_name(dirs[-1]) if dirs else "")
    elif MUTATING_CLI.search(cmd) or MUTATING_API.search(cmd):
        out.update(write=True, resource=_repo(repo) if repo else "")
    elif CURL_MUTATE.search(cmd) and host and host not in LOCAL_HOSTS:
        path = next((m.group(2) or "" for m in URL.finditer(cmd)), "").strip("/").split("/")[0]
        out.update(write=True, resource=f"web:{host}/{path}" if path else f"web:{host}")
    elif host and host not in LOCAL_HOSTS and FETCH.search(cmd):
        out["reads"] = "untrusted"
    elif re.search(r"\bgit\s+(clone|pull|fetch)\b", cmd):
        out["reads"] = "low"
    return out


class Normalizer:
    def __init__(self, agents, sessions, rooms):
        self.agents = agents            # agent uuid -> (name, joined_at)
        self.sessions = sessions        # session uuid -> agent uuid
        self.rooms = rooms              # room uuid -> name
        names = sorted({n for n, _ in agents.values()}, key=len, reverse=True)
        self.mention = re.compile(r"(?<![\w.-])@?(" + "|".join(re.escape(n) for n in names) + r")(?![\w-]|\.\d)")
        self.skipped = {}

    def _skip(self, kind):
        self.skipped[kind] = self.skipped.get(kind, 0) + 1

    def _event(self, eid, ts, agent, span, action, target, intent, ref, **extra):
        human = agent == HUMAN
        return dict(
            id=eid, timestamp=iso(ts), agentId=agent, parentId="" if human else ROOT,
            spanId=span, parentSpanId="" if human else f"admit:{agent}", action=action,
            channel="out_of_band" if human else "in_band", target=target, intent=intent,
            intentVector=intent_vector(intent), depth=1, source="ai-village", sourceRef=ref, **extra,
        )

    def _mentions(self, text, speaker):
        return sorted({m for m in self.mention.findall(text or "") if m != speaker})

    def chat(self, row):
        room = self.rooms.get(row["room_id"], "unknown")
        if row["speaker_type"] == "agent" and row["agent_speaker_id"] in self.agents:
            speaker = self.agents[row["agent_speaker_id"]][0]
            intent = one_line(row["content"], 240)
        else:
            speaker, intent = HUMAN, "[human or scaffold message; text withheld]"
        mentions = self._mentions(row["content"], speaker)
        return self._event(f"chat-{row['id'][:12]}", row["created_at"], speaker, f"chat:{speaker}", "message",
                           mentions[0] if mentions else f"room:{room}", intent, f"chat_messages:{row['id']}",
                           mentions=mentions, detail=f"room {room}")

    def _tool(self, eid, ts, agent, span, tool, ref, action):
        """Shared mapping for computer-use agent_action dicts and Claude Code tool inputs."""
        if "command" in action and isinstance(action["command"], str):
            info = analyze_bash(action["command"])
            return self._event(eid, ts, agent, span, "tool", tool, info.pop("intent"), ref,
                               detail=scrub(action["command"], 1500), **info)
        kind = action.get("action")
        text = action.get("text") if isinstance(action.get("text"), str) else ""
        if kind == "type" and URL.search(text):
            host = URL.search(text).group(1).lower()
            return self._event(eid, ts, agent, span, "tool", "village:computer.navigate", f"navigate to {host}", ref,
                               detail=scrub(text.strip(), 300), network=host,
                               reads="" if host in LOCAL_HOSTS else "untrusted")
        mapped = {
            "search_history": ("village:search_history", action.get("query", "")),
            "move_to_room": ("village:move_to_room", f"move to {action.get('roomName', '')}"),
            "request_approval_for_unsolicited_outreach": ("village:outreach.request", f"request outreach approval via {action.get('medium', 'unknown')}"),
            "request_Google_sign_in": ("village:google_sign_in.request", "request Google sign-in hand-off"),
            "request_human_helper": ("village:human_helper.request", "request a human helper"),
            "cancel_request_for_human_helper": ("village:human_helper.cancel", "cancel human helper request"),
            "view_clipboard": ("village:clipboard.read", "read clipboard"),
        }.get(kind)
        if not mapped:
            self._skip(f"gui:{kind}")
            return None
        target, intent = mapped
        return self._event(eid, ts, agent, span, "tool", target, one_line(intent), ref,
                           write=target == "village:outreach.request")

    def turn(self, row):
        action = row.get("agent_action")
        agent = self.agents.get(self.sessions.get(row["session_id"], ""), (None,))[0]
        if not isinstance(action, dict) or not agent:
            self._skip("turn:no-action" if agent else "turn:unknown-session")
            return None
        tool = "village:bash" if "command" in action else f"village:computer.{action.get('action')}"
        return self._tool(f"cu-{row['id'][:12]}", row["created_at"], agent, f"cu:{row['session_id'][:8]}",
                          tool, f"computer_use_turns:{row['id']}", action)

    CLAUDE_CODE = {
        "mcp__village__search_history": "search_history",
        "mcp__village__move_to_room": "move_to_room", "mcp__village__request_google_sign_in": "request_Google_sign_in",
    }

    def claude_code(self, row):
        """Yield tool calls from one Claude Agent SDK assistant message."""
        message = (row.get("content") or {}).get("message") or {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        agent = self.agents.get(row["agent_id"], (None,))[0]
        span = f"cc:{(row.get('sdk_session_id') or '')[:8]}"
        for block in blocks:
            if block.get("type") != "tool_use" or not agent:
                continue
            name, args = block["name"], block.get("input") or {}
            eid, ref = f"cc-{block.get('id', row['id'])[-12:]}", f"claude_code_messages:{row['id']}"
            if name in ("Bash", "mcp__village__bash"):
                yield self._tool(eid, row["created_at"], agent, span, "cc:Bash" if name == "Bash" else "village:bash", ref, args)
            elif name in self.CLAUDE_CODE:
                event = self._tool(eid, row["created_at"], agent, span, "", ref, {**args, "action": self.CLAUDE_CODE[name],
                                   "roomName": args.get("roomName", args.get("room", ""))})
                if event:
                    yield event
            elif name == "mcp__village__computer_use":
                event = self._tool(eid, row["created_at"], agent, span, "", ref, args)
                if event:
                    yield event
            elif name in ("Write", "Edit"):
                yield self._event(eid, row["created_at"], agent, span, "tool", f"cc:{name}", f"{name.lower()} {args.get('file_path', '')}"[:160], ref,
                                  detail=scrub(args.get("file_path", ""), 300), write=True)
            elif name in ("Read", "Grep", "Glob"):
                yield self._event(eid, row["created_at"], agent, span, "tool", f"cc:{name}", one_line(f"{name.lower()} {args.get('file_path') or args.get('pattern', '')}"), ref,
                                  detail=scrub(str(args.get("file_path") or args.get("pattern", "")), 300))
            elif name in ("WebFetch", "WebSearch"):
                url = args.get("url", "")
                host = URL.search(url).group(1).lower() if URL.search(url) else ""
                yield self._event(eid, row["created_at"], agent, span, "tool", f"cc:{name}", one_line(f"{name}: {host or args.get('query', '')}"), ref,
                                  detail=scrub(url or args.get("query", ""), 300), network=host, reads="untrusted")
            elif name == "mcp__village__edit_memory":
                yield self._event(eid, row["created_at"], agent, span, "tool", "village:memory.edit", f"edit memory ({args.get('action', '')})", ref, write=True)
            else:
                self._skip(f"cc:{name}")
