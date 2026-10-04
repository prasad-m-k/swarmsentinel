"""Run real model-backed agents, then inspect their session in the dashboard's Live tab."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.runner import DEFAULT_MODEL, model_client, run_showcase
from sdk.guard import Guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["normal", "adversarial"], default="normal")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--remote", default="http://localhost",
                        help="Engine origin through Replit's shared proxy; defaults to http://localhost")
    parser.add_argument("--token-file", type=Path, required=True,
                        help="Private file containing the owner's Clerk session JWT; refreshed externally before expiry")
    args = parser.parse_args()
    def token_provider():
        token = args.token_file.read_text().strip()
        if not token:
            raise ValueError("Clerk token file is empty")
        return token

    token_provider()  # Fail before any model inference; never print the token.
    client = model_client()  # Fail before creating a session if provider configuration is missing.
    decisions = []

    def record(call, decision):
        decisions.append({"agent": call["agentId"], "target": call["target"],
                          "allowed": decision.allowed, "rule": decision.rule})
        print(f"[{'ALLOW' if decision.allowed else 'BLOCK'}] {call['agentId']}: "
              f"{call['target']} ({decision.rule})", flush=True)

    guard = Guard.remote(args.remote, token_provider=token_provider, policy="agents", on_decision=record,
                         label=f"Real LLM agents · {args.mode} · {args.model}")
    print(f"Live session: {guard.session_id}", flush=True)
    print(f"Preview path: /?scenario=live&session={guard.session_id}", flush=True)
    print("Or sign in as the token's owner, open Live → My private sessions, then Watch.", flush=True)
    directory = Path(__file__).resolve().parents[4] / "data" / "agent-demos" / guard.session_id
    directory.mkdir(parents=True, exist_ok=False)  # Controller evidence only, not a tool sandbox.
    try:
        result = run_showcase(guard, directory, client, args.model, args.mode == "adversarial")
        result["gatewayDecisions"] = decisions
        (directory / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)
    except Exception as e:
        (directory / "failure.json").write_text(json.dumps(
            {"type": type(e).__name__,
             "message": "Provider, gateway, or iteration failure. Inspect the model transcript for completed steps. No fallback.",
             "gatewayDecisions": decisions}, indent=2))
        print(f"Real-agent run FAILED ({type(e).__name__}). "
              f"Inspect {directory / 'failure.json'}; no scripted fallback was used.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"Cannot launch real agents ({type(e).__name__}). "
              "Check the OpenAI integration and running engine. No scripted fallback was used.", file=sys.stderr)
        raise SystemExit(1)