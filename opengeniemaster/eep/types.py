"""Embodied Execution Protocol (EEP) data model — open re-implementation.

GenieMaster's project page describes EEP as an MCP-inspired protocol between a
coding agent and robots, built from four contracts:

* **Capability** – what the system can do (domain workflows and composed
  tools; robot skills bound to an instance, its calibration and an execution
  service).
* **Guidance** – what the policy should use (detailed language, spatial cues
  such as boxes/points, and object reference images).
* **Feedback** – what physically happened. Published fields:
  ``observation``, ``action_result``, ``failure_reason``.
* **Continuity** – what must remain true when the next task arrives.
  Published fields: ``task_id``, ``completed_steps``, ``assigned_robot``,
  ``pending_work`` (plus the held object and outstanding dependencies).

The published field names are kept verbatim. Envelopes, enums, defaults and
error codes are this project's own design (``PROTOCOL_VERSION``); they are
not an official AgiBot wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .._serde import from_dict, to_dict

PROTOCOL_VERSION = "open-eep/0.1"


class ExecutionMode(str, Enum):
    """How an execution request reaches the robot (architecture figure:
    Language / Affordance / Action / Primitives)."""

    LANGUAGE = "language"  # language-conditioned policy inference (VLA)
    AFFORDANCE = "affordance"  # grasp/placement targets consumed by a policy or planner
    ACTION = "action"  # direct robot control, e.g. IK targets
    PRIMITIVE = "primitive"  # motion primitives / whole-body behaviours


class Outcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    REJECTED = "rejected"  # refused by shared admission before execution
    ERROR = "error"  # infrastructure problem: service down, timeout, adapter error
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    PENDING = "pending"  # waiting on dependencies
    READY = "ready"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})
DONE_STEP_STATUSES = frozenset({StepStatus.SUCCEEDED, StepStatus.SKIPPED})


class _Model:
    """Mixin giving dataclasses JSON round-tripping."""

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        return from_dict(cls, data)


# ---------------------------------------------------------------------------
# Capability contract
# ---------------------------------------------------------------------------


@dataclass
class SkillSpec(_Model):
    """A robot-level (embodiment) capability bound to an execution service."""

    name: str
    service: str  # "policy" | "control" | "primitive" | "perception"
    mode: ExecutionMode = ExecutionMode.LANGUAGE
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)  # JSON schema of args
    guidance: list[str] = field(default_factory=list)  # accepted guidance modalities


@dataclass
class RobotDescriptor(_Model):
    robot_id: str
    model: str
    features: list[str]
    skills: list[SkillSpec]
    status: str = "idle"
    location: str | None = None
    holding: str | None = None
    calibrated: bool = True
    adapter: str = "sim"
    policy: dict[str, Any] = field(default_factory=dict)  # policy card summary


@dataclass
class StepTemplate(_Model):
    """One step of a domain workflow before it is bound to a robot."""

    step_id: str
    skill: str
    args: dict[str, Any] = field(default_factory=dict)
    instruction: str = ""
    depends_on: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)  # robot features
    optional: bool = False
    group: str | None = None  # steps sharing a group run on the same robot (e.g. pick -> carry -> place)


@dataclass
class WorkflowDescriptor(_Model):
    name: str
    domain: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)  # JSON schema


@dataclass
class ToolDescriptor(_Model):
    name: str
    domain: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class DomainDescriptor(_Model):
    name: str
    description: str
    vocabulary: list[str] = field(default_factory=list)
    safety: list[str] = field(default_factory=list)
    workflows: list[WorkflowDescriptor] = field(default_factory=list)
    tools: list[ToolDescriptor] = field(default_factory=list)


@dataclass
class CapabilityContract(_Model):
    protocol: str
    domains: list[DomainDescriptor]
    robots: list[RobotDescriptor]


# ---------------------------------------------------------------------------
# Guidance contract
# ---------------------------------------------------------------------------


@dataclass
class SpatialCue(_Model):
    """Box / point / region / trajectory in normalized image coordinates."""

    kind: str  # "box" | "point" | "region" | "trajectory"
    coords: list[float]  # box [x0,y0,x1,y1]; point [x,y]; region/trajectory flat [x0,y0,...]
    frame: str = "head"  # camera whose image the coordinates refer to
    role: str = "target"  # "target" | "placement" | "avoid" | "waypoint"
    label: str | None = None


@dataclass
class ObjectReference(_Model):
    """Visual goal conditioning: what the target is expected to look like."""

    ref_id: str | None = None  # catalog id (e.g. SKU) when known
    image: str | None = None  # URI: file://, https://, data:, sim://
    description: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    source: str | None = None  # "merchant_database" | "goal_image" | "camera_crop" | "user"


@dataclass
class Guidance(_Model):
    instruction: str = ""  # subtask instruction
    detailed: str | None = None  # dense prompt: order, arm assignment, approach, contact constraints
    constraints: list[str] = field(default_factory=list)
    spatial: list[SpatialCue] = field(default_factory=list)
    references: list[ObjectReference] = field(default_factory=list)
    arm: str | None = None  # "left" | "right" | "both"
    camera: str | None = None  # preferred perception camera: "head" | "wrist"
    approach: str | None = None
    options: dict[str, Any] = field(default_factory=dict)  # machine-readable execution flags

    def modalities(self) -> list[str]:
        mods = []
        if self.instruction:
            mods.append("language")
        if self.detailed or self.constraints:
            mods.append("detailed")
        if any(c.kind == "box" for c in self.spatial):
            mods.append("box")
        if any(c.kind == "point" for c in self.spatial):
            mods.append("point")
        if self.references:
            mods.append("reference")
        return mods


@dataclass
class ExecutionRequest(_Model):
    robot_id: str
    skill: str
    args: dict[str, Any] = field(default_factory=dict)
    guidance: Guidance = field(default_factory=Guidance)
    mode: ExecutionMode | None = None  # defaults to the robot's skill binding
    task_id: str | None = None
    step_id: str | None = None
    request_id: str | None = None
    timeout_s: float | None = None
    verify: list[str] = field(default_factory=list)  # extra criteria to evaluate
    tags: list[str] = field(default_factory=list)  # agent-provided context tags
    recovery: str | None = None  # recovery action this attempt applies, if any
    memory: list[str] = field(default_factory=list)  # long-term memory entries applied to this request


# ---------------------------------------------------------------------------
# Feedback contract
# ---------------------------------------------------------------------------


@dataclass
class Detection(_Model):
    label: str
    bbox: list[float]  # normalized [x0, y0, x1, y1]
    confidence: float
    camera: str
    ref_id: str | None = None  # recognized catalog id, if any
    attributes: dict[str, str] = field(default_factory=dict)
    track_id: str | None = None


@dataclass
class Observation(_Model):
    robot_id: str
    t: float
    camera: str | None = None
    location: str | None = None
    detections: list[Detection] = field(default_factory=list)
    robot_state: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)  # e.g. "person_in_workspace"
    frames: list[str] = field(default_factory=list)  # frame URIs, when available


@dataclass
class ActionResult(_Model):
    skill: str
    mode: ExecutionMode
    outcome: Outcome
    duration_s: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FailureReason(_Model):
    code: str
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    recoverable: bool = True


@dataclass
class CriterionResult(_Model):
    name: str
    passed: bool
    confidence: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Continuity contract
# ---------------------------------------------------------------------------


@dataclass
class StepState(_Model):
    step_id: str
    skill: str
    args: dict[str, Any] = field(default_factory=dict)
    instruction: str = ""
    depends_on: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    optional: bool = False
    group: str | None = None
    status: StepStatus = StepStatus.PENDING
    assigned_robot: str | None = None
    attempts: int = 0
    last_failure: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContinuityState(_Model):
    task_id: str
    goal: str
    status: TaskStatus = TaskStatus.ACTIVE
    completed_steps: list[str] = field(default_factory=list)
    pending_work: list[str] = field(default_factory=list)
    assigned_robot: str | None = None
    assignments: dict[str, str] = field(default_factory=dict)  # step_id -> robot_id
    held_objects: dict[str, str] = field(default_factory=dict)  # robot_id -> object
    dependencies: dict[str, list[str]] = field(default_factory=dict)  # step -> unmet deps
    domain: str | None = None
    workflow: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    steps: list[StepState] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def step(self, step_id: str) -> StepState:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        raise KeyError(step_id)

    def summary(self) -> dict[str, Any]:
        """Compact view with the published continuity fields first."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status.value,
            "completed_steps": list(self.completed_steps),
            "pending_work": list(self.pending_work),
            "assigned_robot": self.assigned_robot,
            "held_objects": dict(self.held_objects),
            "dependencies": {k: list(v) for k, v in self.dependencies.items()},
            "priority": self.priority,
        }


@dataclass
class Feedback(_Model):
    request_id: str
    robot_id: str
    success: bool
    observation: Observation
    action_result: ActionResult
    failure_reason: FailureReason | None = None
    progress: float = 0.0  # progress of the step, 0..1
    criteria: list[CriterionResult] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)  # recovery hints
    task_id: str | None = None
    step_id: str | None = None
    continuity: ContinuityState | None = None
    applied_memory: list[str] = field(default_factory=list)
    policy_success: bool | None = None  # what the policy itself reported
