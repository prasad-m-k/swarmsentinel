# TAISAP submission: Agent Security Policy

Target: ACM Transactions on AI Security and Privacy (TAISAP), original research paper (recommended up to 35 pages). Manuscript: `ASP-TAISAP-submission.docx`, 35 pages in the ACM single-column submission layout (TAISAP recommends up to 35, so there is no room left for additions without cuts). Title: "Agent Security Policy: Enforcing Integrity and Confidentiality Outside the Model for LLM Agents and Agent Swarms". The paper presents both tools as one system: the tool-path monitor (SwarmSentinel, called ASP-RM in the paper) and the model-path egress monitor (SwarmSheriff, called ASP-EG). `ASP-TAISAP-submission-preview.pdf` is a Word export for checking layout.

## Open items before upload

0. **Merge both feature branches.** SwarmSentinel `feature/task-scoped-taint` (task-scoped taint, MCP task rotation, confidentiality labels, egress endpoints; 230 tests pass) and SwarmSheriff `feature/sentinel-integration` (engine identity and labels, fail-closed resolution, recorder sink; 113 test cases pass). Both are uncommitted. The anonymized artifact must contain both tools, `deploy/confinement/` and `analysis/e2e_egress.py`, with project names and author traces removed.
1. **Artifact link.** The Artifact Availability statement contains `[ANONYMIZED-ARTIFACT-URL]`. Create an anonymized mirror of the code (for example with anonymous.4open.science) without author names, commit history, `CLAUDE.md`, decks or the README's live-demo links, and replace the placeholder.
2. **AI-use disclosure.** ACM's Policy on Authorship (updated 14 May 2026) requires AI tools used in the conduct of the research, including writing the code that produces the results, to be described in the methods section. Writing assistance needs no disclosure. Add a short paragraph to Section 6.1 if it applies.
3. **History line.** "Received Month Year; revised Month Year; accepted Month Year" after the references is the template placeholder. Leave it for ACM or remove it, as the submission system instructs.
4. **Fonts.** The styles name Linux Libertine O and Linux Biolinum O (the ACM template fonts). They are not installed on this Mac, so the preview PDF uses substitutes. Install them, or upload through ACM's Word template, for a faithful page count.
5. **ORCID.** Required for the submitting author in ScholarOne.
6. **Notify AI Digest.** The AI Village terms ask researchers to tell the maintainers about publications that use the dataset.

## Double-anonymous choices already made

- No author name, affiliation, ORCID, funding or acknowledgments in the manuscript. File metadata author is empty, and file names do not identify the author.
- The implementation is called ASP-RM in the paper, not by its public project name, which would identify the author through its GitHub page. Restore the name and link after acceptance.
- The SSRN preprint is not cited. Preprints are allowed by ACM policy and do not count as prior publication, but citing the same paper's preprint would reveal authorship. The title differs from the preprint's.
- No live-demo URL, hackathon reference or deck reference.

## Where the numbers come from

All runs on an Apple M2, 8 GB, macOS 26.6.2, Python 3.13.5, on 11 October 2026. Scripts are in `analysis/`; aggregate outputs are in `results/` (counts only, no dataset text).

| Paper element | Script | Output |
|---|---|---|
| Table 3, Table 9 | `analysis/synthetic.py` | `results/synthetic.json` |
| Table 4 (layer ablation) | `analysis/ablation.py` | `results/ablation.json` |
| Tables 5 and 6, Figure 3 | `analysis/village_analysis.py --variants baseline` | `results/village_baseline.json` |
| Table 7 | `analysis/village_analysis.py` (sweep variants) | `results/village_sweeps.json` |
| Figure 4, Section 6.5 | `analysis/village_analysis.py --variants agent-taint,agent-taint-local,task-local,task` | `results/village_agenttaint*.json`, `results/village_task.json` |
| Table 8, Figure 5 | `analysis/bench_all.py` (three runs, final code) | `results/benchv2_1..3.json` |
| Table 9 (both monitors end to end) | `analysis/e2e_egress.py` (engine + SwarmSheriff binary) | `results/e2e_egress.json` |
| Table 10 (model-request labels on AI Village) | `analysis/village_analysis.py --variants conf-agent,conf-task,conf-agent-prop,conf-task-prop` | `results/village_conf.json` |
| Table 11 (bypass probes) | `deploy/confinement/` (Docker compose, `bypass_probe.py`) | `results/confinement-*.json` |
| Figures | `analysis/figures.py` | `figures/` |
| Manuscript | `analysis/build.js` with `content.js` and `math.js` | the .docx |

Run the Python scripts from `artifacts/swarm-sentinel/python` with `PYTHONPATH=$PWD`. The village scripts need the local store built by `village.ingest`; they only read it.

## Findings that differ from earlier materials

