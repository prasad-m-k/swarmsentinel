# Content inspection

Pattern-based scanning of tool arguments and tool results, configured per policy. Code: `artifacts/swarm-sentinel/python/content_scan.py` (detectors), `asp_gateway.py` (arguments), `mcp_proxy/broker.py` (results). Tests: `tests/test_content_inspection.py`.

This is heuristic evidence for a policy decision. It is not a classifier and it does not detect every attack: a clean scan does not mean content is safe, and a finding does not prove intent. Threat model gap G4 still describes the main defence against injection, which is limiting what a context can do after it reads untrusted content, not recognising injected text.

## Policy

```json
"inspection": {
  "tools": ["mcp:*"],
  "arguments": {"secrets": "deny", "pii": "report", "injection": "report"},
  "results":   {"secrets": "redact", "pii": "report", "injection": "taint"}
}
```

| Where | Family | Actions |
|---|---|---|
| Arguments (every live path that sends `detail`) | secrets, pii, injection | `off`, `report` (advisory violation), `deny` (rule `inspection.<family>`) |
| Results (MCP proxy) | secrets, pii | `off`, `report`, `redact` (matched spans replaced with `[REDACTED:<detector>]`), `block` (result withheld; the tool did run) |
| Results (MCP proxy) | injection | `off`, `report`, `taint` (the caller's span and agent become contaminated, as after an untrusted read), `block` |

`tools` limits inspection to matching targets. Policies without an `inspection` section behave exactly as before, and their declarations are unchanged. Argument scanning runs inside the argument-constraint stage of Algorithm 1, after explicit `deny-arguments` rules, so the check order is preserved.

## Detectors

| Family | Detectors |
|---|---|
| secrets | private key blocks, AWS access keys, GitHub and GitLab tokens, Slack tokens, `sk-` style API keys, JWTs, bearer tokens, `password=`/`api_key:` style assignments |
| pii | email addresses, phone numbers, US SSNs (invalid ranges excluded), payment card numbers (Luhn-checked) |
| injection | instruction-override phrasing, role reassignment, system-prompt exfiltration requests, concealment from the user, chat-template tokens |

Only the first 200,000 characters are scanned. When redacting, content beyond that limit is withheld rather than passed through unscanned. Structured result content that matched is withheld, because it cannot be redacted reliably in place.

## What is recorded

Violations carry the directive (`content-inspection.<family>`), detector names and counts, and whether the action was enforced. The matched text is never stored, so recorder rows, exports and reports cannot leak what was found. Tests assert this.

## Known limits

- English-centric injection phrases; paraphrases, other languages, encodings and images are missed.
- PII patterns are US-leaning (SSN, NANP phone formats).
- Emails and phone numbers in legitimate business content will trigger `pii`; prefer `report` unless the deployment really must keep them out.
- Redaction changes what the agent sees; a downstream tool may still have acted on the unredacted data.
