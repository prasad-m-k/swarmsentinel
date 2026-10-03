def markdown_report(run, settings):
    events, alerts = run["events"], run["alerts"]
    agents = {e["agentId"] for e in events}
    agents.update(e["target"] for e in events if e["action"] == "spawn" and e["executed"])
    resources = sorted({e["target"] for e in events if e["action"] in {"tool", "wiki_edit"} and e["executed"]})
    blocked = [e for e in events if not e["executed"]]
    involved = sorted({a for alert in alerts for a in alert["agents"]})
    lines = [
        "# SwarmSentinel — Forensic Incident Report", "",
        "**Provenance: synthetic demonstration, not AI Village records or real incident evidence.**", "",
        f"- Run: `{run['id']}`", f"- Scenario: {run['scenario']}",
        f"- Initiating agent: `{run['rootAgent']}` (known simulator lineage; not a causal inference)",
        f"- Initiation timestamp: {run['startedAt']}",
        f"- First detection: {alerts[0]['timestamp'] if alerts else 'No swarm pattern detected'}",
        f"- Observed agents: {len(agents)}",
        f"- Agents implicated by heuristics: {len(involved)} ({', '.join(involved) or 'none'})",
        f"- Resources reached: {', '.join(resources) or 'none'}",
        f"- Calls not executed: {len(blocked)}",
        f"- Automated feedback: {'enabled' if settings.feedbackEnabled else 'disabled'}",
        f"- Policy: depth cap {settings.maxDepth}, identical-intent limit {settings.semanticLimit}, write limit {settings.writeLimit}; windows 10 seconds",
        "", "## Detections",
    ]
    lines.extend(f"- {a['timestamp']} — {a['kind']}: {a['reason']}" for a in alerts)
    if not alerts:
        lines.append("- No heuristic detections.")
    lines += ["", "## Policy interventions"]
    lines.extend(f"- {e['id']} — {e['decision']}: {e['rule']} — {e['reason']}" for e in blocked)
    lines.extend(f"- Policy v{p['version']}: {p['reason']}; revoked: {', '.join(p['blockedAgents'])}" for p in run["policies"])
    lines += ["", "## Chronological flight recorder", "",
              "| UTC time | Actor | Action / target | Decision | Rule |",
              "|---|---|---|---|---|"]
    for e in events:
        lines.append(f"| {e['timestamp']} | {e['agentId']} | {e['action']} / {e['target']} | {e['decision']} | {e['rule']} |")
    lines += [
        "", "## Scope and limitations",
        "- Out-of-band edits and messages are observations, not intercepted tool calls. Revocation affects subsequent in-band mock calls only.",
        "- All tools are mock endpoints; no external APIs, accounts, MCP servers, or autonomous agents were invoked.",
        "- Identical-intent throttling uses normalized text equality, not learned semantic embeddings. Intent vectors are deterministic hash fingerprints.",
        "- Heuristic detections identify suspicious coordination, not proof of maliciousness or agent compromise.",
        "- Enforcement and recorder are separated in code but run in one prototype process; this is not a tamper-proof production boundary.",
    ]
    return "\n".join(lines) + "\n"