# Compliance mapping

How SwarmSentinel's controls relate to the OWASP Top 10 for LLM Applications (2025), the OWASP Top 10 for Agentic Applications (2026), the NIST AI Risk Management Framework 1.0, and ISO/IEC 42001:2023 Annex A.

Read this as an informative mapping for a research prototype, not as evidence of compliance or certification. SwarmSentinel implements controls from the ASP proposal in one process; it is not tamper-proof (threat model G6), and none of these frameworks can be satisfied by a tool alone. Organizational processes, impact assessments and governance remain the adopter's job. Statuses describe the prototype as tested on this branch.

Status key: **Implemented** means a control exists with tests. **Partial** means it covers part of the risk, or only under stated conditions. **Not addressed** means outside what the tool does.

## OWASP Top 10 for LLM Applications 2025

| ID | Risk | SwarmSentinel controls | Status |
|---|---|---|---|
| LLM01 | Prompt Injection | Content-trust taint and capability withdrawal (`trust.contaminated_tool`, `trust.contaminated_write`); taint propagation through messages; injection-marker scanning of results that taints the reader; MCP proxy so a hijacked agent cannot bypass the checks | Partial. Limits what an injected context can do; detection is heuristic (G4) |
| LLM02 | Sensitive Information Disclosure | Argument rules for credential stores and secrets; inspection that denies secrets in arguments and redacts or blocks secrets and PII in results; findings never store matched text | Partial. Pattern-based |
| LLM03 | Supply Chain | MCP servers and tools declared in policy; undeclared tools never listed or dispatched; server self-descriptions ignored | Partial. No signing, provenance or AIBOM |
| LLM04 | Data and Model Poisoning | None | Not addressed |
| LLM05 | Improper Output Handling | Model output reaches tools only through gateway checks (allowlist, argument rules, network scope); result inspection before content returns to the agent | Partial |
| LLM06 | Excessive Agency | Default-deny allowlist, explicit deny list, inherit-and-restrict scopes, depth cap, write caps, step budgets, Sentinel revocation | Implemented (prototype) |
| LLM07 | System Prompt Leakage | Prompt-exfiltration phrasing detector (report, taint or block) | Partial. Heuristic only |
| LLM08 | Vector and Embedding Weaknesses | None | Not addressed |
| LLM09 | Misinformation | None; agent narration is treated as a claim, never as evidence | Not addressed |
| LLM10 | Unbounded Consumption | Step budget, write cap, repeated-intent throttle, fanout and delegation-loop tripwires | Partial. Counts actions, not tokens or cost |

## OWASP Top 10 for Agentic Applications 2026

Identifiers and titles as published by the OWASP GenAI Security Project in December 2025, taken from secondary summaries; check wording against the official document before citing.

| ID | Risk | SwarmSentinel controls | Status |
|---|---|---|---|
| ASI01 | Agent Goal Hijack | Content-trust taint, capability withdrawal after untrusted reads, injection taint from results | Partial |
| ASI02 | Tool Misuse and Exploitation | Allowlist, argument constraints, network scope, write caps, engine-owned dispatch in MCP proxy mode | Implemented for proxied MCP tools; cooperative elsewhere (G1) |
| ASI03 | Identity and Privilege Abuse | Engine-issued MCP agent credentials, lineage verification, scoped delegation, tenant and role checks | Partial. Credentials last as long as their session |
| ASI04 | Agentic Supply Chain Vulnerabilities | Policy-declared MCP servers and semantics; undeclared tools hidden | Partial. No signing or provenance of servers |
| ASI05 | Unexpected Code Execution | Shell tools deniable by policy; engine-owned sandbox tools | Partial. No operating-system sandbox (G6) |
| ASI06 | Memory and Context Poisoning | Span and agent taint; inherited by descendants | Partial. Shared-state propagation not tracked (G5) |
| ASI07 | Insecure Inter-Agent Communication | Message taint propagation, lineage checks | Partial. No signed messages; proxy messaging planned |
| ASI08 | Cascading Failures | Sentinel tripwires (fanout, shared-state loop, rapid consensus, echo cascade, injection spread, delegation loop) with revocation of descendants | Implemented (prototype) |
| ASI09 | Human-Agent Trust Exploitation | Concealment-phrasing detector only | Not addressed. No human confirmation flows |
| ASI10 | Rogue Agents | Behavioral tripwires and revocation; flight recorder | Partial |

