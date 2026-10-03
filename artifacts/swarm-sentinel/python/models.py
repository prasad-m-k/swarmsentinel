from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict, model_validator


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: Literal["normal", "attack", "ai-village"]
    feedbackEnabled: bool = True
    # Omitted limits fall back to the scenario's ASP policy declaration.
    maxDepth: Optional[int] = Field(default=None, ge=1, le=10)
    semanticLimit: Optional[int] = Field(default=None, ge=2, le=20)
    writeLimit: Optional[int] = Field(default=None, ge=1, le=60)
    # AI Village replay window: an indexed episode, or an explicit UTC range.
    episodeId: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    maxEvents: int = Field(default=5000, ge=10, le=20000)

    @model_validator(mode="after")
    def _window(self):
        if self.scenario == "ai-village" and not self.episodeId and not (self.start and self.end):
            raise ValueError("ai-village replay needs episodeId or both start and end")
        return self


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
    # Provenance and ASP evaluation inputs. Defaults keep synthetic samples valid.
    source: Literal["synthetic", "ai-village"] = "synthetic"
    sourceRef: str = ""
    detail: str = ""
    network: str = ""
    reads: Literal["", "high", "medium", "low", "untrusted"] = ""
    write: Optional[bool] = None
    resource: str = ""
    mentions: list[str] = []


class Violation(BaseModel):
    directive: str
    attempted_action: str
    detail: str = ""
    enforced: bool


class Trace(Event):
    decision: Literal["allow", "throttle", "drop", "observed"]
    rule: str
    reason: str
    executed: bool
    policyVersion: int
    mode: Literal["enforce", "report-only"] = "enforce"
    violations: list[Violation] = []
    contaminated: bool = False
