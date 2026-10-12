"""Figures for the TAISAP manuscript. Grayscale-safe ACM palette, hatching as secondary encoding, JPEG 300 dpi."""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
DATA = Path(sys.argv[2])

PRIMARY, TEAL, GOLD, LIGHT, DARK = "#1F4E5B", "#4A7C7A", "#B8860B", "#E2E8F0", "#1A202C"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": DARK,
                     "axes.labelcolor": DARK, "xtick.color": DARK, "ytick.color": DARK,
                     "axes.spines.top": False, "axes.spines.right": False, "savefig.dpi": 300})


def save(fig, name):
    fig.savefig(OUT / f"{name}.jpg", format="jpg", dpi=300, bbox_inches="tight", pad_inches=0.04,
                pil_kwargs={"quality": 95})
    plt.close(fig)


def box(ax, x, y, w, h, text, fill, color="white", hatch=None, lw=1.0, size=8.5, weight="bold"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                fc=fill, ec=DARK, lw=lw, hatch=hatch))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", color=color, fontsize=size, weight=weight,
            linespacing=1.25)


def arrow(ax, a, b, style="-|>", ls="-", color=DARK, lw=1.1, rad=0.0):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=10, lw=lw, color=color,
                                 linestyle=ls, connectionstyle=f"arc3,rad={rad}"))


# Figure 1: architecture: trust zones, identity and tasks, two monitor paths
fig, ax = plt.subplots(figsize=(7.0, 5.6))
ax.set_xlim(0, 12.6); ax.set_ylim(0, 10.0); ax.axis("off")
def zone(x0, y0, w, h, title, fill, ls):
    ax.add_patch(FancyBboxPatch((x0, y0), w, h, boxstyle="round,pad=0.02,rounding_size=0.12", fc=fill, ec=DARK, lw=0.9, ls=ls))
    ax.text(x0 + w / 2, y0 + h - 0.32, title, ha="center", fontsize=7.5, weight="bold", style="italic", color=DARK)
zone(0.1, 1.4, 3.75, 8.45, "Untrusted: agent host", "#F4F6F8", "--")
zone(4.15, 0.15, 4.3, 9.7, "Trusted: engine and monitors", LIGHT, "-")
zone(8.75, 1.4, 3.75, 8.45, "Outside the engine", "#FFFFFF", ":")
box(ax, 0.3, 7.75, 1.6, 0.95, "User input\n(medium)", TEAL, size=7)
box(ax, 2.05, 7.75, 1.6, 0.95, "Retrieved\n(untrusted)", TEAL, size=7, hatch="////")
box(ax, 0.3, 5.3, 3.35, 1.55, "Agent context window", PRIMARY, size=7.5)
arrow(ax, (1.1, 7.75), (1.1, 6.85)); arrow(ax, (2.85, 7.75), (2.85, 6.85))
box(ax, 0.35, 2.4, 3.3, 1.2, "Agent credential:\nengine-issued, one per task", "#FFFFFF", color=DARK, size=6.5, weight="normal")
ax.text(2.0, 4.0, "sent with every request\non both paths", ha="center", fontsize=6.3, color=DARK)
box(ax, 4.45, 7.55, 3.7, 1.3, "Egress monitor\nmodel-API path", GOLD, size=7.8)
box(ax, 4.45, 5.25, 3.7, 1.45, "Identity, task and\nlabel registry", "#FFFFFF", color=DARK, size=7.3)
box(ax, 4.45, 3.0, 3.7, 1.3, "ASP gateway\ntool path", GOLD, size=7.8)
box(ax, 4.45, 0.45, 1.75, 1.4, "Sentinel\ntripwires", TEAL, size=7, hatch="..")
box(ax, 6.4, 0.45, 1.75, 1.4, "Flight\nrecorder", "#FFFFFF", color=DARK, size=7)
box(ax, 9.0, 7.55, 3.25, 1.3, "Model providers", PRIMARY, size=7.5)
box(ax, 9.0, 3.0, 3.25, 1.3, "Tools and MCP servers", PRIMARY, size=7.5)
box(ax, 9.0, 5.25, 3.25, 1.45, "Operator (trusted)\nopens tasks, mints\ncredentials", "#FFFFFF", color=DARK, size=6.8, weight="normal")
# Model-API path: dashed; tool path: solid. Both carry the credential.
arrow(ax, (3.65, 6.6), (4.45, 8.0), ls="--", lw=1.4)
arrow(ax, (8.15, 8.2), (9.0, 8.2), ls="--", lw=1.4); ax.text(8.57, 8.38, "allow", ha="center", fontsize=6.3)
arrow(ax, (3.65, 5.55), (4.45, 3.9), lw=1.4)
# Legend for the two paths
ax.plot([0.45, 1.25], [1.95, 1.95], color=DARK, lw=1.4); ax.text(1.4, 1.95, "tool path", fontsize=6.5, va="center")
ax.plot([0.45, 1.25], [1.6, 1.6], color=DARK, lw=1.4, ls="--"); ax.text(1.4, 1.6, "model-API path", fontsize=6.5, va="center")
arrow(ax, (8.15, 3.65), (9.0, 3.65), lw=1.4); ax.text(8.57, 3.83, "allow", ha="center", fontsize=6.3)
# Registry links
arrow(ax, (6.3, 7.55), (6.3, 6.7), style="<|-|>", ls=":"); ax.text(6.18, 7.12, "identity, task,\nlabels", fontsize=6, va="center", ha="right")
arrow(ax, (6.3, 4.3), (6.3, 5.25), ls=":"); ax.text(6.42, 4.78, "taint and labels", fontsize=6, va="center")
arrow(ax, (9.0, 5.97), (8.15, 5.97)); ax.text(8.57, 6.15, "tasks", ha="center", fontsize=6.3)
arrow(ax, (5.3, 3.0), (5.3, 1.85)); ax.text(5.42, 2.42, "executed", fontsize=6, va="center")
arrow(ax, (4.6, 1.85), (4.6, 5.25), ls=":", rad=0.0); ax.text(4.68, 2.8, "", fontsize=6)
ax.text(4.28, 2.15, "revoke", fontsize=6, rotation=90, va="bottom")
arrow(ax, (7.3, 3.0), (7.3, 1.85)); ax.text(7.42, 2.42, "decisions", fontsize=6, va="center")
arrow(ax, (8.05, 7.55), (8.05, 1.85), ls="--", lw=0.8)
box(ax, 9.0, 0.25, 3.25, 1.0, "Policy files\nread by monitors only", "#FFFFFF", color=DARK, size=6.5, weight="normal")
arrow(ax, (9.0, 0.72), (8.45, 0.72))
save(fig, "fig1_architecture")