## NIST AI RMF 1.0 (NIST AI 100-1)

Subcategory wording checked against NIST AI 100-1, Tables 1 to 4.

| Subcategory | Outcome (abridged) | How SwarmSentinel supports it | Status |
|---|---|---|---|
| GOVERN 1.4 | Risk management established through transparent policies and controls | Machine-readable ASP policy files, validated and versioned in the repository | Partial |
| GOVERN 1.6 | Mechanisms to inventory AI systems | Agent registry with lineage; MCP server and tool declarations | Partial. Inventories agents and tools, not AI systems organization-wide |
| GOVERN 4.3 | AI testing, identification of incidents, information sharing | Test suites per control; Sentinel alerts; reports | Partial |
| MAP 4.1 / 4.2 | Risks of components, including third-party software, mapped; internal risk controls documented | Threat model with status column; per-tool semantics for third-party MCP servers | Partial |
| MEASURE 2.4 | Functionality and behavior monitored in production | Live sessions, streaming recorder, engine-observed outcomes | Implemented (prototype) |
| MEASURE 2.7 | Security and resilience evaluated and documented | Threat model, gateway and proxy test suites | Partial |
| MEASURE 2.8 | Transparency and accountability risks examined | Recorder separates permission, engine observation and caller reports | Partial |
| MEASURE 2.10 | Privacy risk examined | PII inspection; dataset handling rules; human participants anonymized | Partial |
| MEASURE 3.1 | Existing and emergent risks tracked | Sentinel swarm-level tripwires | Partial |
| MANAGE 2.3 | Respond to and recover from previously unknown risk | Tripwire triggers revocation with feedback enabled | Partial |
| MANAGE 2.4 | Mechanisms to supersede, disengage or deactivate AI systems | Revocation of agents and descendants; capability withdrawal | Implemented (prototype) |
| MANAGE 3.1 | Third-party risks monitored and controls applied | Proxy enforcement in front of third-party MCP servers | Partial |
| MANAGE 4.1 | Post-deployment monitoring, including override and incident response | Live monitoring and revocation | Partial. No incident workflow |
| MANAGE 4.3 | Incidents and errors communicated and tracked | Alerts and reports in the recorder | Partial. No external communication |

## ISO/IEC 42001:2023 Annex A

Control identifiers checked against two independent summaries of Annex A; titles abridged. The standard's text is normative.

| Control | Topic | How SwarmSentinel supports it | Status |
|---|---|---|---|
| A.2.2 | AI policy | ASP policies are technical enforcement policy, an input to, not a substitute for, an organizational AI policy | Partial |
| A.3.2 | AI roles and responsibilities | Viewer, operator and admin roles for operating the engine | Partial |
| A.4.2 / A.4.4 | Resource and tooling documentation | MCP server and tool declarations document tooling and its semantics | Partial |
| A.5.2 to A.5.5 | AI system impact assessment | None | Not addressed |
| A.6.2.4 | Verification and validation | Automated tests per control, synthetic regression pinning v1 decisions | Partial |
| A.6.2.6 | Operation and monitoring | Live monitoring, Sentinel tripwires, revocation | Partial |
| A.6.2.7 | Technical documentation | Design documents, threat model, policy declarations | Partial |
| A.6.2.8 | Recording of event logs | Flight recorder with provenance; durable journal (`SWARM_STATE_DB`) | Implemented (prototype) |
| A.7.5 | Data provenance | Event `source` and `sourceRef`; execution provenance on each row | Partial |
| A.8.4 | Communication of incidents | Alerts within the engine | Partial |
| A.9.2 / A.9.4 | Responsible and intended use | Default-deny allowlists and scopes restrict agents to declared uses | Partial |
| A.10.3 | Suppliers | Third-party MCP servers governed by policy-declared semantics, not their own claims | Partial |

## Sources

- OWASP GenAI Security Project, Top 10 for LLM Applications 2025: https://genai.owasp.org/
- OWASP GenAI Security Project, Top 10 for Agentic Applications 2026: https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/
- NIST AI 100-1, Artificial Intelligence Risk Management Framework (AI RMF 1.0), January 2023: https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf
- ISO/IEC 42001:2023, Annex A control list as summarized by ISMS.online (https://www.isms.online/iso-42001/annex-a-controls/) and TCSA (https://www.tcsa.in/frameworks/iso-42001/controls)
