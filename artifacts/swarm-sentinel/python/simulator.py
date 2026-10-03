"""Deterministic, invented samples. Never represents real incident evidence."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
from models import Event


def intent_vector(text):
    values = np.array(list(hashlib.sha256(text.lower().encode()).digest()[:8]), dtype=float)
    return np.round(values / np.linalg.norm(values), 5).tolist()


def generate(scenario):
    events = []
    lineage = {"orchestrator": ("", 0, "")}
    base = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)

    def emit(agent, action, target, intent, channel="in_band"):
        parent, depth, span = lineage.get(agent, ("", 0, ""))
        event_id = f"{scenario}-{len(events)+1:03d}"
        events.append(Event(
            id=event_id, timestamp=(base + timedelta(seconds=len(events)*.4)).isoformat(),
            agentId=agent, parentId=parent, spanId=f"span-{event_id}",
            parentSpanId=span, action=action, channel=channel, target=target,
            intent=intent, intentVector=intent_vector(intent),
            depth=depth + (1 if action == "spawn" else 0),
        ).model_dump())
        if action == "spawn":
            lineage[target] = (agent, depth+1, f"span-{event_id}")

    for agent in ["researcher", "reviewer", "publisher"]:
        emit("orchestrator", "spawn", agent, f"Delegate distinct task to {agent}")
    if scenario == "normal":
        for i, agent in enumerate(["researcher", "reviewer", "publisher"]):
            emit(agent, "tool", "mock:search.read", f"Research topic {i}")
            emit(agent, "wiki_edit", f"wiki:task-{i}", f"Document distinct finding {i}", "out_of_band")
            emit(agent, "tool", "mock:mcp:wiki.write", f"Save independent task {i}")
            emit(agent, "message", "orchestrator", f"Report completed task {i}")
        return events

    previous = "researcher"
    for child in ["research-2", "research-3", "research-4", "research-5"]:
        emit(previous, "spawn", child, "Repeat delegated validation")
        previous = child
    for agent in ["researcher", "reviewer", "publisher", "research-2", "research-3", "research-4"]:
        emit(agent, "tool", "mock:mcp:wiki.write", "answer confirmed")
    for i in range(8):
        emit("orchestrator", "tool", "mock:mcp:board.write", f"Post confirmation copy {i}")
    for agent in ["researcher", "reviewer", "publisher", "research-2"]:
        emit(agent, "wiki_edit", "wiki:shared-consensus", "task complete", "out_of_band")
    for agent in ["researcher", "reviewer", "publisher", "research-2"]:
        emit(agent, "tool", "mock:mcp:wiki.write", "answer confirmed")
    for source, target in [
        ("researcher", "reviewer"), ("reviewer", "publisher"), ("publisher", "researcher"),
        ("researcher", "publisher"), ("publisher", "reviewer"), ("reviewer", "researcher"),
    ]:
        emit(source, "message", target, "answer confirmed", "out_of_band")
    for child in ["worker-4", "worker-5", "worker-6"]:
        emit("orchestrator", "spawn", child, "Fan out further validation")
    for agent in ["orchestrator", "researcher", "reviewer"]:
        emit(agent, "tool", "mock:mcp:board.write", "task complete")
    return events


if __name__ == "__main__":
    folder = Path(__file__).parent / "samples"
    folder.mkdir(exist_ok=True)
    for scenario in ["normal", "attack"]:
        (folder / f"{scenario}.json").write_text(json.dumps(generate(scenario), indent=2))