# Figure 2: taint flow in the injection scenario
fig, ax = plt.subplots(figsize=(7.0, 2.9))
ax.set_xlim(0, 12.4); ax.set_ylim(0, 5.0); ax.axis("off")
box(ax, 0.1, 3.3, 2.2, 1.0, "Invoice\n(untrusted)", TEAL, size=7.5, hatch="////")
box(ax, 3.2, 3.3, 2.5, 1.0, "receipt-parser\nscope: fs.read", PRIMARY, size=7.5)
box(ax, 6.6, 3.3, 2.5, 1.0, "ledger-agent\nscope: payments", PRIMARY, size=7.5)
box(ax, 10.0, 3.3, 2.3, 1.0, "payments\n.transfer", LIGHT, color=DARK, size=7.5)
box(ax, 6.6, 0.5, 2.5, 1.0, "notifier\nscope: board", PRIMARY, size=7.5)
box(ax, 10.0, 0.5, 2.3, 1.0, "board.write", LIGHT, color=DARK, size=7.5)
arrow(ax, (2.3, 3.8), (3.2, 3.8)); ax.text(2.75, 4.45, "read", ha="center", fontsize=6.5)
arrow(ax, (5.7, 3.8), (6.6, 3.8)); ax.text(6.15, 4.45, "message", ha="center", fontsize=6.5)
arrow(ax, (9.1, 3.8), (10.0, 3.8), ls="--"); ax.text(9.55, 4.45, "DROP (taint)", ha="center", fontsize=6.5, weight="bold")
arrow(ax, (4.45, 3.3), (6.6, 1.0), rad=0.2); ax.text(5.55, 2.1, "message", fontsize=6.5)
arrow(ax, (9.1, 1.0), (10.0, 1.0), ls="--"); ax.text(9.55, 1.65, "DROP (revoked)", ha="center", fontsize=6.5, weight="bold")
ax.text(0.1, 2.85, "parser's own payment: DROP (scope)\nparser spawns helper: DROP (widening)", ha="left", va="top", fontsize=6.5)
save(fig, "fig2_taint_flow")

# Figure 3: AI Village would-block decisions by rule (single series)
base = json.loads((DATA / "village_baseline.json").read_text())["baseline"]
rules = [("Network outside allowlist", base["by_rule"]["network.default_deny"]),
         ("Argument constraints", base["by_rule"]["tools.deny_arguments"]),
         ("Write cap per resource", base["by_rule"]["tools.write_cap"]),
         ("Repeated identical intent", base["by_rule"]["semantic.identical_intent"])]
