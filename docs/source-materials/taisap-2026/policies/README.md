# Policies used for the results

| File | Used for |
|---|---|
| `mock.asp.json` | Synthetic scenarios and layer ablation (Tables 3, 4, 13; Listing 1 abridges it) |
| `ai-village.asp.json` | AI Village replay baseline (Tables 5, 6; Figure 3; Listing 2 reproduces it verbatim) |
| `ai-village.write-cap-*`, `.repeated-intent-*`, `.consensus-density-*` | Threshold sensitivity (Table 7) |
| `ai-village.task-local`, `.task`, `.propagate` | Taint granularity (Figure 4). The agent-level rows use `ai-village.propagate` (with propagation) or the baseline (without), replayed by `AgentTaintGateway` in `analysis/village_analysis.py`, which pins taint to the agent; that is a replay variant, not a policy field |
| `ai-village.conf-*` | Model-request labels (Table 10) |
| `workspace-sample.asp.json` | Sample MCP workspace policy from the test suite |
| `latency-benchmark.asp.json` | MCP proxy latency (Table 8): sample policy without step budget or write cap |
| `confinement-lab.asp.json` | Confinement lab (Table 11): sample policy with task-scoped taint |
| `e2e-engine.asp.json`, `e2e-egress-monitor.policy.json` | End-to-end run of both monitors (Table 9) |

The sample-derived files contain the placeholder `{tests}`, which the scripts replace with the test directory path. Every `.asp.json` file validates against the engine's policy schema.
