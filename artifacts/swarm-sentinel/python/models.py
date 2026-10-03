from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: Literal["normal", "attack"]
    feedbackEnabled: bool = True
    maxDepth: int = Field(default=4, ge=1, le=10)
    semanticLimit: int = Field(default=5, ge=2, le=20)
    writeLimit: int = Field(default=6, ge=1, le=30)


class Event(BaseModel):
    id: str
    timestamp: str
    agentId: str
    parentId: str = ""
    spanId: str
    parentSpanId: str = ""
    action: Literal["spawn", "tool", "message", "wiki_edit"]
    channel: Literal["in_band", "out_of_band"]
    target: str
    intent: str
    intentVector: list[float]
    depth: int


class Trace(Event):
    decision: Literal["allow", "throttle", "drop", "observed"]
    rule: str
    reason: str
    executed: bool
    policyVersion: int