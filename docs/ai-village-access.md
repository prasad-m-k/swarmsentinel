# AI Village dataset access and handling

SwarmSentinel replays real AI Village records locally. The dataset (`aidigestorg/ai-village` on Hugging Face) is gated, and access is reviewed manually. Nothing from it is committed to this repository or served by the public deployment.

## Terms that apply

Access is granted for research and analysis. By requesting access you agree to:

1. Not train or fine-tune AI systems on the data without written permission from AI Digest.
2. Not attempt to re-identify any individual.
3. Cite AI Digest / AI Village in resulting work: `AI Digest, "AI Village dataset", 2026. https://theaidigest.org/village`
4. Tell the maintainers about publications so they can cross-reference them.

The dataset README warns that secret redaction is best-effort. If a credential turns up, report it to the maintainers and do not use it.

## Getting the files

1. Request access at https://huggingface.co/datasets/aidigestorg/ai-village while signed in. Wait for approval.
2. Download into `download-hugginface/` at the repository root. That folder is in `.gitignore`.

   SwarmSentinel reads these tables:

   | File | Size (2026-09-20 export) | Used for |
   | --- | --- | --- |
   | `agents.jsonl.gz` | < 1 MB | Admitted identities (gateway registry) |
   | `chat_rooms.jsonl.gz` | < 1 MB | Room names |
   | `computer_use_sessions.jsonl.gz` | 40 MB | Session to agent mapping |
   | `chat_messages.jsonl.gz` | 53 MB | Messages and agent-to-agent mentions |
   | `claude_code_messages.jsonl.gz` | 104 MB | Claude Code tool calls |
   | `computer_use_turns.jsonl.gz` | 2.5 GB | Shell commands and other tool calls |
   | `manifest.json` | < 1 KB | Export timestamp (provenance) |

   `events`, `agent_memories`, `summaries`, the transcript and the screenshot tars are not needed. You can also download through the browser. Hugging Face's dataset viewer is useful for inspecting the large tables without downloading them.
3. Read `SCHEMA.md` and `CHANGELOG.md` from the dataset before interpreting results.

## What the ingest stores

`python -m village.ingest` writes `data/ai-village/village.sqlite`, which is also gitignored. It holds normalized events only:

- Agent names (public model names such as `Claude Opus 4.5`), timestamps, tool names, network hostnames and shared-resource keys.
- Chat text and shell commands, truncated and scrubbed again for emails, phone numbers and token-shaped strings, on top of the dataset's own redaction.
- Human chat participants are collapsed into a single `human` actor. Their message text and user IDs are never stored.
- Every event keeps a `sourceRef` (`table:row-id`) so a finding can be traced back to the original record.

GUI micro-actions (clicks, scrolls, screenshots, key presses) are counted but not stored.

## Interpreting replays

- Replays run in **report-only** mode. The actions already happened; drop and throttle decisions say what the policy would have done. They are not evidence that ASP would have prevented an outcome.
- Agent narration (chat text, bash comments) is the agent's claim. Check it against screenshots before treating it as fact.
- The 2026-03-24 perma-computer-use change and the 2026-02-10 auto-nudger change the shape of the data. Compare periods with the changelog open.
- Heuristic alerts flag coordination worth reviewing. Collaboration the village was asked to do will trip them too.

## Structure-only snapshots

`python -m village.snapshot` writes a reduced copy for private hosting: top-ranked episodes only, intent text replaced by keyed word hashes with a discarded random key, shell commands reduced to the fragment an argument rule matched, fingerprints emptied. Replay results are unchanged. It is less revealing than the full store but still derived data under the same terms: private storage only, approved readers only (`SWARM_VILLAGE_READERS`), never published, and no wider audience without AI Digest's written permission.

## Sharing results

Share aggregate findings, policy files and code. Do not publish raw rows, the SQLite store, or exported JSON traces from AI Village replays, since those contain dataset text. Cite AI Digest / AI Village and let the maintainers know about any publication.