- **Latency.** The README and both decks quote p50 0.024 ms in-process and 0.39 ms over HTTP. Those numbers predate authentication. Measured on the final code: 0.047 ms in-process, 2.24 ms over loopback HTTP with RS256 token verification per request, and 4.06 ms through the MCP proxy (1.06 ms for a direct MCP call).
- **Threat model gap G2** has been corrected on the feature branch with the measured numbers (span-level propagation adds nothing; agent-level with propagation flags 92.4% of writes; task-level with propagation 53.2%).
- **Threat model gap G10** contradicted T5 on result inspection; corrected on the feature branch.

## Response to the feedback of 11 October 2026

| Point | Change |
|---|---|
| Confinement is external | Section 5.3 now specifies a container profile; new RQ6 (Section 6.7, Table 9) tests it: 16 of 16 bypass probes closed when confined, 13 of 16 with ordinary network access. Limitations state what the probe does not show. |
| Taint saturation | Task-scoped taint implemented (engine, MCP rotation, 13 tests) and evaluated: 43.8% of writes flagged with laundering refused, 53.2% with chat propagation, against 79.1% and 92.4% for agent-level. Proposition 2, Section 4.4, RQ4, Figure 4 and Discussion 7.2 updated, including the controller's declassification responsibility. |
| Report-only phase | Section 7.3 now prescribes report-only as a required phase with a concrete tuning procedure. |
| Evaluation and detector limits | Limitations now state that delegation, depth, fan-out and taint inheritance were exercised only synthetically and in unit tests, and that detector miss rates on adversarial inputs were not measured. |

## Joint-system revision (11 October 2026)

- Threat model gains A4 (data leaving through model requests) and goal G6. Section 2.5 motivates provenance labels over pattern matching on outbound text.
- Section 4.5 adds confidentiality labels (Bell-LaPadula high-water mark, same unit as taint) and Proposition 3. Section 5.3 describes the egress monitor and its engine link.
- RQ6 (Section 6.7): end-to-end run of both monitors (10 of 10 steps as designed) and the AI Village model-request counterfactual (2.1% of requests labelled with task scope, 9.8% per agent). Credential resolution costs 1.33 ms per model request at p50; the egress decision itself 29 µs.
- Limitations: the egress monitor's detectors and classifier are only smoke-tested (20 samples) and no result depends on them; it listens on loopback only, so a containerized agent's model route was not tested in the confinement lab.

## Presentation revision (11 October 2026)

- Figure 1 redrawn: untrusted agent host, trusted engine and monitors, and outside services as separate zones; solid tool path and dashed model-API path; the identity, task and label registry; the agent credential; the operator who opens tasks.
- Table 12 (start of Section 8) separates assumptions, invariants (Propositions 1 to 3) and tested behaviours, each with its basis and scope; Sections 3.3 and 4.3 point to it.
- Tables 5 to 7 and 10 now state each denominator and unit (events, tool actions, shared-state writes, model requests, agent-days, tasks, days).
- Listing 2 shows the AI Village policy with the network allowlist and argument patterns elided (marked `...`); the complete file is `policies/ai-village.asp.json`. `policies/` holds every policy used for a result, including the variants and the policies the scripts build in code (see `policies/README.md`).

## Pre-submission fixes (11 October 2026, second round)

- Artifact: `asp-artifact.zip` (1.3 MB, 200 files) is the anonymized supplementary file named in Artifact Availability. Tools renamed ASP-RM and ASP-EG throughout, including the Go module and the `X-Asp-Eg-*` headers; no author, repository, preprint or internal-document references. Verified inside the archive: 230 Python tests pass (5 skipped), Go vet and tests pass, end-to-end 10 of 10. Upload it as a supplementary file, not a public link.
- Production metadata removed: no ACM Reference Format block, DOI, volume or article number, and no Received/revised/accepted line.
- Denominators: 1,233,716 replay events = 1,223,724 by the 46 agents + 9,992 out-of-band chat messages from the anonymous human and scaffold actor (checked against the store). Stated in Section 6.3 and the Table 5 and 10 captions.
- The 2.1% vs 9.8% result is qualified as label selectivity, not measured leakage, wherever it appears, with the risk of a clean task opened while a secret persists in memory, files or messages.
- Table 13 compares eight prior systems with ASP on seven mechanisms; the introduction states the contribution is the integration around one engine-issued identity. Appendix tables are now 14 and 15.
- Page count back to 35: Listing 2 abbreviated and compacted (all 36 settings kept), table cell padding 1 pt, Table 13 uses Y/P/n.d. with a key.
- Use of Generative AI statement added (prototype development and figure-generation script). Dataset described as private with access on request, citing AI Digest 2026 and the AI Swarm Dynamics Hackathon page (swarmchasing.com).
