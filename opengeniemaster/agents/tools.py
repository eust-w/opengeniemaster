"""The EEP tool surface exposed to coding agents.

One registry backs both the MCP server (Claude Code, Codex, ...) and the
built-in LLM agent. Each tool maps 1:1 onto a :class:`Harness` operation and
returns JSON-ready data. Names use ``[a-z0-9_]`` only so that they are valid
on every tool-calling API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..eep.errors import EEPError, InvalidRequest, ToolError
from ..eep.schema import SchemaError, schema_for, validate
from ..eep.types import ExecutionMode, ExecutionRequest, Feedback, Guidance
from ..harness.practice import capability_gaps, plan_practice

if TYPE_CHECKING:  # pragma: no cover
    from ..harness.harness import Harness

Handler = Callable[["Harness", dict[str, Any]], Any]

GUIDANCE_SCHEMA = schema_for(Guidance)
GUIDANCE_SCHEMA["description"] = (
    "Multimodal guidance for the policy: instruction (subtask), detailed (dense language: order, arm assignment, approach, contact "
    "constraints), constraints, spatial cues (normalized boxes/points in a camera frame; role target|placement|waypoint|avoid), "
    "references (catalog/goal images), camera ('head'|'wrist'), arm, approach, options (keep_gripper_closed, pause_on_intrusion, "
    "verify_close_range)."
)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    title: str = ""
    read_only: bool = False
    destructive: bool = False
    tags: list[str] = field(default_factory=list)

    def mcp(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.title:
            out["title"] = self.title
        out["annotations"] = {"readOnlyHint": self.read_only, "destructiveHint": self.destructive, "openWorldHint": not self.read_only}
        return out

    def anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
O = {"type": "object"}


def feedback_view(fb: Feedback) -> dict[str, Any]:
    """Compact feedback for agents: the three published fields plus decisions."""
    out: dict[str, Any] = {
        "request_id": fb.request_id,
        "robot_id": fb.robot_id,
        "success": fb.success,
        "progress": fb.progress,
        "action_result": fb.action_result.to_dict(),
        "observation": fb.observation.to_dict(),
    }
    if fb.failure_reason is not None:
        out["failure_reason"] = fb.failure_reason.to_dict()
    if fb.policy_success is not None and fb.policy_success != fb.success:
        out["policy_reported_success"] = fb.policy_success
    if fb.criteria:
        out["criteria"] = [c.to_dict() for c in fb.criteria]
    if fb.suggestions:
        out["suggestions"] = fb.suggestions
    if fb.continuity is not None:
        out["continuity"] = fb.continuity.summary()
        out["next_steps"] = [
            {"step_id": s.step_id, "skill": s.skill, "assigned_robot": s.assigned_robot, "status": s.status.value}
            for s in fb.continuity.steps
            if s.status.value in ("ready", "assigned")
        ]
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _capabilities(h: "Harness", a: dict[str, Any]) -> Any:
    return h.capabilities(domain=a.get("domain"), robot_id=a.get("robot_id"))


def _observe(h: "Harness", a: dict[str, Any]) -> Any:
    return h.observe(a["robot_id"], camera=a.get("camera", "head")).to_dict()


def _task_create(h: "Harness", a: dict[str, Any]) -> Any:
    task = h.create_task(a["goal"], domain=a.get("domain"), workflow=a.get("workflow"), params=a.get("params"), steps=a.get("steps"),
                         priority=int(a.get("priority", 0)))
    return {"task": task.summary(), "steps": [s.to_dict() for s in task.steps], "context": h.task_context(task.task_id)}


def _task_get(h: "Harness", a: dict[str, Any]) -> Any:
    task = h.get_task(a["task_id"])
    out: dict[str, Any] = {"task": task.summary()}
    if a.get("steps", True):
        out["steps"] = [s.to_dict() for s in task.steps]
    if a.get("context", True):
        out["context"] = h.task_context(task.task_id)
    return out


def _task_list(h: "Harness", a: dict[str, Any]) -> Any:
    return {"tasks": [t.summary() for t in h.list_tasks(a.get("status"))]}


def _task_update(h: "Harness", a: dict[str, Any]) -> Any:
    task = h.update_task(a["task_id"], a["action"], step_id=a.get("step_id"), robot_id=a.get("robot_id"), note=a.get("note"),
                         force=bool(a.get("force", False)))
    return {"task": task.summary()}


def _dispatch(h: "Harness", a: dict[str, Any]) -> Any:
    return {"assignments": h.dispatch(a.get("task_id"))}


def _suggest(h: "Harness", a: dict[str, Any]) -> Any:
    return h.suggest_guidance(a["task_id"], a["step_id"], a.get("robot_id"), observe=bool(a.get("observe", True)))


def _execute(h: "Harness", a: dict[str, Any]) -> Any:
    guidance = a.get("guidance") or {}
    if isinstance(guidance, str):
        guidance = {"instruction": guidance}
    req = ExecutionRequest(
        robot_id=a["robot_id"],
        skill=a["skill"],
        args=dict(a.get("args") or {}),
        guidance=Guidance.from_dict(guidance),
        mode=ExecutionMode(a["mode"]) if a.get("mode") else None,
        task_id=a.get("task_id"),
        step_id=a.get("step_id"),
        verify=list(a.get("verify") or []),
        tags=list(a.get("tags") or []),
        recovery=a.get("recovery"),
        memory=list(a.get("memory") or []),
    )
    return feedback_view(h.execute(req))


def _verify(h: "Harness", a: dict[str, Any]) -> Any:
    close = bool(a.get("close_range", True))
    if a.get("step_id"):
        return h.recheck_step(a["task_id"], a["step_id"], robot_id=a.get("robot_id"), close_range=close)
    return {"results": h.verify_task(a["task_id"], close_range=close)}


def _wait(h: "Harness", a: dict[str, Any]) -> Any:
    return h.wait(a["robot_id"], float(a.get("seconds", 10.0)))


def _stop(h: "Harness", a: dict[str, Any]) -> Any:
    return h.stop_robot(a["robot_id"], a.get("reason", "agent stop"))


def _resume(h: "Harness", a: dict[str, Any]) -> Any:
    return h.resume_robot(a["robot_id"])


def _memory_search(h: "Harness", a: dict[str, Any]) -> Any:
    return {"entries": h.memory_search(query=a.get("query"), domain=a.get("domain"), skill=a.get("skill"), robot_model=a.get("robot_model"),
                                       tags=a.get("tags"), sku=a.get("sku"))}


def _memory_note(h: "Harness", a: dict[str, Any]) -> Any:
    return h.memory_note(a["text"], task_id=a.get("task_id"), step_id=a.get("step_id"), robot_id=a.get("robot_id"), tags=a.get("tags"),
                         kind=a.get("kind", "note"))


def _memory_records(h: "Harness", a: dict[str, Any]) -> Any:
    records = h.pending_records(limit=int(a.get("limit", 120)))
    if a.get("failures_only"):
        records = [r for r in records if not r.get("success") or r.get("recovery") or r.get("contradicted")]
    return {"pending": len(h.memory.temporary.pending()), "records": records,
            "hint": "Propose instructions with memory_propose, citing record_id values as evidence."}


def _memory_propose(h: "Harness", a: dict[str, Any]) -> Any:
    proposals = [p for p in a["proposals"] if isinstance(p, dict)]
    decisions = h.memory_propose(proposals, source="agent")  # provenance is fixed by this path, not by the caller
    if a.get("mark_reflected"):
        h.memory.temporary.advance()
    return {"decisions": decisions, "memory": h.memory.stats()}


def _memory_reflect(h: "Harness", a: dict[str, Any]) -> Any:
    return h.reflect(dry_run=bool(a.get("dry_run", False))).to_dict()


def _gaps(h: "Harness", a: dict[str, Any]) -> Any:
    gaps = capability_gaps(h)
    out: dict[str, Any] = {"gaps": [g.to_dict() for g in gaps[: int(a.get("limit", 5))]]}
    if a.get("schedule_practice") and gaps:
        idx = int(a.get("gap_index", 0))
        out["practice_task_id"] = plan_practice(h, gaps[idx]) if 0 <= idx < len(gaps) else None
    return out


def _domain_tool(domain: str, tool: str) -> Handler:
    def handler(h: "Harness", a: dict[str, Any]) -> Any:
        return h.call_tool(domain, tool, a)

    return handler


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def build_tools(harness: "Harness") -> list[Tool]:
    tools = [
        Tool("eep_capabilities", "Capability contract: protocol version, domains (workflows, composed tools, safety rules) and robots "
             "(features, skills with execution service bindings, location, calibration, policy card). Call this first.",
             _obj({"domain": S, "robot_id": S}), _capabilities, title="Capabilities", read_only=True),
        Tool("eep_observe", "Capture an observation from a robot camera at its current location: detections (label, catalog ref_id, "
             "normalized bbox, confidence), robot state (location, holding, held_label) and flags such as person_in_workspace. "
             "Use camera='wrist' for a close-range look.",
             _obj({"robot_id": S, "camera": {"type": "string", "enum": ["head", "wrist"]}}, ["robot_id"]), _observe, title="Observe"),
        Tool("eep_task_create", "Continuity contract: create a persistent task from a domain workflow (+params) or from explicit steps "
             "(step_id, skill, args, depends_on, criteria, requires, group). Returns continuity state and task context with memory.",
             _obj({"goal": S, "domain": S, "workflow": S, "params": O, "priority": I,
                   "steps": {"type": "array", "items": {"type": "object"}}}, ["goal"]), _task_create, title="Create task"),
        Tool("eep_task_get", "Continuity contract: task state (task_id, completed_steps, assigned_robot, pending_work, held_objects, "
             "dependencies), all steps, and task context (domain goal and limits, next steps, relevant long-term memory).",
             _obj({"task_id": S, "steps": B, "context": B}, ["task_id"]), _task_get, title="Get task", read_only=True),
        Tool("eep_task_list", "List tasks, optionally filtered by status (active, paused, interrupted, completed, failed, cancelled).",
             _obj({"status": {"type": "array", "items": S}}), _task_list, title="List tasks", read_only=True),
        Tool("eep_task_update", "Change task state: pause, resume, interrupt, cancel, fail, complete, retry_step, skip_step, fail_step, reopen "
             "(comma-separated step ids), assign (step to robot), handoff (move a work item to another robot; force restarts it if the "
             "old robot holds its object), note.",
             _obj({"task_id": S, "action": S, "step_id": S, "robot_id": S, "note": S, "force": B}, ["task_id", "action"]), _task_update,
             title="Update task"),
        Tool("eep_dispatch", "Assign ready, unassigned steps to capable idle robots (respects work-item affinity and memory "
             "coordination rules). Omit task_id to dispatch across all active tasks.",
             _obj({"task_id": S}), _dispatch, title="Dispatch"),
        Tool("eep_suggest_guidance", "Draft guidance for a step from business records (reference images, planogram placement points), "
             "the latest observation (target box) and long-term memory. Returns guidance, resolved args, applied memory ids and notes; "
             "review it, then pass it to eep_execute.",
             _obj({"task_id": S, "step_id": S, "robot_id": S, "observe": B}, ["task_id", "step_id"]), _suggest, title="Suggest guidance"),
        Tool("eep_execute", "Guidance + Feedback contracts: execute one skill on a robot. Include task_id/step_id to advance a task. "
             "Returns success (decided by completion criteria, not the policy's own signal), observation, action_result, "
             "failure_reason, criteria, recovery suggestions and updated continuity. Pass the memory entry ids you applied in "
             "'memory' and the recovery you are attempting in 'recovery'.",
             _obj({
                 "robot_id": S, "skill": S, "args": O, "guidance": GUIDANCE_SCHEMA, "task_id": S, "step_id": S,
                 "mode": {"type": "string", "enum": [m.value for m in ExecutionMode]},
                 "verify": {"type": "array", "items": S}, "tags": {"type": "array", "items": S}, "recovery": S,
                 "memory": {"type": "array", "items": S},
             }, ["robot_id", "skill"]), _execute, title="Execute"),
        Tool("eep_verify", "Re-check completion criteria (close range by default) for one step (reconciles continuity: a false "
             "failure becomes success, a false success becomes failure and its record is contradicted) or for all completed steps "
             "with persistent effects of a task.",
             _obj({"task_id": S, "step_id": S, "robot_id": S, "close_range": B}, ["task_id"]), _verify, title="Verify"),
        Tool("eep_wait", "Let a robot hold position for some seconds (e.g. until its action service restarts or a person leaves).",
             _obj({"robot_id": S, "seconds": {"type": "number", "minimum": 0}}, ["robot_id"]), _wait, title="Wait"),
        Tool("eep_robot_stop", "Stop a robot (operator safety stop). Tasks with unfinished work on it are paused until resumed "
             "explicitly with eep_task_update action=resume (after eep_robot_resume or a handoff).",
             _obj({"robot_id": S, "reason": S}, ["robot_id"]), _stop, title="Stop robot", destructive=True),
        Tool("eep_robot_resume", "Clear a robot stop.", _obj({"robot_id": S}, ["robot_id"]), _resume, title="Resume robot"),
        Tool("memory_search", "Search long-term memory: by context (domain, skill, robot_model, tags, sku) or free-text query.",
             _obj({"query": S, "domain": S, "skill": S, "robot_model": S, "tags": {"type": "array", "items": S}, "sku": S}),
             _memory_search, title="Search memory", read_only=True),
        Tool("memory_note", "Add a note to temporary memory (an observation or diagnosis the harness cannot record itself).",
             _obj({"text": S, "task_id": S, "step_id": S, "robot_id": S, "tags": {"type": "array", "items": S},
                   "kind": {"type": "string", "enum": ["note", "takeover"]}}, ["text"]), _memory_note, title="Note"),
        Tool("memory_records", "Temporary memory awaiting reflection: execution records with failures, evidence, recoveries and "
             "contradictions. Read these to reflect, then call memory_propose.",
             _obj({"limit": I, "failures_only": B}), _memory_records, title="Pending records", read_only=True),
        Tool("memory_propose", "Propose long-term instructions (scope domain|embodiment; kind guidance|criteria|recovery|coordination|"
             "safety; text; when {domain, skill, robot_model, tags_all, tags_any, sku}; optional patch; evidence record ids). Shared "
             "admission validates evidence, merges duplicates and rejects unsafe changes. Set mark_reflected after a full review.",
             _obj({"proposals": {"type": "array", "items": {"type": "object"}}, "mark_reflected": B}, ["proposals"]),
             _memory_propose, title="Propose memory"),
        Tool("memory_reflect", "Run the built-in rule reflector over pending records (dry_run to preview decisions).",
             _obj({"dry_run": B}), _memory_reflect, title="Reflect"),
        Tool("eep_capability_gaps", "Capability gaps from recent execution records (repeated failure, instability, slowness); "
             "optionally schedule a practice task for idle time.",
             _obj({"limit": I, "schedule_practice": B, "gap_index": I}), _gaps, title="Capability gaps"),
    ]
    for dname, domain in sorted(harness.domains.items()):
        for tname, tool in sorted(domain.tools.items()):
            tools.append(Tool(f"{dname}_{tname}", f"[{dname}] {tool.description}", tool.params or _obj({}), _domain_tool(dname, tname),
                              title=f"{dname}: {tname}", read_only=tname in ("lookup", "orders")))
    return tools


class ToolRegistry:
    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        self.tools = {t.name: t for t in build_tools(harness)}

    def list(self) -> list[Tool]:
        return list(self.tools.values())

    def call(self, name: str, arguments: dict[str, Any] | None) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            raise InvalidRequest(f"unknown tool '{name}'", data={"tools": sorted(self.tools)})
        # an explicit null means "not given" for optional arguments
        args = {k: v for k, v in (arguments or {}).items() if v is not None}
        try:
            validate(args, tool.input_schema)
        except SchemaError as exc:
            raise InvalidRequest(f"invalid arguments for {name}: {exc}", data={"errors": exc.errors}) from None
        try:
            return tool.handler(self.harness, args)
        except EEPError:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise InvalidRequest(f"{name}: {exc}") from exc
        except Exception as exc:  # report, never abort the agent's loop
            raise ToolError(f"{name} failed: {type(exc).__name__}: {exc}") from exc