fig, ax = plt.subplots(figsize=(5.0, 1.9))
labels, values = zip(*rules)
y = range(len(rules))[::-1]
ax.barh(list(y), values, color=PRIMARY, height=0.55, edgecolor=DARK, lw=0.6)
ax.set_yticks(list(y)); ax.set_yticklabels(labels)
ax.set_xscale("log"); ax.set_xlim(500, 300000)
ax.set_xlabel("Would-block decisions (log scale)")
for yi, v in zip(y, values):
    ax.text(v * 1.12, yi, f"{v:,}", va="center", fontsize=8, color=DARK)
ax.grid(axis="x", color=LIGHT, lw=0.6); ax.set_axisbelow(True)
save(fig, "fig3_village_rules")

# Figure 4: latency by enforcement path (p50 bar, p95 and p99 markers)
runs = [json.loads((DATA / f"benchv2_{i}.json").read_text()) for i in (1, 2, 3)]
import statistics
paths = [("In-process guard", "in_process_guard"), ("HTTP guard (JWT)", "http_guard_loopback_jwt"),
         ("MCP direct (baseline)", "mcp_direct_stdio_baseline"), ("MCP proxy, refused", "mcp_proxy_refused"),
         ("MCP proxy, allowed", "mcp_proxy_allowed")]
med = lambda k, m: statistics.median(r[k][m] for r in runs)  # noqa: E731
fig, ax = plt.subplots(figsize=(5.0, 2.1))
y = list(range(len(paths)))[::-1]
p50 = [med(k, "p50") for _, k in paths]
hatches = ["", "", "////", "", ""]
for yi, v, h in zip(y, p50, hatches):
    ax.barh(yi, v, color=TEAL if h else PRIMARY, height=0.5, edgecolor=DARK, lw=0.6, hatch=h)
ax.scatter([med(k, "p95") for _, k in paths], y, marker="|", s=90, color=DARK, lw=1.4, label="p95", zorder=3)
ax.scatter([med(k, "p99") for _, k in paths], y, marker="x", s=22, color=DARK, lw=1.0, label="p99", zorder=3)
ax.set_yticks(y); ax.set_yticklabels([p for p, _ in paths])
ax.set_xscale("log"); ax.set_xlim(0.02, 20)
ax.set_xlabel("Latency per call, ms (log scale); bar = median p50 of three runs")
for yi, v in zip(y, p50):
    ax.text(v * 0.9, yi + 0.33, f"{v:.3f}" if v < 1 else f"{v:.2f}", ha="right", fontsize=7, color=DARK)
ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=7, frameon=False)
ax.grid(axis="x", color=LIGHT, lw=0.6); ax.set_axisbelow(True)
save(fig, "fig5_latency")

# Figure 5: taint granularity (single series)
local = json.loads((DATA / "village_agenttaint_local.json").read_text())["agent-taint-local"]
prop = json.loads((DATA / "village_agenttaint.json").read_text())["agent-taint"]
task = json.loads((DATA / "village_task.json").read_text())
rows = [("Span (no laundering\nprotection)", base["contaminated_writes"] / base["writes"]),
        ("Agent", local["contaminated_writes"] / local["writes"]),
        ("Agent + chat\npropagation", prop["contaminated_writes"] / prop["writes"]),
        ("Task (controller-\nopened)", task["task-local"]["contaminated_writes"] / task["task-local"]["writes"]),
        ("Task + chat\npropagation", task["task"]["contaminated_writes"] / task["task"]["writes"])]
fig, ax = plt.subplots(figsize=(5.0, 2.5))
y = list(range(len(rows)))[::-1]
for i, (yi, (_, v), h) in enumerate(zip(y, rows, ["", "\\\\", "xxxx", "", "...."])):
    fill = GOLD if i >= 3 else (PRIMARY if not h else TEAL)
    ax.barh(yi, v * 100, color=fill, height=0.5, edgecolor=DARK, lw=0.6, hatch=h)
    ax.text(v * 100 + 1.5, yi, f"{v:.1%}", va="center", fontsize=8, color=DARK)
ax.set_yticks(y); ax.set_yticklabels([r for r, _ in rows])
ax.set_xlim(0, 105); ax.set_xlabel("Shared-state writes flagged as contaminated (% of 101,332), by unit of taint")
ax.grid(axis="x", color=LIGHT, lw=0.6); ax.set_axisbelow(True)
save(fig, "fig4_taint_granularity")
print("ok", sorted(p.name for p in OUT.glob("*.jpg")))
