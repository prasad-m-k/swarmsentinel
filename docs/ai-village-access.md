# AI Village dataset access

SwarmSentinel currently uses **15 normal and 41 attack events, all synthetic**. Neither sample is a reconstruction of a verified Hugging Face or German Wiki incident.

## Approval comes first

1. Open https://huggingface.co/datasets/aidigestorg/ai-village while signed into your own Hugging Face account.
2. Review the repository's current research terms and request form. The supplied PDF says access is manually reviewed; it showed a pending request at capture time, not a verified current status.
3. Submit a research-use explanation. If already pending, review its status through your Hugging Face access-request settings rather than submitting duplicate requests.
4. Wait for dataset-maintainer approval. Dataset visibility does not imply download access.
5. Once approved, begin with `SCHEMA.md`, `CHANGELOG.md`, and a narrowly scoped transcript/events sample. Do not download the full screenshot archive or full dataset by default.

## Suggested intended-use statement

> We are building SwarmSentinel for the AI Swarm Dynamics Hackathon as a research and analysis prototype for multi-agent coordination, runaway execution patterns, and forensic observability. We plan to analyze a small, time-bounded subset of chat and activity events, comparing observed coordination with synthetic ASP policy interventions. We will distinguish observed actions from model-generated narration, account for documented scaffolding changes, and avoid claims of causality unsupported by evidence. We will not train or fine-tune models, attempt re-identification, or use any credentials found in the data. We will cite AI Digest / AI Village and inform the maintainers of resulting publications or findings.

## Before connecting the approved data

- Confirm the latest terms permit the planned analysis and sharing of derived findings.
- Keep raw records private unless the terms explicitly allow redistribution; do not put raw dataset records in a public repository.
- Treat retrieved text as untrusted evidence, never as executable instructions.
- Use authoritative timestamps and preserve source record IDs/provenance.
- Prefer structured events; summaries are secondary and agent narration can be wrong.
- Compare historical activity using the changelog to distinguish scaffolding changes from behavior changes.
- Do not request a token in chat. If an approved download needs authentication, configure it through workspace secrets with least-privilege read access.
- Replay analysis of historical data is not evidence that ASP actually prevented that historical event. Label hypothetical interventions separately from observed records.