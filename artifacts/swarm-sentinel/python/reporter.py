TABLE_LIMIT = 300


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def markdown_report(run, settings):
    events, alerts, summary = run["events"], run["alerts"], run["summary"]
    village = run["source"] == "ai-village"
    report_only = run["mode"] == "report-only"
    evidence = run.get("executionEvidence", False)
    agents = {e["agentId"] for e in events}
    agents.update(e["target"] for e in events if e["action"] == "spawn" and e["executed"])
    resources = sorted({e["target"] if e["action"] == "wiki_edit" else e["resource"] or e["target"]
                        for e in events if (e.get("executionStatus") == "succeeded" if evidence else e["executed"]) and (e["action"] == "wiki_edit" or (e["action"] == "tool" and e["write"] is not False and (e["resource"] or e["target"].endswith(".write"))))})
    blocked = [e for e in events if e["decision"] in {"drop", "throttle"}]
    involved = sorted({a for alert in alerts for a in alert["agents"]})
    policy = run["policy"]
    lines = ["# SwarmSentinel — Forensic Incident Report", "", f"**Provenance: {run['provenance']}**", ""]
    if village:
        w = run["window"]
        lines += [
            f"- Dataset: `{w['dataset']}`, export {w['exportedAt']}. Cite as: {w['citation']}",
            f"- Replay window (UTC): {w['start']} to {w['end']}" + (" (truncated at event cap)" if w["truncated"] else ""),
            f"- Village day (Pacific): {w['day']}; scaffolding regime: {w['regime']}",
            "- Mode: **report-only**. Every action below already happened. Drop and throttle decisions show what the ASP policy *would* have done; they are counterfactual, not evidence of prevention.",
        ]
    lines += [
        f"- Run: `{run['id']}`", f"- Scenario: {run['scenario']}",
        f"- Root: `{run['rootAgent']}` ({'dataset registry of admitted agents' if village else 'known simulator lineage; not a causal inference'})",
        f"- Initiation timestamp: {run['startedAt']}",
        f"- First detection: {alerts[0]['timestamp'] if alerts else 'No swarm pattern detected'}",
        f"- Observed actors: {len(agents)}",
        f"- Agents implicated by heuristics: {len(involved)} ({', '.join(involved) or 'none'})",
        f"- {'Shared resources with successful tool writes' if evidence else 'Shared resources written'}: {', '.join(resources[:25]) or 'none'}{' …' if len(resources) > 25 else ''}" + (" (includes caller-reported claims; not proof of side effects)" if evidence else ""),
        f"- Calls {'the policy would have blocked' if report_only else 'not executed'}: {len(blocked)}",
        f"- {'Proposed writes' if evidence else 'Writes'} from contexts that had read untrusted content: {summary['contaminatedWrites']} of {summary['writes']}",
        f"- Automated feedback: {'enabled' if settings.feedbackEnabled else 'disabled'}",
        f"- Policy `{run['policyName']}`: depth cap {policy['delegation']['max-depth']}, repeated-intent limit {policy['swarm']['repeated-intent']['limit']}, write cap {policy['swarm']['write-cap']}; window {policy['swarm']['window-seconds']} seconds",
        "", "## Detections",
    ]
    lines.extend(f"- {a['timestamp']} — {a['kind']}: {a['reason']} ({', '.join(a['agents'])})" for a in alerts)
    if not alerts:
        lines.append("- No heuristic detections.")
    if evidence:
        tools = [e for e in events if e["action"] == "tool"]
        lines += ["", "## Tool execution evidence",
                  "- `executed` is gateway authorization, not proof of completion. Permission-only allowed calls have no observed outcome.",
                  "- `executionProvenance` distinguishes engine-observed execution from caller-reported completion. Caller reports are owner assertions, not engine verification or proof of side effects.",
                  "- Not-started calls never entered the tool body. Failed bodies may have partial side effects; failure does not prove rollback.",
                  "- Outcomes contain no tool results, exception text, credentials, or sandbox paths."]
        for status in ("not-started", "succeeded", "failed"):
            lines.append(f"- {status}: {sum(e.get('executionStatus') == status for e in tools)}")
        lines.append(f"- Outcome not observed: {sum(not e.get('executionStatus') for e in tools)}")
        for provenance in ("engine-observed", "caller-reported"):
            lines.append(f"- {provenance}: {sum(e.get('executionProvenance') == provenance for e in tools)}")
        warnings = run.get("completionWarnings", [])
        lines += ["", "## Missing-completion warnings (visibility only)",
                  f"- Grace period: {run.get('completionGraceSeconds', 60)} seconds after server admission.",
                  "- An overdue receipt means completion has not been reported. It does not establish body entry, failure, rollback, or retry safety. A slow tool may still be running.",
                  "- These warnings are separate from heuristic detections and admission-based enforcement; a late receipt clears the warning."]
        lines.extend(f"- Missing completion: `{_cell(w['eventId'])}` — {_cell(w['agentId'])} / {_cell(w['target'])}; admitted at {w['admittedAt']}."
                     for w in warnings[:TABLE_LIMIT])
        if not warnings:
            lines.append("- No overdue completion receipts.")
        if len(warnings) > TABLE_LIMIT:
            lines.append(f"- … {len(warnings) - TABLE_LIMIT} more in the JSON export.")
    lines += ["", "## Policy interventions" + (" (counterfactual)" if report_only else "")]
    lines.extend(f"- {rule}: {n}" for rule, n in summary["blockedByRule"].items())
    lines.extend(f"- advisory {d}: {n} (reported, not blocked)" for d, n in summary["advisories"].items())
    lines.extend(f"- {e['id']} — {e['decision']}: {e['rule']} — {_cell(e['reason'])}" for e in blocked[:TABLE_LIMIT // 3])
    if len(blocked) > TABLE_LIMIT // 3:
        lines.append(f"- … {len(blocked) - TABLE_LIMIT // 3} more in the JSON trace export")
    lines.extend(f"- Policy v{p['version']}: {p['reason']}; revoked: {', '.join(p['blockedAgents'])}" for p in run["policies"])
    lines += ["", "## Heaviest interactions", "",
              ("Authorized actions collapsed to one edge per actor and target; weight counts admissions, not successful executions."
               if evidence else "Executed actions collapsed to one edge per actor and target; weight counts repeats."), "",
              "| Actor | Target | Weight | Actions |", "|---|---|---|---|"]
    for e in run["edges"][:15]:
        actions = ", ".join(f"{a} {n}" for a, n in sorted(e["actions"].items(), key=lambda kv: -kv[1]))
        lines.append(f"| {_cell(e['source'])} | {_cell(e['target'])} | {e['weight']} | {actions} |")
    if not run["edges"]:
        lines.append("| none | | | |")
    lines += ["", "## Chronological flight recorder", "",
              ("| UTC time | Actor | Action / target | Decision | Rule | Source | Authorized | Tool outcome | Body entered | Error | Evidence provenance |"
               if evidence else "| UTC time | Actor | Action / target | Decision | Rule | Source |"),
              ("|---|---|---|---|---|---|---|---|---|---|---|" if evidence else "|---|---|---|---|---|---|")]
    for e in events[:TABLE_LIMIT]:
        row = f"| {e['timestamp']} | {_cell(e['agentId'])} | {e['action']} / {_cell(e['target'])} | {e['decision']} | {e['rule']} | {e['sourceRef'] or ('live' if evidence else 'synthetic')} |"
        if evidence:
            status = e.get("executionStatus", "not observed" if e["action"] == "tool" else "not applicable")
            body = str(e["toolBodyExecuted"]).lower() if "toolBodyExecuted" in e else "not observed"
            row += f" {'yes' if e['executed'] else 'no'} | {status} | {body} | {e.get('executionError', '')} | {e.get('executionProvenance', 'not observed')} |"
        lines.append(row)
    if len(events) > TABLE_LIMIT:
        lines.append(f"\n{len(events) - TABLE_LIMIT} further events are in the JSON trace export.")
    lines += ["", "## Scope and limitations"]
    if village:
        lines += [
            "- Replay of historical records. The gateway did not exist when these actions ran; nothing was blocked or contained.",
            "- Agent narration (chat text, bash comments) is the agent's own claim. Verify against screenshots before treating it as ground truth.",
            "- GUI micro-actions (clicks, scrolls, screenshots) are aggregated, not replayed. Shared-state writes are inferred from shell commands, so GUI edits to shared documents are not seen.",
            "- Human messages are anonymized into one `human` actor with text withheld. After 2026-02-10 some of them may be scaffold-generated nudges.",
            "- Behaviour shifts across the 2026-03-24 perma-computer-use change reflect scaffolding, not only agent behaviour. Read the dataset CHANGELOG before comparing periods.",
            "- No sub-agent spawning exists in this export, so recursion-depth and fan-out tripwires cannot fire on it.",
        ]
    elif run["source"] == "live":
        lines += [
            ("- Registered sandbox tools are dispatched by the engine after authorization; completion is observed by the engine. Permission-only calls rely on the caller honouring the decision; explicit completion receipts are caller-reported, otherwise the outcome remains unobserved."
             if evidence else "- Live session: each decision was returned to the calling agent before it acted. Enforcement relies on the caller honouring it; the SwarmSentinel guard wrapper refuses to run a denied call."),
            "- The gateway runs in the engine process, not an isolated boundary the agent cannot reach.",
        ]
    else:
        lines += [
            "- Out-of-band edits and messages are observations, not intercepted tool calls. Revocation affects subsequent in-band mock calls only.",
            "- All tools are mock endpoints; no external APIs, accounts, MCP servers, or autonomous agents were invoked.",
        ]
    lines += [
        "- Repeated-intent throttling uses normalized text equality. The echo detector uses token-set overlap, not learned semantic embeddings; intent vectors are deterministic hash fingerprints.",
        "- Heuristic detections identify suspicious coordination, not proof of maliciousness or agent compromise.",
        "- Enforcement and recorder are separated in code but run in one prototype process; this is not a tamper-proof production boundary.",
    ]
    return "\n".join(lines) + "\n"
