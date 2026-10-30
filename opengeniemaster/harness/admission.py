"""Shared admission: validate and admit requests and memory changes.

The architecture figure places a "Shared Admission (validate · admit)" gate
between the coding agent and both robots and memory. Here it

* refuses execution requests that are malformed, target a robot without the
  skill or calibration, violate step dependencies or robot affinity, or would
  move a manipulator while a person is known to be in the workspace;
* validates memory proposals (schema, evidence, support), merges duplicates,
  rejects proposals that weaken safety rules, and resolves conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .._util import stable_hash
from ..eep.types import DONE_STEP_STATUSES, ContinuityState, ExecutionRequest, Observation, StepStatus
from ..memory.reflection import Proposal
from ..memory.store import KINDS, SCOPES, WHEN_KEYS, MemoryEntry, MemoryStore
from .recovery import RECOVERIES

if TYPE_CHECKING:  # pragma: no cover
    from ..embodiment.robots import RobotInstance

MANIPULATION_SKILLS = {"pick", "place", "hang", "handover", "wipe", "spread"}

PATCH_SCHEMA: dict[str, type | tuple[type, ...]] = {
    "camera": str,
    "arm": str,
    "approach": str,
    "constraints": list,
    "detailed": str,
    "options": dict,
    "use_reference": bool,
    "use_box": bool,
    "refresh_observation": bool,
    "use_placement_point": bool,
    "corner_points": bool,
    "verify_close_range": bool,
    "pause_on_intrusion": bool,
    "keep_gripper_closed": bool,
    "prefer_features": list,
    "max_attempts": int,
    "recovery_order": list,
    "search_first": list,
}
SAFETY_FLAGS = ("pause_on_intrusion", "keep_gripper_closed")


@dataclass
class Decision:
    admitted: bool
    action: str  # allow | reject | admit | merge
    reasons: list[str] = field(default_factory=list)
    entry_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"admitted": self.admitted, "action": self.action, "reasons": list(self.reasons)}
        if self.entry_id:
            out["entry_id"] = self.entry_id
        return out


RECOVERY_NAMES = frozenset(RECOVERIES)
STR_WHEN_KEYS = ("domain", "robot_model", "sku")
LIST_WHEN_KEYS = ("tags_any", "tags_all", "features_all")
STR_LIST_PATCH_KEYS = ("constraints", "prefer_features", "search_first", "recovery_order")


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _when_problems(when: Any) -> list[str]:
    if not isinstance(when, dict):
        return ["'when' must be an object"]
    problems = []
    unknown = sorted(set(when) - set(WHEN_KEYS))
    if unknown:
        problems.append(f"unknown 'when' keys {unknown}; allowed {list(WHEN_KEYS)}")
    for key in STR_WHEN_KEYS:
        if key in when and not isinstance(when[key], str):
            problems.append(f"'when.{key}' must be a string")
    if "skill" in when and not (isinstance(when["skill"], str) or _is_str_list(when["skill"])):
        problems.append("'when.skill' must be a string or a list of strings")
    for key in LIST_WHEN_KEYS:
        if key in when and not _is_str_list(when[key]):
            problems.append(f"'when.{key}' must be a list of strings")
    return problems


def _patch_problems(patch: Any) -> list[str]:
    if not isinstance(patch, dict):
        return ["'patch' must be an object"]
    problems = []
    for key, value in patch.items():
        typ = PATCH_SCHEMA.get(key)
        if typ is None:
            problems.append(f"unknown patch key '{key}'")
        elif not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            problems.append(f"patch '{key}' must be {getattr(typ, '__name__', typ)}")
        elif key in STR_LIST_PATCH_KEYS and not _is_str_list(value):
            problems.append(f"patch '{key}' must be a list of strings")
    if patch.get("camera") not in (None, "head", "wrist"):
        problems.append("patch 'camera' must be 'head' or 'wrist'")
    if isinstance(patch.get("max_attempts"), int) and not 1 <= patch["max_attempts"] <= 10:
        problems.append("patch 'max_attempts' must be between 1 and 10")
    if _is_str_list(patch.get("recovery_order")):
        unknown = [r for r in patch["recovery_order"] if r not in RECOVERY_NAMES]
        if unknown:
            problems.append(f"unknown recoveries in 'recovery_order': {unknown}")
    options = patch.get("options")
    if isinstance(options, dict):
        for k, v in options.items():
            if not isinstance(k, str) or not (v is None or isinstance(v, (str, int, float, bool))):
                problems.append("patch 'options' values must be strings, numbers, booleans or null")
                break
    for flag in SAFETY_FLAGS:
        for where, container in (("patch", patch), ("patch.options", options if isinstance(options, dict) else {})):
            if flag in container and container[flag] is not True:
                problems.append(f"{where}.{flag} may only be true: proposals may not weaken safety behaviour")
    return problems


def _safety_on(patch: dict[str, Any], flag: str) -> bool:
    options = patch.get("options") if isinstance(patch.get("options"), dict) else {}
    return patch.get(flag) is True or options.get(flag) is True


def subsumes(general: dict[str, Any], specific: dict[str, Any]) -> bool:
    """True if every context matched by ``specific`` is also matched by ``general``."""
    for key, value in general.items():
        if key in ("tags_all", "features_all"):
            if not set(value) <= set(specific.get(key, [])):
                return False
        elif key == "skill":
            g = set(value if isinstance(value, list) else [value])
            s = specific.get(key)
            if s is None or not set(s if isinstance(s, list) else [s]) <= g:
                return False
        elif specific.get(key) != value:
            return False
    return True


def entry_id_for(p: Proposal) -> str:
    return "mem-" + stable_hash({"scope": p.scope, "kind": p.kind, "when": p.when, "patch": p.patch, "text": p.text.lower()}, 8)


class SharedAdmission:
    def __init__(self, *, min_support: int = 2, agent_min_support: int = 1, max_entries: int = 200,
                 safety_interlock: bool = True) -> None:
        self.min_support = min_support
        self.agent_min_support = agent_min_support
        self.max_entries = max_entries
        self.safety_interlock = safety_interlock

    # ------------------------------------------------------------ requests

    def check_request(self, req: ExecutionRequest, robot: "RobotInstance", task: ContinuityState | None,
                      last_observation: Observation | None = None) -> Decision:
        reasons: list[str] = []
        if robot.status in ("stopped", "offline"):
            reasons.append(f"robot {robot.robot_id} is {robot.status}")
        spec = robot.skill(req.skill)
        if spec is None:
            reasons.append(f"robot {robot.robot_id} ({robot.model}) has no skill '{req.skill}'")
        if req.skill in MANIPULATION_SKILLS and not robot.calibration.valid:
            reasons.append(f"robot {robot.robot_id} calibration '{robot.calibration.version}' is not valid")
        for cue in req.guidance.spatial:
            n = len(cue.coords)
            if cue.kind == "box" and n != 4 or cue.kind == "point" and n != 2 or n == 0:
                reasons.append(f"spatial cue '{cue.kind}' has {n} coordinates")
            elif any(not (0.0 <= c <= 1.0) for c in cue.coords):
                reasons.append("spatial cue coordinates must be normalized to [0, 1]")
        for ref in req.guidance.references:
            if not (ref.ref_id or ref.image or ref.description):
                reasons.append("object reference needs ref_id, image or description")
        if task is not None and req.step_id is not None:
            try:
                step = task.step(req.step_id)
            except KeyError:
                reasons.append(f"task {task.task_id} has no step '{req.step_id}'")
                step = None
            if step is not None:
                if step.skill != req.skill:
                    reasons.append(f"step {step.step_id} expects skill '{step.skill}', got '{req.skill}'")
                if step.status in DONE_STEP_STATUSES:
                    reasons.append(f"step {step.step_id} is already {step.status.value}")
                if step.status == StepStatus.CANCELLED:
                    reasons.append(f"step {step.step_id} is cancelled")
                unmet = [d for d in step.depends_on if task.step(d).status not in DONE_STEP_STATUSES]
                if unmet:
                    reasons.append(f"dependencies not done: {unmet}")
                missing = sorted(set(step.requires) - set(robot.features))
                if missing:
                    reasons.append(f"robot {robot.robot_id} lacks required features {missing}")
                if step.assigned_robot and step.assigned_robot != robot.robot_id and step.group:
                    holder = next((r for r, obj in task.held_objects.items() if r == step.assigned_robot), None)
                    if holder:
                        reasons.append(
                            f"work item '{step.group}' is bound to {step.assigned_robot}, which holds {task.held_objects[holder]}; "
                            "hand off explicitly or let that robot continue"
                        )
        if (self.safety_interlock and req.skill in MANIPULATION_SKILLS and last_observation is not None
                and "person_in_workspace" in last_observation.flags
                and not (req.guidance.options.get("pause_on_intrusion"))):
            reasons.append("a person is in the workspace: wait, or set guidance.options.pause_on_intrusion")
        if reasons:
            return Decision(False, "reject", reasons)
        return Decision(True, "allow")

    # ------------------------------------------------------------ memory

    def _validate(self, p: Proposal, memory: MemoryStore) -> list[str]:
        problems = []
        if p.scope not in SCOPES:
            problems.append(f"scope must be one of {SCOPES}")
        if p.kind not in KINDS:
            problems.append(f"kind must be one of {KINDS}")
        if not isinstance(p.text, str) or not (8 <= len(p.text) <= 600):
            problems.append("text must be 8..600 characters")
        problems += _when_problems(p.when)
        problems += _patch_problems(p.patch)
        evidence = list(dict.fromkeys(p.evidence))
        missing = [e for e in evidence if e not in memory.temporary]
        if missing:
            problems.append(f"unknown evidence records {missing[:5]}")
        support = len(self.support(p, memory))
        if p.source == "human":
            needed = 0
        elif p.kind == "safety":
            needed = 1
        elif p.source.startswith("reflection"):
            needed = self.min_support
        else:
            needed = self.agent_min_support
        if support < needed:
            problems.append(f"needs at least {needed} distinct relevant execution record(s) as evidence, has {support}")
        return problems

    @staticmethod
    def support(p: Proposal, memory: MemoryStore) -> set[str]:
        """Distinct execution records (not notes) cited as evidence that fit the proposal's context."""
        out = set()
        for rid in dict.fromkeys(p.evidence):
            rec = memory.temporary.get(rid)
            if rec is None or rec.kind != "execution":
                continue
            if "domain" in p.when and rec.domain != p.when["domain"]:
                continue
            if "robot_model" in p.when and rec.robot_model != p.when["robot_model"]:
                continue
            out.add(rid)
        return out

    def _conflicts(self, p: Proposal, memory: MemoryStore) -> list[MemoryEntry]:
        out = []
        for e in memory.long_term.admitted():
            if e.when != p.when:
                continue
            for key, value in p.patch.items():
                if key in e.patch and e.patch[key] != value and key not in ("constraints", "evidence"):
                    out.append(e)
                    break
        return out

    def check_proposal(self, p: Proposal, memory: MemoryStore) -> Decision:
        problems = self._validate(p, memory)
        if problems:
            return Decision(False, "reject", problems)
        eid = entry_id_for(p)
        existing = memory.long_term.entries.get(eid)
        if existing is None:
            for e in memory.long_term.admitted():
                if e.scope == p.scope and e.kind == p.kind and e.when == p.when and e.patch == p.patch and e.patch:
                    existing = e
                    break
        if existing is not None and existing.status == "admitted":
            return Decision(True, "merge", ["duplicate of an admitted entry; evidence merged"], existing.entry_id)
        for e in memory.long_term.admitted():
            if e.kind == p.kind and e.patch == p.patch and e.patch and subsumes(e.when, p.when):
                return Decision(True, "merge", [f"covered by the more general entry {e.entry_id}; evidence merged"], e.entry_id)
        conflicts = [c for c in self._conflicts(p, memory) if c.entry_id != p.supersedes]
        if conflicts:
            return Decision(False, "reject", [f"conflicts with {c.entry_id}: {c.text!r}" for c in conflicts])
        if p.supersedes:
            old = memory.long_term.entries.get(p.supersedes)
            if old is None or old.status != "admitted":
                return Decision(False, "reject", [f"supersedes unknown or retired entry {p.supersedes}"])
            if old.kind == "safety" or any(_safety_on(old.patch, f) for f in SAFETY_FLAGS):
                kept = [f for f in SAFETY_FLAGS if not _safety_on(old.patch, f) or _safety_on(p.patch, f)]
                if p.kind != "safety" and old.kind == "safety":
                    return Decision(False, "reject", ["a safety entry can only be superseded by another safety entry"])
                if len(kept) != len(SAFETY_FLAGS):
                    return Decision(False, "reject", [f"the replacement must keep the safety behaviour of {old.entry_id}"])
            old_support = len({r for r in old.evidence if r in memory.temporary}) or len(set(old.evidence))
            if len(self.support(p, memory)) < old_support:
                return Decision(False, "reject", [f"needs at least as much distinct evidence as {old.entry_id} ({old_support})"])
        return Decision(True, "admit", [p.rationale] if p.rationale else [], eid)

    def admit(self, p: Proposal, memory: MemoryStore, *, t: float = 0.0) -> Decision:
        decision = self.check_proposal(p, memory)
        if not decision.admitted:
            return decision
        lt = memory.long_term
        if decision.action == "merge":
            assert decision.entry_id is not None
            lt.merge_evidence(decision.entry_id, p.evidence, t=t)
            return decision
        if p.supersedes:
            lt.retire(p.supersedes, reason=f"superseded by {decision.entry_id}", t=t)
        admitted = lt.admitted()
        if len(admitted) >= self.max_entries:
            victim = min((e for e in admitted if e.kind != "safety"), key=lambda e: (e.succeeded - e.applied, e.updated_at),
                         default=None)
            if victim is not None:
                lt.retire(victim.entry_id, reason="capacity", t=t)
        entry = MemoryEntry(
            entry_id=decision.entry_id or entry_id_for(p),
            scope=p.scope,
            kind=p.kind,
            text=p.text,
            when=dict(p.when),
            patch=dict(p.patch),
            evidence=sorted(set(p.evidence)),
            source=p.source,
            created_at=t,
        )
        lt.add(entry, reason=p.rationale or p.source, t=t)
        return decision
