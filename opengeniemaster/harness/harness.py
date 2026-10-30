"""The GenieMaster-style harness: one stable contract between reasoning and action.

``Harness`` is the server side of EEP. A coding agent (Claude Code, Codex,
an OpenAI-compatible model, or the built-in scripted orchestrator) calls it
to discover capabilities, create and resume tasks, execute steps with
multimodal guidance, read feedback, and reflect into memory. Robots are
reached through execution adapters; the harness never assumes a particular
robot or backend.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .._util import IdFactory
from ..domain.base import DEFAULT_CRITERIA, Domain
from ..eep.errors import Conflict, InvalidRequest, NotFound
from ..eep.types import (
    DONE_STEP_STATUSES,
    PROTOCOL_VERSION,
    TERMINAL_TASK_STATUSES,
    ActionResult,
    CapabilityContract,
    ContinuityState,
    CriterionResult,
    ExecutionMode,
    ExecutionRequest,
    FailureReason,
    Feedback,
    Observation,
    Outcome,
    StepState,
    StepStatus,
    StepTemplate,
    TaskStatus,
)
from ..embodiment.robots import Fleet, RobotInstance
from ..memory.reflection import Proposal, ReflectionReport, Reflector, RuleReflector, reflect
from ..memory.store import ExecutionRecord, MemoryStore
from . import recovery as recovery_lib
from .admission import Decision, SharedAdmission
from .continuity import TaskStore
from .criteria import PERSISTENT_EFFECT, criterion_args, failure_code
from .events import EventLog
from .guidance import ALL_MODALITIES, GuidanceBuilder, GuidancePlan


@dataclass
class HarnessConfig:
    evaluate_criteria: bool = True
    record_observations: bool = True
    max_suggestions: int = 3


class Harness:
    def __init__(
        self,
        *,
        domains: Iterable[Domain] | dict[str, Domain],
        fleet: Fleet,
        memory: MemoryStore | None = None,
        admission: SharedAdmission | None = None,
        ids: IdFactory | None = None,
        events: EventLog | None = None,
        state_dir: str | Path | None = None,
        config: HarnessConfig | None = None,
        persist_tasks: bool = True,
    ) -> None:
        self.domains: dict[str, Domain] = dict(domains) if isinstance(domains, dict) else {d.name: d for d in domains}
        self.fleet = fleet
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.memory = memory or MemoryStore(self.state_dir / "memory" if self.state_dir else None)
        self.admission = admission or SharedAdmission()
        self.ids = ids or IdFactory()
        self.events = events or EventLog(self.state_dir / "events.jsonl" if self.state_dir else None)
        # Simulated worlds restart empty, so their tasks are not persisted; real fleets keep continuity across restarts.
        self.tasks = TaskStore(self.ids, self.state_dir if persist_tasks else None)
        self.builder = GuidanceBuilder(self.memory.long_term)
        self.config = config or HarnessConfig()
        self.counters: Counter[str] = Counter()
        self._last_obs: dict[str, Observation] = {}
        self._tried: dict[tuple[str, str], list[str]] = {}

    # ------------------------------------------------------------------ time

    def now(self, robot_id: str | None = None) -> float:
        if robot_id is not None:
            robot = self.fleet.get(robot_id)
            return robot.adapter.now(robot)
        return max((r.adapter.now(r) for r in self.fleet), default=0.0)

    def wait(self, robot_id: str, seconds: float) -> dict[str, Any]:
        robot = self.fleet.get(robot_id)
        robot.adapter.wait(robot, seconds)
        self.counters["wait"] += 1
        self.events.emit("wait", self.now(robot_id), {"robot_id": robot_id, "seconds": seconds})
        return {"robot_id": robot_id, "t": round(self.now(robot_id), 2)}

    # -------------------------------------------------------- capability contract

    def domain(self, name: str | None) -> Domain | None:
        if name is None:
            return next(iter(self.domains.values())) if len(self.domains) == 1 else None
        try:
            return self.domains[name]
        except KeyError:
            raise NotFound(f"unknown domain '{name}'", data={"domains": sorted(self.domains)}) from None

    def capabilities(self, domain: str | None = None, robot_id: str | None = None) -> dict[str, Any]:
        self.counters["capabilities"] += 1
        domains = [self.domain(domain)] if domain else list(self.domains.values())
        robots = [self.fleet.get(robot_id)] if robot_id else list(self.fleet)
        contract = CapabilityContract(
            protocol=PROTOCOL_VERSION,
            domains=[d.describe() for d in domains if d is not None],
            robots=[r.descriptor() for r in robots],
        )
        out = contract.to_dict()
        out["memory"] = self.memory.stats()
        out["recoveries"] = {k: v.description for k, v in recovery_lib.RECOVERIES.items()}
        return out

    # -------------------------------------------------------- continuity contract

    def create_task(self, goal: str, *, domain: str | None = None, workflow: str | None = None, params: dict[str, Any] | None = None,
                    steps: list[dict[str, Any] | StepTemplate] | None = None, priority: int = 0) -> ContinuityState:
        dom = self.domain(domain)
        if workflow:
            if dom is None:
                raise InvalidRequest("domain is required when several domains are loaded", data={"domains": sorted(self.domains)})
            templates = dom.expand(workflow, dict(params or {}))
        elif steps:
            templates = [s if isinstance(s, StepTemplate) else StepTemplate.from_dict(s) for s in steps]
            for s in templates:
                if not s.criteria and s.skill in DEFAULT_CRITERIA:
                    s.criteria = list(DEFAULT_CRITERIA[s.skill])
        else:
            raise InvalidRequest("give either a workflow (+params) or explicit steps")
        task = self.tasks.create(goal, templates, t=self.now(), domain=dom.name if dom else domain, workflow=workflow,
                                 params=params, priority=priority)
        self.events.emit("task", self.now(), {
            "action": "create",
            "task": task.summary(),
            "domain": task.domain,
            "workflow": workflow,
            "params": params,
            "steps": None if workflow else [s.to_dict() for s in templates],
        })
        self.tasks.save()
        return task

    def get_task(self, task_id: str) -> ContinuityState:
        return self.tasks.get(task_id)

    def list_tasks(self, status: Iterable[str] | None = None) -> list[ContinuityState]:
        statuses = [TaskStatus(s) for s in status] if status else None
        return self.tasks.list(statuses)

    def ready_steps(self, task_id: str) -> list[StepState]:
        return self.tasks.ready_steps(self.tasks.get(task_id))

    def task_context(self, task_id: str) -> dict[str, Any]:
        """Task Context: domain goal and limits, continuity and relevant memory."""
        task = self.tasks.get(task_id)
        dom = self.domain(task.domain) if task.domain else None
        memory_hits: dict[str, Any] = {}
        for step in task.steps:
            if step.status in DONE_STEP_STATUSES:
                continue
            robot = self._robot_for_context(step)
            if robot is None:
                continue
            ctx = self.builder.context(dom, step, robot, self._safe_args(task, step))
            for e in self.memory.long_term.relevant(ctx):
                memory_hits.setdefault(e.entry_id, e.brief())
        ready = self.tasks.ready_steps(task)
        return {
            "continuity": task.summary(),
            "domain": {"name": dom.name, "description": dom.description, "safety": dom.safety} if dom else None,
            "next_steps": [
                {"step_id": s.step_id, "skill": s.skill, "args": s.args, "instruction": s.instruction,
                 "assigned_robot": s.assigned_robot, "status": s.status.value, "criteria": s.criteria}
                for s in ready
            ],
            "failed_steps": [{"step_id": s.step_id, "skill": s.skill, "last_failure": s.last_failure, "attempts": s.attempts}
                             for s in self.tasks.failed_steps(task)],
            "memory": list(memory_hits.values()),
            "notes": list(task.notes),
        }

    def _robot_for_context(self, step: StepState) -> RobotInstance | None:
        if step.assigned_robot and step.assigned_robot in self.fleet:
            return self.fleet.get(step.assigned_robot)
        capable = self.fleet.capable(step.skill, step.requires)
        return capable[0] if capable else None

    def _safe_args(self, task: ContinuityState, step: StepState) -> dict[str, Any]:
        try:
            return self.tasks.resolve_args(task, step)
        except Conflict:
            return {k: v for k, v in step.args.items() if not (isinstance(v, str) and v.startswith("@"))}

    def update_task(self, task_id: str, action: str, *, step_id: str | None = None, robot_id: str | None = None,
                    note: str | None = None, force: bool = False) -> ContinuityState:
        task = self.tasks.get(task_id)
        t = self.now()
        if action == "pause":
            self.tasks.set_status(task, TaskStatus.PAUSED, t=t, note=note)
        elif action == "resume":
            if task.status in TERMINAL_TASK_STATUSES:
                raise Conflict(f"task {task_id} is {task.status.value}; reopen steps instead of resuming it")
            stopped = sorted({s.assigned_robot for s in task.steps if s.assigned_robot and s.status not in DONE_STEP_STATUSES
                              and s.assigned_robot in self.fleet and self.fleet.get(s.assigned_robot).status == "stopped"})
            if stopped and not force:
                raise Conflict(f"robots {stopped} are stopped; resume them or hand their work off first (force=true to override)")
            self.tasks.set_status(task, TaskStatus.ACTIVE, t=t, note=note)
        elif action == "interrupt":
            self.tasks.interrupt(task, t=t, note=note or "interrupted")
        elif action == "cancel":
            self.tasks.set_status(task, TaskStatus.CANCELLED, t=t, note=note)
        elif action == "fail":
            self.tasks.set_status(task, TaskStatus.FAILED, t=t, note=note)
        elif action == "complete":
            self.tasks.set_status(task, TaskStatus.COMPLETED, t=t, note=note)
        elif action == "retry_step":
            self._need(step_id, action)
            self.tasks.retry_step(task, step_id, t=t)  # type: ignore[arg-type]
        elif action == "skip_step":
            self._need(step_id, action)
            self.tasks.skip_step(task, step_id, t=t, note=note)  # type: ignore[arg-type]
        elif action == "fail_step":
            self._need(step_id, action)
            self.tasks.finish(task, step_id, success=False, t=t, failure=note or "failed by agent")  # type: ignore[arg-type]
        elif action == "reopen":
            self._need(step_id, action)
            self.tasks.reopen(task, [s.strip() for s in str(step_id).split(",")], t=t, note=note)
        elif action == "handoff":
            self._need(robot_id, action)
            self._handoff(task, step_id, robot_id, force=force)  # type: ignore[arg-type]
        elif action == "assign":
            self._need(step_id, action)
            self._need(robot_id, action)
            robot = self.fleet.get(robot_id)  # type: ignore[arg-type]
            step = task.step(step_id)  # type: ignore[arg-type]
            if not robot.can(step.skill, step.requires):
                raise Conflict(f"{robot_id} cannot perform {step.skill} (requires {step.requires})")
            self.tasks.assign(task, step_id, robot_id, t=t)  # type: ignore[arg-type]
        elif action == "note":
            task.notes.append(note or "")
        else:
            raise InvalidRequest(f"unknown action '{action}'",
                                 data={"actions": ["pause", "resume", "interrupt", "cancel", "fail", "complete", "retry_step", "skip_step",
                                                   "fail_step", "reopen", "handoff", "assign", "note"]})
        self.events.emit("task", t, {"action": action, "task_id": task_id, "step_id": step_id, "robot_id": robot_id, "note": note})
        self.tasks.save()
        return task

    @staticmethod
    def _need(value: Any, action: str) -> None:
        if not value:
            raise InvalidRequest(f"action '{action}' needs more arguments")

    def _handoff(self, task: ContinuityState, step_id: str | None, to_robot: str, *, force: bool) -> list[str]:
        target = self.fleet.get(to_robot)
        from_robot = task.step(step_id).assigned_robot if step_id else task.assigned_robot
        if from_robot == to_robot:
            raise Conflict(f"work is already assigned to {to_robot}")
        group = task.step(step_id).group if step_id else None
        active = [s for s in task.steps if s.status not in DONE_STEP_STATUSES and s.status != StepStatus.CANCELLED]
        if step_id:
            steps = [s for s in active if s.step_id == step_id or (group and s.group == group)]
        else:
            steps = [s for s in active if s.assigned_robot == from_robot]
        unable = [s.step_id for s in steps if not target.can(s.skill, s.requires)]
        if unable:
            raise Conflict(f"{to_robot} cannot perform {unable}")
        holding = bool(from_robot) and from_robot in task.held_objects
        if holding and not force:
            raise Conflict(
                f"{from_robot} still holds {task.held_objects[from_robot]} for this task; wait for it to resume or put the object down "
                "first (force=true restarts the affected work items on the new robot)"
            )
        if holding:
            # The object stays in the old robot's gripper: restart every affected work item from its first step.
            groups = {group} if step_id else {s.group for s in steps}
            redo = [s.step_id for s in task.steps if s.group is not None and s.group in groups]
            if redo:
                self.tasks.reopen(task, redo, t=self.now(), note=f"work items {sorted(g for g in groups if g)} restarted on {to_robot}")
            held = task.held_objects.pop(from_robot)
            task.notes.append(f"{from_robot} still holds {held}; put it down when it is available again")
        moved = self.tasks.handoff(task, from_robot, to_robot, t=self.now(), step_id=step_id)
        if holding and not step_id:
            # reopened steps of the affected groups may have been assigned to the old robot
            for s in task.steps:
                if s.group in {x.group for x in steps} and s.status not in DONE_STEP_STATUSES and s.assigned_robot != to_robot:
                    s.assigned_robot = to_robot
                    moved.append(s.step_id)
            self.tasks.refresh(task)
        return moved

    def dispatch(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Assign ready, unassigned steps to capable robots (earliest-available first)."""
        self.counters["dispatch"] += 1
        tasks = [self.tasks.get(task_id)] if task_id else self.tasks.list([TaskStatus.ACTIVE])
        busy = self._busy_robots()
        out = []
        for task in tasks:
            for step in self.tasks.ready_steps(task):
                if step.assigned_robot:
                    continue
                robot = self._choose_robot(task, step, busy)
                if robot is None:
                    continue
                self.tasks.assign(task, step.step_id, robot.robot_id, t=self.now())
                busy.add(robot.robot_id)
                out.append({"task_id": task.task_id, "step_id": step.step_id, "robot_id": robot.robot_id})
        if out:
            self.tasks.save()
        return out

    def _busy_robots(self) -> set[str]:
        busy = set()
        for task in self.tasks.list([TaskStatus.ACTIVE, TaskStatus.INTERRUPTED, TaskStatus.PAUSED]):
            for s in task.steps:
                if s.assigned_robot and s.status not in DONE_STEP_STATUSES and s.status != StepStatus.CANCELLED:
                    busy.add(s.assigned_robot)
            busy.update(task.held_objects)
        return busy

    def _choose_robot(self, task: ContinuityState, step: StepState, busy: set[str]) -> RobotInstance | None:
        if step.group:
            for s in task.steps:
                if s.group == step.group and s.assigned_robot:
                    robot = self.fleet.get(s.assigned_robot)
                    return None if robot.status in ("stopped", "offline") else robot
        capable = self.fleet.capable(step.skill, step.requires)
        if not capable:
            return None
        dom = self.domain(task.domain) if task.domain else None
        prefer: list[str] = []
        for robot in capable[:1]:
            ctx = self.builder.context(dom, step, robot, self._safe_args(task, step))
            for e in self.memory.long_term.relevant(ctx, kinds=["coordination"]):
                feats = e.patch.get("prefer_features", [])
                prefer += [f for f in feats if isinstance(f, str)] if isinstance(feats, list) else []
        free = [r for r in capable if r.robot_id not in busy] or capable
        return min(free, key=lambda r: (not set(prefer) <= set(r.features), r.adapter.now(r), r.robot_id))

    def pick_idle_robot(self, skill: str, *, location: str | None = None, requires: Iterable[str] = (), allow_busy: bool = False) -> str:
        capable = self.fleet.capable(skill, requires)
        if not capable:
            raise NotFound(f"no robot can perform '{skill}'")
        busy = self._busy_robots()
        free = [r for r in capable if r.robot_id not in busy]
        if not free:
            if not allow_busy:
                raise Conflict(f"every robot that can perform '{skill}' is busy", data={"busy": sorted(busy)})
            free = capable
        return min(free, key=lambda r: (r.adapter.now(r), r.robot_id)).robot_id

    # -------------------------------------------------------- guidance + feedback

    def observe(self, robot_id: str, camera: str = "head", location: str | None = None) -> Observation:
        robot = self.fleet.get(robot_id)
        self.counters["observe"] += 1
        obs = robot.adapter.observe(robot, camera=camera, location=location)
        self._last_obs[robot_id] = obs
        if self.config.record_observations:
            self.events.emit("observe", obs.t, {"robot_id": robot_id, "camera": camera, "location": location, "observation": obs.to_dict()})
        return obs

    def last_observation(self, robot_id: str) -> Observation | None:
        return self._last_obs.get(robot_id)

    def plan_guidance(self, task_id: str, step_id: str, robot_id: str | None = None, *, observation: Observation | None = None,
                      extra_patch: dict[str, Any] | None = None, use_memory: bool = True,
                      modalities: tuple[str, ...] = ALL_MODALITIES) -> GuidancePlan:
        task = self.tasks.get(task_id)
        step = task.step(step_id)
        rid = robot_id or step.assigned_robot or self.pick_idle_robot(step.skill, requires=step.requires, allow_busy=True)
        robot = self.fleet.get(rid)
        dom = self.domain(task.domain) if task.domain else None
        args = self._safe_args(task, step)
        obs = observation if observation is not None else self._last_obs.get(rid)
        return self.builder.plan(domain=dom, step=step, robot=robot, args=args, observation=obs, extra_patch=extra_patch,
                                 use_memory=use_memory, modalities=modalities)

    def suggest_guidance(self, task_id: str, step_id: str, robot_id: str | None = None, *, observe: bool = False,
                         extra_patch: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counters["suggest_guidance"] += 1
        task = self.tasks.get(task_id)
        step = task.step(step_id)
        rid = robot_id or step.assigned_robot or self.pick_idle_robot(step.skill, requires=step.requires, allow_busy=True)
        plan = self.plan_guidance(task_id, step_id, rid, extra_patch=extra_patch)
        if observe and plan.wants_observation:
            robot_loc = self.fleet.get(rid).adapter.state(self.fleet.get(rid)).get("location")
            if robot_loc == plan.args.get("location"):
                self.observe(rid, camera=plan.guidance.camera or "head")
                plan = self.plan_guidance(task_id, step_id, rid, extra_patch=extra_patch)
        out = plan.to_dict()
        out["robot_id"] = rid
        out["memory"] = [self.memory.long_term.entries[e].brief() for e in plan.applied_memory]
        return out

    def execute(self, req: ExecutionRequest, *, evaluate_criteria: bool | None = None) -> Feedback:
        self.counters["execute"] += 1
        robot = self.fleet.get(req.robot_id)
        req = copy.deepcopy(req)
        req.request_id = req.request_id or self.ids.make("req")
        task = self.tasks.get(req.task_id) if req.task_id else None
        step = task.step(req.step_id) if (task is not None and req.step_id) else None
        if req.step_id and task is None:
            raise InvalidRequest("step_id given without task_id")
        spec = robot.skill(req.skill)
        mode = req.mode or (spec.mode if spec else ExecutionMode.LANGUAGE)
        dom = self.domain(task.domain) if (task is not None and task.domain) else (self.domain(None) if len(self.domains) == 1 else None)

        decision = self.admission.check_request(req, robot, task, self._last_obs.get(robot.robot_id))
        if not decision.admitted:
            return self._rejected(req, robot, mode, decision, dom)

        if task is not None and step is not None:
            try:
                resolved = self.tasks.resolve_args(task, step)
            except Conflict as exc:
                return self._rejected(req, robot, mode, Decision(False, "reject", [str(exc)]), dom)
            req.args = {**resolved, **req.args}
            self.tasks.start(task, step.step_id, robot.robot_id, t=self.now(robot.robot_id))
        robot.status = "busy"
        t0 = self.now(robot.robot_id)
        fb = robot.adapter.execute(robot, req, mode)
        raw = fb.to_dict()  # what the robot reported, before the harness applies criteria
        robot.status = "idle" if robot.status == "busy" else robot.status
        fb.request_id = req.request_id
        fb.task_id, fb.step_id = req.task_id, req.step_id
        fb.policy_success = fb.success if fb.policy_success is None else fb.policy_success
        fb.applied_memory = list(req.memory)
        self._last_obs[robot.robot_id] = fb.observation

        evaluate = self.config.evaluate_criteria if evaluate_criteria is None else evaluate_criteria
        names = list(step.criteria) if step is not None else list(DEFAULT_CRITERIA.get(req.skill, []))
        names += [v for v in req.verify if v not in names]
        if evaluate and fb.success and names:
            close = bool(req.guidance.options.get("verify_close_range"))
            results = [self._check(robot, n, req.args, close) for n in names]
            fb.criteria = results
            failed = [c for c in results if not c.passed]
            if failed:
                fb.success = False
                fb.action_result.outcome = Outcome.FAILURE
                fb.failure_reason = FailureReason(
                    code=failure_code(failed[0].name, fb.observation.robot_state),
                    message=f"policy reported success but criterion '{failed[0].name}' did not pass",
                    evidence={"criterion": failed[0].name, **failed[0].evidence},
                )
                fb.progress = min(fb.progress, 0.8)

        record_tags = self._tags(dom, step, robot, req, fb)
        if task is not None and step is not None:
            self._update_continuity(task, step, robot, req, fb)
        self._update_held(task, robot, req, fb)
        if task is not None:
            fb.continuity = copy.deepcopy(task)
        code = fb.failure_reason.code if fb.failure_reason else None
        key = (req.task_id or "", req.step_id or f"{req.robot_id}:{req.skill}")
        if req.recovery:
            self._tried.setdefault(key, []).append(req.recovery)
        if not fb.success:
            hint = self._recovery_hint(dom, step, robot, req)
            options = recovery_lib.plan(code, req.skill, tried=self._tried.get(key, []), order_hint=hint)
            fb.suggestions = [f"{r.name}: {r.description}" for r in options[: self.config.max_suggestions]]

        attempt = step.attempts if step is not None else 1
        evidence = dict(fb.failure_reason.evidence) if fb.failure_reason else dict(fb.action_result.details)
        record = ExecutionRecord(
            record_id=self.ids.make("rec"),
            t=self.now(robot.robot_id),
            robot_id=robot.robot_id,
            robot_model=robot.model,
            skill=req.skill,
            success=fb.success,
            outcome=fb.action_result.outcome.value,
            domain=dom.name if dom else None,
            task_id=req.task_id,
            step_id=req.step_id,
            goal=task.goal if task else None,
            instruction=req.guidance.instruction,
            args=dict(req.args),
            guidance=req.guidance.to_dict(),
            failure_code=code,
            failure_message=fb.failure_reason.message if fb.failure_reason else None,
            evidence=evidence,
            criteria=[c.to_dict() for c in fb.criteria],
            tags=record_tags,
            attempt=attempt,
            recovery=req.recovery,
            applied_memory=list(req.memory),
            duration_s=round(self.now(robot.robot_id) - t0, 2),
        )
        self.memory.temporary.add(record)
        if task is not None and step is not None:
            self._detect_contradictions(task, step, req, fb)
        if req.memory:
            self.memory.long_term.record_use(req.memory, fb.success)
        self.events.emit("execute", self.now(robot.robot_id), {
            "robot_id": robot.robot_id,
            "key": _request_key(robot.robot_id, req),
            "skill": req.skill,
            "args": req.args,
            "request": req.to_dict(),
            "mode": mode.value,
            "feedback": fb.to_dict(),
            "raw_feedback": raw,
            "record_id": record.record_id,
        })
        if task is not None:
            self.tasks.save()
        return fb

    def _rejected(self, req: ExecutionRequest, robot: RobotInstance, mode: ExecutionMode, decision: Decision, dom: Domain | None) -> Feedback:
        self.counters["rejected"] += 1
        obs = self._last_obs.get(robot.robot_id) or Observation(robot_id=robot.robot_id, t=self.now(robot.robot_id))
        fb = Feedback(
            request_id=req.request_id or "",
            robot_id=robot.robot_id,
            success=False,
            observation=obs,
            action_result=ActionResult(skill=req.skill, mode=mode, outcome=Outcome.REJECTED),
            failure_reason=FailureReason(code="admission_rejected", message="; ".join(decision.reasons),
                                         evidence={"reasons": decision.reasons}),
            task_id=req.task_id,
            step_id=req.step_id,
        )
        if any("person is in the workspace" in r for r in decision.reasons):
            fb.suggestions = ["pause_on_intrusion: set guidance.options.pause_on_intrusion=true, or wait and observe again"]
        self.events.emit("reject", self.now(robot.robot_id), {"robot_id": robot.robot_id, "skill": req.skill, "reasons": decision.reasons})
        return fb

    def _check(self, robot: RobotInstance, name: str, args: dict[str, Any], close: bool) -> CriterionResult:
        self.counters["check"] += 1
        res = robot.adapter.check(robot, name, criterion_args(name, args), close_range=close)
        self.events.emit("check", self.now(robot.robot_id), {"robot_id": robot.robot_id, "criterion": name, "args": criterion_args(name, args),
                                                             "close_range": close, "result": res.to_dict()})
        return res

    def _tags(self, dom: Domain | None, step: StepState | None, robot: RobotInstance, req: ExecutionRequest, fb: Feedback) -> list[str]:
        st = step or StepState(step_id="adhoc", skill=req.skill, args=req.args)
        tags = set(self.builder.context(dom, st, robot, req.args)["tags"])
        tags.update(req.tags)
        tags.update(f for f in fb.observation.flags if f in ("person_in_workspace",))
        if fb.failure_reason and fb.failure_reason.evidence.get("occluder"):
            tags.add("occluded")
        return sorted(tags)

    def _recovery_hint(self, dom: Domain | None, step: StepState | None, robot: RobotInstance, req: ExecutionRequest) -> list[str]:
        st = step or StepState(step_id="adhoc", skill=req.skill, args=req.args)
        ctx = self.builder.context(dom, st, robot, req.args)
        hint: list[str] = []
        for e in self.memory.long_term.relevant(ctx, kinds=["recovery", "coordination"]):
            hint += [h for h in e.patch.get("recovery_order", []) if h not in hint]
        return hint

    def _update_continuity(self, task: ContinuityState, step: StepState, robot: RobotInstance, req: ExecutionRequest, fb: Feedback) -> None:
        t = self.now(robot.robot_id)
        result: dict[str, Any] = {}
        if fb.success:
            result = {k: v for k, v in fb.action_result.details.items() if k not in ("events",)}
            result["robot_id"] = robot.robot_id
            result["t_end"] = round(t, 2)
        code = fb.failure_reason.code if fb.failure_reason else None
        if code in ("service_unavailable",):
            self.tasks.finish(task, step.step_id, success=False, t=t, failure=code)
            self.tasks.interrupt(task, t=t, note=f"{robot.robot_id} service unavailable during {step.step_id}")
        else:
            self.tasks.finish(task, step.step_id, success=fb.success, t=t, result=result, failure=code)
        task.assigned_robot = robot.robot_id

    def _update_held(self, task: ContinuityState | None, robot: RobotInstance, req: ExecutionRequest, fb: Feedback) -> None:
        state = fb.observation.robot_state
        if "holding" not in state:
            return
        if state.get("holding"):
            if task is not None:
                self.tasks.set_held(task, robot.robot_id, state.get("held_label") or req.args.get("sku") or "object")
            return
        for t in self.tasks.list():  # an empty gripper is empty for every task
            if robot.robot_id in t.held_objects:
                self.tasks.set_held(t, robot.robot_id, None)

    def _detect_contradictions(self, task: ContinuityState, step: StepState, req: ExecutionRequest, fb: Feedback) -> None:
        code = fb.failure_reason.code if fb.failure_reason else None
        tmem = self.memory.temporary
        if code == "not_holding":
            for s in reversed(task.steps):
                if s.skill == "pick" and (s.group == step.group or not step.group) and s.status == StepStatus.SUCCEEDED:
                    recs = tmem.query(task_id=task.task_id, step_id=s.step_id, success=True)
                    if recs:
                        reason = ("object dropped after pick: gripper opened in transit"
                                  if fb.failure_reason and fb.failure_reason.evidence.get("gripper_opened_in_transit")
                                  else "pick reported success but the gripper was empty later")
                        tmem.mark_contradicted(recs[-1].record_id, reason)
                    break
        if req.skill == "pick" and code in ("wrong_object", "target_not_found"):
            loc_ref = step.args.get("location")
            if isinstance(loc_ref, str) and loc_ref.startswith("@"):
                sid = loc_ref[1:].split(".", 1)[0]
                recs = tmem.query(task_id=task.task_id, step_id=sid, success=True)
                if recs:
                    tmem.mark_contradicted(recs[-1].record_id, f"search result did not hold up at pick time ({code})")

    def verify(self, robot_id: str, criteria: list[str], args: dict[str, Any], *, close_range: bool = True) -> list[CriterionResult]:
        robot = self.fleet.get(robot_id)
        self.counters["verify"] += 1
        return [self._check(robot, n, args, close_range) for n in criteria]

    def recheck_step(self, task_id: str, step_id: str, *, robot_id: str | None = None, close_range: bool = True) -> dict[str, Any]:
        """Re-evaluate a step's criteria (by default at close range) and reconcile continuity.

        A failed step whose criteria now pass is marked succeeded (the earlier
        verdict was a false negative). A succeeded step whose criteria fail is
        marked failed and its success record contradicted.
        """
        task = self.tasks.get(task_id)
        step = task.step(step_id)
        rid = robot_id or step.assigned_robot
        if not rid:
            raise InvalidRequest(f"step {step_id} has no robot to verify with")
        args = self.tasks.resolve_args(task, step)
        results = self.verify(rid, step.criteria or DEFAULT_CRITERIA.get(step.skill, []), args, close_range=close_range)
        passed = all(r.passed for r in results)
        t = self.now(rid)
        changed = None
        if passed and step.status == StepStatus.FAILED:
            self.tasks.finish(task, step_id, success=True, t=t, result={"robot_id": rid, "rechecked": True})
            changed = "succeeded"
        elif not passed and step.status == StepStatus.SUCCEEDED:
            recs = self.memory.temporary.query(task_id=task_id, step_id=step_id, success=True)
            if recs:
                failed = next(r for r in results if not r.passed)
                self.memory.temporary.mark_contradicted(recs[-1].record_id, f"close-range check failed: {failed.name}")
            self.tasks.finish(task, step_id, success=False, t=t, failure="criteria_failed")
            changed = "failed"
        self.tasks.save()
        return {"step_id": step_id, "passed": passed, "changed": changed, "criteria": [r.to_dict() for r in results]}

    def verify_task(self, task_id: str, *, close_range: bool = True) -> list[dict[str, Any]]:
        """Close-range re-verification of completed steps with persistent effects."""
        task = self.tasks.get(task_id)
        out = []
        for step in task.steps:
            if step.status == StepStatus.SUCCEEDED and step.skill in PERSISTENT_EFFECT and step.assigned_robot:
                out.append(self.recheck_step(task_id, step.step_id, robot_id=step.assigned_robot, close_range=close_range))
        return out

    # -------------------------------------------------------------- safety

    def stop_robot(self, robot_id: str, reason: str = "operator stop") -> dict[str, Any]:
        """Operator stop: the robot halts and its tasks pause until someone resumes them explicitly."""
        robot = self.fleet.get(robot_id)
        robot.adapter.stop(robot)
        robot.status = "stopped"
        paused = []
        for task in self.tasks.list([TaskStatus.ACTIVE, TaskStatus.INTERRUPTED, TaskStatus.PAUSED]):
            if any(s.assigned_robot == robot_id and s.status not in DONE_STEP_STATUSES and s.status != StepStatus.CANCELLED
                   for s in task.steps) or robot_id in task.held_objects:
                self.tasks.interrupt(task, t=self.now(robot_id), note=f"{robot_id} stopped: {reason}", status=TaskStatus.PAUSED)
                paused.append(task.task_id)
        self.events.emit("stop", self.now(robot_id), {"robot_id": robot_id, "reason": reason, "paused_tasks": paused})
        self.tasks.save()
        return {"robot_id": robot_id, "status": "stopped", "paused_tasks": paused}

    def resume_robot(self, robot_id: str) -> dict[str, Any]:
        """Clear the stop. Paused tasks stay paused until resumed (eep_task_update action=resume)."""
        robot = self.fleet.get(robot_id)
        robot.adapter.resume(robot)
        robot.status = "idle"
        paused = [t.task_id for t in self.tasks.list([TaskStatus.PAUSED])
                  if any(s.assigned_robot == robot_id for s in t.steps) or robot_id in t.held_objects]
        self.events.emit("resume", self.now(robot_id), {"robot_id": robot_id})
        return {"robot_id": robot_id, "status": "idle", "paused_tasks": paused}

    # -------------------------------------------------------------- domain tools

    def call_tool(self, domain: str | None, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        dom = self.domain(domain)
        if dom is None:
            raise InvalidRequest("domain is required when several domains are loaded")
        if tool not in dom.tools:
            raise NotFound(f"domain '{dom.name}' has no tool '{tool}'", data={"tools": sorted(dom.tools)})
        self.counters["tool"] += 1
        out = dom.tools[tool].fn(self, dom, dict(args))
        self.events.emit("tool", self.now(), {"domain": dom.name, "tool": tool, "args": args})
        return out

    # -------------------------------------------------------------- memory

    def memory_search(self, *, query: str | None = None, domain: str | None = None, skill: str | None = None,
                      robot_model: str | None = None, tags: list[str] | None = None, sku: str | None = None) -> list[dict[str, Any]]:
        self.counters["memory"] += 1
        if query and not any([skill, robot_model, tags, sku]):
            return [e.brief() for e in self.memory.long_term.search(query)]
        ctx = {"domain": domain, "skill": skill, "robot_model": robot_model, "tags": tags or [], "sku": sku, "features": []}
        if robot_model:
            for r in self.fleet:
                if r.model == robot_model:
                    ctx["features"] = sorted(r.features)
                    break
        return [e.brief() for e in self.memory.long_term.relevant(ctx, limit=30)]

    def memory_note(self, text: str, *, task_id: str | None = None, step_id: str | None = None, robot_id: str | None = None,
                    tags: list[str] | None = None, kind: str = "note", success: bool = True) -> dict[str, Any]:
        self.counters["memory"] += 1
        task = self.tasks.get(task_id) if task_id else None
        robot = self.fleet.get(robot_id) if robot_id else None
        rec = ExecutionRecord(
            record_id=self.ids.make("rec"),
            t=self.now(robot_id) if robot_id else self.now(),
            robot_id=robot_id or "",
            robot_model=robot.model if robot else "",
            skill="note",
            success=success,
            outcome=kind,
            domain=task.domain if task else None,
            task_id=task_id,
            step_id=step_id,
            goal=task.goal if task else None,
            tags=list(tags or []),
            kind=kind,
            note=text,
        )
        self.memory.temporary.add(rec)
        return {"record_id": rec.record_id}

    def memory_propose(self, proposals: list[dict[str, Any]], *, source: str = "agent") -> list[dict[str, Any]]:
        self.counters["memory"] += 1
        out = []
        for p in proposals:
            proposal = Proposal.from_dict(p, source=source)
            decision = self.admission.admit(proposal, self.memory, t=self.now())
            out.append(dict(decision.to_dict(), text=proposal.text))
            self.events.emit("memory", self.now(), {"proposal": proposal.to_dict(), "decision": decision.to_dict()})
        return out

    def reflect(self, reflector: Reflector | None = None, *, dry_run: bool = False) -> ReflectionReport:
        self.counters["reflect"] += 1
        report = reflect(self.memory, self.admission, reflector or RuleReflector(min_support=self.admission.min_support),
                         t=self.now(), dry_run=dry_run)
        self.events.emit("reflect", self.now(), report.to_dict())
        return report

    def pending_records(self, limit: int = 200) -> list[dict[str, Any]]:
        return [r.brief() for r in self.memory.temporary.pending()[-limit:]]


def _request_key(robot_id: str, req: ExecutionRequest) -> str:
    from ..embodiment.adapters import request_key

    return request_key(robot_id, req)
