# SwarmSentinel — project context

**Project description:** AI Swarm Dynamics Hackathon  
**Status:** Synthetic-sample prototype authorized by the user. Former project name: aivillage.

## Working interpretation

The user subsequently supplied the SwarmSentinel build prompt: integrate an ASP-inspired policy gateway and multi-agent forensic dashboard. Build with a small synthetic sample set first, then establish access to AI Village. The prototype uses Python FastAPI and an interactive web dashboard, as permitted by the prompt.

## Source: Agent Security Policy paper

The paper argues that prompt-level protections are insufficient on their own because the model processes system instructions, user input, tool responses, and retrieved content in the same context. It proposes moving policy enforcement outside the model's control.

Key ideas in the proposal:

- Intercept every tool call in an independent gateway before the underlying tool executes.
- Express policy in a machine-readable format with default-deny behavior, tool and network scopes, and parameter constraints.
- Track the trust level of content; reading untrusted material can reduce the agent's permitted capabilities.
- Delegate only a restricted subset of the orchestrator's permissions to sub-agents.
- Emit structured violation events and support report-only mode before enforcement.

**Important distinction:** ASP is presented as a design principle/proposal, not an established standard or an existing implementation. The paper's security claims should not be attributed to a future aivillage app unless equivalent enforcement is actually built and verified.

## Source: AI Village dataset details

The supplied Hugging Face page snapshot describes `aidigestorg/ai-village`, an ongoing record of agents living and working in a shared virtual environment. It includes chat, a structured event timeline, computer-use activity and screenshot references, agent memories and goals, and generated summaries. The source describes the dataset as useful for studying agentic behavior, multi-agent dynamics, long-horizon memory, human-agent interaction, and evaluations.

Approximate inventory in the supplied snapshot:

- About 233,000 structured events and 123,000 chat messages.
- About 37,000 computer-use sessions and 1.14 million computer-use turns.
- About 165,000 agent memories, 800 generated summaries, and metadata for 31 agents.
- Screenshots are stored separately in per-day tar archives; timestamps are authoritative and screenshot days use Pacific Time.
- The dataset changelog documents changes to prompts, tools, models, memory, and agent roster.

Analysis cautions:

- Agent narration can be inaccurate. Treat it as a claim; validate against structured events and screenshots where available.
- Generated summaries are secondary because they were created without seeing the computer-use sessions.
- Read the dataset changelog before drawing conclusions about behavior changes over time; changes may result from scaffolding rather than agent behavior.
- The snapshot says the dataset is access-controlled with manual review. It shows an access request awaiting review, but this is only the status in the supplied snapshot; check the current status before relying on access.
- The stated terms allow research and analysis, prohibit training or fine-tuning AI systems without written permission, prohibit re-identification, require citation to AI Digest / AI Village, and ask researchers to share resulting publications. Do not retrieve or use the dataset outside those terms.
- The source warns that secrets may remain despite best-effort redaction. Never use or expose credentials if encountered; report them to the dataset maintainers.

Suggested citation from the supplied page: AI Digest, “AI Village dataset,” 2026, https://theaidigest.org/village.

## Questions to settle before implementation

- Is aivillage meant to analyze observed agent trajectories, simulate a new swarm, evaluate agent policies, or combine these?
- Is the AI Village dataset available for the intended use, and what subset can be accessed?
- Should ASP-style security be a core capability or only a research lens?
- What is the hackathon's target user, demo outcome, and time constraint?

## Supplied source files

- [`source-materials/ai-village-dataset-details.pdf`](source-materials/ai-village-dataset-details.pdf)
- [`source-materials/agent-security-policy-asp.pdf`](source-materials/agent-security-policy-asp.pdf)