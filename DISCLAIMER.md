# Disclaimer

SwarmSentinel was built for the **AI Swarm Dynamics Hackathon** as a research prototype. It was created independently by Prasad MK (Kameswara Prasad Mukkamala), using personal time and personal resources, to explore runtime containment for multi-agent AI systems based on the author's preprint, "Agent Security Policy (ASP): The Missing Trust Boundary Layer in Agentic AI Systems" (doi.org/10.2139/ssrn.6876926). It is not affiliated with, endorsed by, or created on behalf of any employer, client, or organization the author is or has been associated with, nor with the hackathon organizers beyond being a submission.

## No Warranty

This software and its accompanying files are provided "as is," without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. Use it at your own risk.

## No Liability

In no event shall the author be liable for any claim, damages, or other liability, whether in an action of contract, tort, or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software.

## Research Prototype, Not a Security Product

SwarmSentinel demonstrates ideas; it is not a production security control and makes no security guarantees.

- Interception is cooperative for tools wrapped by the guard: an agent that calls a tool directly is not stopped. Only the engine-owned demo tools run behind the gateway.
- The gateway is not a tamper-proof boundary. There is no container, process-user or filesystem isolation.
- Tripwires are heuristics. They flag coordination worth reviewing, not proof of compromise or malicious intent, and they can fire on legitimate collaboration.
- Do not rely on it to protect real systems, credentials, data or money. The demo payment tools move fictional credits only.

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for what is and is not covered.

## AI Village Data

The real-data evaluation uses the AI Village dataset from AI Digest (`aidigestorg/ai-village` on Hugging Face), accessed under its research terms. **No dataset records, databases or replay exports are included in this repository.** To reproduce the results you need your own approved access; see [docs/JUDGES.md](docs/JUDGES.md).

- Results from that data are counterfactual: they describe what a policy would have done on historical actions, not harm that was caused or prevented.
- Agent narration in the data is the agents' own claims, not verified fact.
- Agents are identified by public model names; human participants are anonymized.
- Please cite: AI Digest, "AI Village dataset", 2026. https://theaidigest.org/village

## Third-Party Material

The MIT License in `LICENSE` covers code and documentation written for this project. It does not extend to:

- the AI Village dataset, its documentation, or the page snapshot in `docs/source-materials/ai-village-dataset-details.pdf`, which belong to AI Digest;
- the ASP preprint (`docs/source-materials/agent-security-policy-asp.pdf`), which is distributed under its own terms on SSRN;
- open-source dependencies, which remain under their own licenses;
- third-party model services used by the optional real-agent demo, which are subject to their providers' terms.

## Independent Work

All code, documentation and ideas in this repository reflect the author's own independent analysis and are not derived from, or based on, any proprietary, confidential, or employer-owned material. Any resemblance to internal tools or processes at any organization is coincidental and based solely on publicly available concepts in software engineering and AI security.

## Open Source Use

This project is released under the MIT License (see `LICENSE`). You are free to use, modify, distribute, and build on it, including for commercial purposes, provided the original copyright and license notice are retained. Contributions, forks, and adaptations are welcome.

## No Professional Advice

Nothing in this repository constitutes legal, security, compliance, or professional advice. Apply your own judgment, and an independent security review, before using any of these ideas to protect real systems.
