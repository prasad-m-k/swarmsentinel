"""Machine-readable ASP policy declarations (paper section IV.D) plus SwarmSentinel's swarm extension."""
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

POLICY_DIR = Path(__file__).parent / "policies"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Network(_Strict):
    default: Literal["deny", "allow"] = "deny"
    allow: list[str] = []


class ArgumentRule(_Strict):
    tool: str
    pattern: str
    label: str


class Tools(_Strict):
    default: Literal["deny", "allow"] = "deny"
    allow: list[str] = []
    deny: list[str] = []
    deny_arguments: list[ArgumentRule] = Field(default=[], alias="deny-arguments")


class ContentTrust(_Strict):
    system_prompt: str = Field(default="high", alias="system-prompt")
    user_input: str = Field(default="medium", alias="user-input")
    tool_response: str = Field(default="low", alias="tool-response")
    retrieved_document: str = Field(default="untrusted", alias="retrieved-document")
    contaminated_write: Literal["report", "deny"] = Field(default="report", alias="contaminated-write")
    # Paper section III.B: contamination reduces capability. These tools are refused outright once tainted.
    contaminated_deny: list[str] = Field(default=[], alias="contaminated-deny")
    # Biba no-read-down across agents: receiving a message from a tainted context taints the recipient.
    propagate: bool = Field(default=False, alias="propagate-via-messages")


class Delegation(_Strict):
    sub_agents: str = Field(default="inherit-and-restrict", alias="sub-agents")
    max_depth: int = Field(default=4, alias="max-depth", ge=1, le=10)


class Reporting(_Strict):
    violations: str


class RepeatedIntent(_Strict):
    limit: int = Field(default=5, ge=2)
    min_agents: int = Field(default=3, alias="min-agents", ge=1)
    actions: list[Literal["tool", "message"]] = ["tool"]


class Tripwires(_Strict):
    fanout_children: int = Field(default=4, alias="fanout-children")
    shared_state_agents: int = Field(default=4, alias="shared-state-agents")
    consensus_min_cluster: int = Field(default=3, alias="consensus-min-cluster")
    consensus_density: float = Field(default=.5, alias="consensus-density")
    echo_agents: int = Field(default=0, alias="echo-agents", description="0 disables the echo detector")
    echo_similarity: float = Field(default=.6, alias="echo-similarity")
    taint_agents: int = Field(default=0, alias="taint-agents", description="0 disables the injection-spread detector")
    delegation_loop: bool = Field(default=False, alias="delegation-loop")


class Swarm(_Strict):
    window_seconds: int = Field(default=10, alias="window-seconds", ge=1)
    repeated_intent: RepeatedIntent = Field(default_factory=RepeatedIntent, alias="repeated-intent")
    write_cap: int = Field(default=6, alias="write-cap", ge=1)
    step_budget: int = Field(default=0, alias="step-budget", ge=0, description="max actions per agent per window; 0 disables")
    tripwires: Tripwires = Field(default_factory=Tripwires)


class ASPPolicy(_Strict):
    asp_version: Literal["1.0"] = Field(alias="asp-version")
    description: str = ""
    network: Network
    tools: Tools
    content_trust: ContentTrust = Field(alias="content-trust")
    delegation: Delegation
    reporting: Reporting
    swarm: Swarm = Field(default_factory=Swarm)

    @classmethod
    def load(cls, name):
        return cls.model_validate(json.loads((POLICY_DIR / f"{name}.asp.json").read_text()))

    def with_overrides(self, max_depth=None, semantic_limit=None, write_limit=None):
        policy = self.model_copy(deep=True)
        if max_depth is not None:
            policy.delegation.max_depth = max_depth
        if semantic_limit is not None:
            policy.swarm.repeated_intent.limit = semantic_limit
        if write_limit is not None:
            policy.swarm.write_cap = write_limit
        return policy

    def declaration(self):
        return self.model_dump(by_alias=True)


def tool_matches(tool, patterns):
    """Glob match; a pattern may hold alternatives separated by '|'."""
    return any(fnmatchcase(tool, alt) for p in patterns for alt in p.split("|"))


def host_allowed(host, network):
    if not host:
        return True
    if any(fnmatchcase(host, p) for p in network.allow):
        return True
    return network.default == "allow"


def argument_violation(tool, detail, rules):
    for rule in rules:
        if tool_matches(tool, [rule.tool]) and re.search(rule.pattern, detail):
            return rule.label
    return None
