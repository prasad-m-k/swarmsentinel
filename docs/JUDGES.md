# For judges: replay real AI Village data on your own machine

The AI Village dataset is access-controlled, so this repository never ships it. With your own approved access you can build the same local SQLite store we used (about 15 minutes of machine time) and see the real-data results yourself. Everything else, including the synthetic scenarios, the live attack demo and the threat model, needs no dataset at all.

## 0. Before you start

- **Dataset access:** request it at https://huggingface.co/datasets/aidigestorg/ai-village while signed in to Hugging Face. Access is reviewed manually, so request early. The terms: research use only, no training on the data, no re-identification, cite AI Digest.
- **Tools:** git, Python 3.13 or newer, and [uv](https://docs.astral.sh/uv/).
- **Disk:** about 4 GB for the downloads and 1 GB for the store.

## 1. Get the code and Python dependencies

```bash
git clone https://github.com/prasad-m-k/swarmsentinel.git
cd swarmsentinel
uv sync --frozen
source .venv/bin/activate
```

## 2. Download the seven tables the ingest reads

```bash
uvx --from huggingface_hub hf auth login          # paste a read token; it stays in ~/.cache/huggingface
uvx --from huggingface_hub hf download aidigestorg/ai-village --repo-type dataset \
  --local-dir download-hugginface \
  agents.jsonl.gz chat_rooms.jsonl.gz computer_use_sessions.jsonl.gz \
  chat_messages.jsonl.gz claude_code_messages.jsonl.gz \
  computer_use_turns.jsonl.gz manifest.json
```

`download-hugginface/` is gitignored. The largest file, `computer_use_turns.jsonl.gz`, is about 2.5 GB.

## 3. Build your own SQLite store

```bash
cd artifacts/swarm-sentinel/python
python -m village.ingest --raw ../../../download-hugginface   # about 5 minutes
python -m village.scan                                        # about 3 minutes; ranks episodes
```

This writes `data/ai-village/village.sqlite` (gitignored), with normalized events and the episode index. Text is scrubbed again for emails, phone numbers and token-like strings, and human participants are collapsed into one anonymous actor.

## 4A. See the results from the command line (no sign-in needed)

```bash
python -m village.replay --list                       # ranked episodes
python -m village.replay --episode 2026-05-11-1643    # the episode shown in our deck
```

Each replay prints the alerts and counterfactual would-block counts, and writes the same Markdown report and JSON trace the dashboard exports, to `data/ai-village/replays/`. This is the path we tested end to end.

## 4B. Optional: see it in the dashboard

The dashboard's AI Village tab only appears for signed-in, approved accounts, so it needs a free [Clerk](https://clerk.com) development application, plus Node.js 24 and pnpm 10.

1. Create a Clerk development app, sign up once in it, and copy your **publishable key** and your **user ID** (`user_...`) from the Clerk dashboard.
2. Start the engine (from the repository root):

   ```bash
   export CLERK_PUBLISHABLE_KEY=pk_test_...
   export SWARM_AUTHORIZED_PARTIES=http://localhost:5173
   export SWARM_ENABLE_VILLAGE=true
   export SWARM_VILLAGE_READERS=user_...
   python -m uvicorn server:app --app-dir artifacts/swarm-sentinel/python --host 127.0.0.1 --port 8000
   ```

3. In a second terminal, start the dashboard:

   ```bash
   pnpm install --frozen-lockfile
   VITE_CLERK_PUBLISHABLE_KEY=pk_test_... SWARM_ENGINE_URL=http://127.0.0.1:8000 \
     PORT=5173 BASE_PATH=/ pnpm --filter @workspace/swarm-sentinel run dev
   ```

4. Open http://localhost:5173, sign in, choose **AI Village**, pick an episode and press **Replay**.

On macOS, the committed lockfile strips macOS-native build binaries (it targets Linux). If `vite` fails to start, copy the repository elsewhere, delete the platform `"-"` overrides in `pnpm-workspace.yaml`, and run `pnpm install --no-frozen-lockfile` in that copy.

## How to read the results

- **Counterfactual:** these are historical actions. Every drop or throttle is what the policy *would* have done, not harm that was prevented.
- **Agent narration is a claim:** chat text and command comments are what an agent said, not verified fact.
- **Two regimes:** behaviour before and after the 2026-03-24 scaffolding change reflects different agent setups; see the dataset's CHANGELOG.
- **Please keep your store and exports private,** and cite: AI Digest, "AI Village dataset", 2026. https://theaidigest.org/village
