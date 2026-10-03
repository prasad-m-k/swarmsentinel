"""Live prompt-injection demo in the terminal: agents run real Python tools through the guard.

    cd artifacts/swarm-sentinel/python
    python examples/live_injection_demo.py                                  # in-process gateway
    python examples/live_injection_demo.py --remote http://127.0.0.1:8000   # through the engine API
    python examples/live_injection_demo.py --remote http://127.0.0.1:8000 --pause 1.5   # watch it in the dashboard

The story itself lives in demo.py; the dashboard can also launch it (POST /api/swarm/demo/injection).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo import run_injection  # noqa: E402
from sdk.guard import Guard  # noqa: E402


def show(call, decision):
    mark = "ALLOW" if decision.allowed else "BLOCK"
    taint = f"  tainted by {decision.taintOrigin}" if decision.taintOrigin else ""
    print(f"  [{mark}] {call['agentId']:<15} {call['action']:<7} {call['target']:<24} {decision.rule}"
          f"  ({decision.evaluationMs:.2f} ms){taint}")
    for alert in decision.alerts:
        print(f"  [TRIPWIRE] {alert['kind']}: {alert['reason']} -> revoked {', '.join(alert['agents'])}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", help="engine base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--no-feedback", action="store_true", help="disable tripwire revocation")
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between steps")
    args = parser.parse_args()
    options = dict(policy="mock", feedback=not args.no_feedback, on_decision=show)
    guard = Guard.remote(args.remote, **options) if args.remote else Guard.local(**options)
    print(f"SwarmSentinel live session {guard.session_id} ({'remote ' + args.remote if args.remote else 'in-process'})")

    tools = run_injection(guard, pause=args.pause, narrate=lambda text: print(f"\n{text}" if text[0].isdigit() else text))

    print(f"\nMoney moved: {tools.money_moved or 'none'}")
    print(f"Board posts: {tools.board}")
    if args.remote:
        print(f"Session record: {args.remote}/api/swarm/sessions/{guard.session_id}")
    if tools.money_moved:
        raise SystemExit("FAILED: a transfer executed")


if __name__ == "__main__":
    main()
