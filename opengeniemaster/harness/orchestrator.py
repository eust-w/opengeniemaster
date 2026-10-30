"""Scripted orchestrator: a deterministic GenieMaster-style agent.

It drives the harness exactly the way an external coding agent would (through
the same EEP operations) but without a language model, so it can run in tests,
offline benchmarks and as a fallback on a robot. The same class, with
different configurations, also implements the baselines used in the
benchmark (vanilla end-to-end policy, dual-system planner + policy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..eep.types import (
    DONE_STEP_STATUSES,
    TERMINAL_TASK_STATUSES,
    ExecutionRequest,
    Feedback,
    Guidance,
    StepStatus,
    TaskStatus,
)
from . import recovery as recovery_lib
from .guidance import ALL_MODALITIES, GuidancePlan, merge_patches

if TYPE_CHECKING:  # pragma: no cover
    from ..embodiment.robots import RobotInstance
    from .harness import Harness

HOLD_SKILLS = ("place", "hang", "handover", "spread")


@dataclass
class OrchestratorConfig:
    name: str = "geniemaster"
    use_memory: bool = True
    modalities: tuple[str, ...] = ALL_MODALITIES
    max_attempts: int = 3
    recover: bool = True
    observe_before_pick: bool = True
    verify_final: bool = True
    multi_robot: bool = True
    trust_policy: bool = False  # skip completion criteria (baselines trust the policy's own signal)
    end_to_end: bool = False  # give the policy only the overall goal (vanilla VLA)
    continue_on_failure: bool = False  # skip a failed step and keep going
    max_restarts: int = 1
    max_iterations: int = 500


GENIEMASTER = OrchestratorConfig()
DUAL_SYSTEM = OrchestratorConfig(
    name="dual_system", use_memory=False, modalities=("language",), max_attempts=2, recover=False, observe_before_pick=False,
    verify_final=False, multi_robot=False, trust_policy=True, continue_on_failure=True,
)
VANILLA_VLA = OrchestratorConfig(
    name="vanilla_vla", use_memory=False, modalities=("language",), max_attempts=1, recover=False, observe_before_pick=False,
    verify_final=False, multi_robot=False, trust_policy=True, end_to_end=True, continue_on_failure=True,
)


class Advisor(Protocol):
    """Optional privileged hook (e.g. an oracle with ground truth)."""

    def adjust(self, harness: "Harness", task_id: str, step_id: str, robot: "RobotInstance", plan: GuidancePlan) -> None:
        ...


@dataclass
class RunResult:
    task_id: str
    status: str
    steps_succeeded: int
    steps_total: int
    attempts: int
    recoveries: list[str] = field(default_factory=list)
    tool_calls: int = 0
    makespan_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class ScriptedOrchestrator:
    def __init__(self, harness: "Harness", config: OrchestratorConfig | None = None, advisor: Advisor | None = None) -> None:
        self.h = harness
        self.cfg = config or GENIEMASTER
        self.advisor = advisor
        self._patches: dict[tuple[str, str], dict[str, Any]] = {}
        self._restarts: dict[tuple[str, str], int] = {}
        self.recoveries: list[str] = []
        self.notes: list[str] = []
        self._verified: set[str] = set()

    # ----------------------------------------------------------------- run

    def run(self, task_id: str) -> RunResult:
        h, cfg = self.h, self.cfg
        calls0 = sum(h.counters.values())
        t0 = {r.robot_id: h.now(r.robot_id) for r in h.fleet}
        attempts0 = sum(s.attempts for s in h.get_task(task_id).steps)
        if not cfg.multi_robot:
            self._assign_single_robot(task_id)
        self._drain(task_id)
        if cfg.verify_final and h.get_task(task_id).status == TaskStatus.COMPLETED:
            if self._final_verification(task_id):
                self._drain(task_id)
        task = h.get_task(task_id)
        makespan = max((h.now(r.robot_id) - t0.get(r.robot_id, 0.0) for r in h.fleet), default=0.0)
        return RunResult(
            task_id=task_id,
            status=task.status.value,
            steps_succeeded=sum(1 for s in task.steps if s.status == StepStatus.SUCCEEDED),
            steps_total=len(task.steps),
            attempts=sum(s.attempts for s in task.steps) - attempts0,
            recoveries=list(self.recoveries),
            tool_calls=sum(h.counters.values()) - calls0,
            makespan_s=round(makespan, 1),
            notes=list(self.notes) + list(task.notes),
        )

    def _drain(self, task_id: str) -> None:
        """Run ready steps (earliest-available robot first) until the task ends."""
        h, cfg = self.h, self.cfg
        for _ in range(cfg.max_iterations):
            task = h.get_task(task_id)
            if task.status in TERMINAL_TASK_STATUSES or task.status == TaskStatus.PAUSED:
                return  # paused work (e.g. an operator stop) waits for an explicit resume
            if task.status == TaskStatus.INTERRUPTED:
                h.update_task(task_id, "resume", note="orchestrator resumed")
                continue
            if cfg.multi_robot:
                h.dispatch(task_id)
            task = h.get_task(task_id)
            ready = [s for s in h.tasks.ready_steps(task) if s.assigned_robot]
            if not ready:
                note = ("no runnable steps left after failures" if h.tasks.failed_steps(task)
                        else "no capable robot for the remaining steps")
                h.update_task(task_id, "fail", note=note)
                return
            step = min(ready, key=lambda s: (h.now(s.assigned_robot), s.step_id))
            outcome = self._run_step(task_id, step.step_id)
            if outcome == "paused":
                return
            if outcome == "failed" and cfg.continue_on_failure:
                h.update_task(task_id, "skip_step", step_id=step.step_id, note="baseline continues after failure")
        h.update_task(task_id, "fail", note="iteration budget exhausted")

    def run_ready(self, until: float | None = None) -> bool:
        """Scheduler mode: run one ready step across all active tasks.

        Picks the highest-priority task first, then the robot that becomes
        free earliest (and before ``until`` if given). Returns False when
        nothing could run.
        """
        h = self.h
        for task in h.tasks.list([TaskStatus.INTERRUPTED]):
            h.update_task(task.task_id, "resume", note="scheduler resumed")
        h.dispatch()
        candidates = []
        for task in h.tasks.list([TaskStatus.ACTIVE]):
            ready = [s for s in h.tasks.ready_steps(task)
                     if s.assigned_robot and h.fleet.get(s.assigned_robot).status not in ("stopped", "offline")]
            if not ready and h.tasks.failed_steps(task) and not any(
                    s.status in (StepStatus.READY, StepStatus.ASSIGNED, StepStatus.RUNNING) for s in task.steps):
                h.update_task(task.task_id, "fail", note="no runnable steps left after failures")
                continue
            for s in ready:
                t = h.now(s.assigned_robot)
                if until is None or t < until:
                    candidates.append((-task.priority, t, task.created_at, task.task_id, s.step_id))
        if not candidates:
            return False
        _, _, _, task_id, step_id = min(candidates)
        outcome = self._run_step(task_id, step_id)
        task = h.get_task(task_id)
        if outcome == "failed" and self.cfg.continue_on_failure:
            h.update_task(task_id, "skip_step", step_id=step_id, note="continues after failure")
        elif task.status == TaskStatus.COMPLETED and self.cfg.verify_final and task_id not in self._verified:
            self._verified.add(task_id)
            self._final_verification(task_id)
        return True

    def _assign_single_robot(self, task_id: str) -> None:
        h = self.h
        task = h.get_task(task_id)
        for robot in h.fleet:
            if all(robot.can(s.skill, s.requires) for s in task.steps):
                for s in task.steps:
                    if s.status not in DONE_STEP_STATUSES and s.assigned_robot != robot.robot_id:
                        h.update_task(task_id, "assign", step_id=s.step_id, robot_id=robot.robot_id)
                return
        self.notes.append("no single robot can do every step; falling back to dispatch")
        h.dispatch(task_id)

    # ------------------------------------------------------------ one step

    def _run_step(self, task_id: str, step_id: str) -> str:
        h, cfg = self.h, self.cfg
        key = (task_id, step_id)
        tried: list[str] = []
        recovery_name: str | None = None
        args_override: dict[str, Any] = {}
        for _attempt in range(cfg.max_attempts):
            task = h.get_task(task_id)
            if task.status in TERMINAL_TASK_STATUSES:
                return "failed"
            if task.status == TaskStatus.PAUSED:
                return "paused"
            if task.status == TaskStatus.INTERRUPTED:
                h.update_task(task_id, "resume", note="resumed for retry")
            step = task.step(step_id)
            if step.status in DONE_STEP_STATUSES:
                return "ok"
            robot = h.fleet.get(step.assigned_robot)
            if step.skill == "pick" and h.get_task(task_id).held_objects.get(robot.robot_id):
                self._put_back(task_id, robot.robot_id)
            plan = self._plan(task_id, step_id, robot, args_override)
            fb = self._execute(task_id, step_id, robot, plan, recovery_name)
            if fb.success:
                return "ok"
            if (step.skill == "pick" and fb.failure_reason and fb.failure_reason.code == "wrong_object"
                    and fb.observation.robot_state.get("holding")):
                # Never carry a wrong object around: it may be another work item's target.
                self._put_back(task_id, robot.robot_id)
            if not cfg.recover:
                if fb.failure_reason and fb.failure_reason.code == "service_unavailable":
                    h.wait(robot.robot_id, float(fb.failure_reason.evidence.get("retry_after_s", 30.0)) + 1.0)
                continue
            code = fb.failure_reason.code if fb.failure_reason else "unknown"
            if code == "admission_rejected":
                if not self._handle_rejection(task_id, step_id, robot, fb):
                    # A rejected request changes nothing; record the failure so the scheduler cannot spin on it.
                    h.update_task(task_id, "fail_step", step_id=step_id, note=f"admission_rejected: {fb.failure_reason.message}")
                    return "failed"
                recovery_name = "pause_on_intrusion" if self._patches.get(key, {}).get("pause_on_intrusion") else recovery_name
                continue
            if self._needs_restart(task_id, step_id, fb):
                return "restart"
            hint = h._recovery_hint(h.domain(task.domain) if task.domain else None, step, robot,
                                    ExecutionRequest(robot_id=robot.robot_id, skill=step.skill, args=plan.args))
            options = [o for o in recovery_lib.plan(code, step.skill, tried=tried, order_hint=hint, limit=8)
                       if not _already_applied(o, plan, robot)]
            if not options:
                break
            choice = options[0]
            tried.append(choice.name)
            recovery_name = choice.name
            self.recoveries.append(f"{step_id}:{code}->{choice.name}")
            outcome = self._apply(task_id, step_id, robot, choice, fb, args_override)
            if outcome in ("ok", "restart", "failed"):
                return outcome
        task = h.get_task(task_id)
        step = task.step(step_id)
        return "ok" if step.status in DONE_STEP_STATUSES else "failed"

    def _plan(self, task_id: str, step_id: str, robot: "RobotInstance", args_override: dict[str, Any]) -> GuidancePlan:
        h, cfg = self.h, self.cfg
        task = h.get_task(task_id)
        step = task.step(step_id)
        patch = self._patches.get((task_id, step_id), {})
        obs = None
        plan = h.plan_guidance(task_id, step_id, robot.robot_id, extra_patch=patch, use_memory=cfg.use_memory, modalities=cfg.modalities)
        if args_override:
            plan.args.update(args_override)
        if step.skill == "pick" and (cfg.observe_before_pick or plan.patch.get("refresh_observation")) and ("box" in cfg.modalities or "guided_policy" not in robot.features):
            state = robot.adapter.state(robot)
            if state.get("location") == plan.args.get("location"):
                cam = plan.guidance.camera or "head"
                obs = h.observe(robot.robot_id, camera=cam)
                plan = h.plan_guidance(task_id, step_id, robot.robot_id, observation=obs, extra_patch=patch, use_memory=cfg.use_memory,
                                       modalities=cfg.modalities)
                if args_override:
                    plan.args.update(args_override)
        if self.advisor is not None:
            self.advisor.adjust(h, task_id, step_id, robot, plan)
        return plan

    def _execute(self, task_id: str, step_id: str, robot: "RobotInstance", plan: GuidancePlan, recovery_name: str | None) -> Feedback:
        cfg = self.cfg
        task = self.h.get_task(task_id)
        guidance = plan.guidance
        if cfg.end_to_end:
            guidance = Guidance(instruction=task.goal, options={"end_to_end": True})
        req = ExecutionRequest(
            robot_id=robot.robot_id,
            skill=task.step(step_id).skill,
            args=plan.args,
            guidance=guidance,
            task_id=task_id,
            step_id=step_id,
            recovery=recovery_name,
            memory=plan.applied_memory,
            tags=plan.tags,
        )
        return self.h.execute(req, evaluate_criteria=not cfg.trust_policy)

    # ------------------------------------------------------------ recoveries

    def _apply(self, task_id: str, step_id: str, robot: "RobotInstance", choice: recovery_lib.Recovery, fb: Feedback,
               args_override: dict[str, Any]) -> str:
        h = self.h
        key = (task_id, step_id)
        if choice.patch:
            self._patches[key] = merge_patches([], {**self._patches.get(key, {}), **choice.patch})
        action = choice.action
        if action == "recheck":
            res = h.recheck_step(task_id, step_id, robot_id=robot.robot_id)
            return "ok" if res["passed"] else "continue"
        if action == "wait":
            secs = float((fb.failure_reason.evidence if fb.failure_reason else {}).get("retry_after_s", 30.0))
            h.wait(robot.robot_id, secs + 1.0)
            h.update_task(task_id, "resume", note=f"resumed after waiting {secs:.0f}s for {robot.robot_id}")
            return "continue"
        if action == "handoff":
            return self._handoff(task_id, step_id, robot)
        if action == "put_back":
            self._put_back(task_id, robot.robot_id)
            return "continue"
        if action == "repick":
            return self._repick(task_id, step_id, robot, fb)
        if action == "reposition":
            return self._reposition(task_id, step_id, robot)
        if action == "search":
            return self._search_elsewhere(task_id, step_id, robot, args_override)
        return "continue"

    def _adhoc(self, task_id: str, robot_id: str, skill: str, args: dict[str, Any], guidance: Guidance | None = None,
               recovery: str | None = None) -> Feedback:
        req = ExecutionRequest(robot_id=robot_id, skill=skill, args=args, guidance=guidance or Guidance(instruction=f"{skill} {args}"),
                               task_id=task_id, recovery=recovery)
        return self.h.execute(req, evaluate_criteria=False)

    def _put_back(self, task_id: str, robot_id: str) -> None:
        robot = self.h.fleet.get(robot_id)
        loc = robot.adapter.state(robot).get("location")
        if loc:
            self._adhoc(task_id, robot_id, "place", {"location": loc}, Guidance(instruction="put the wrong item back where it was"),
                        recovery="put_back")

    def _handoff(self, task_id: str, step_id: str, robot: "RobotInstance") -> str:
        h = self.h
        task = h.get_task(task_id)
        step = task.step(step_id)
        busy = h._busy_robots() - {robot.robot_id}
        others = [r for r in h.fleet.capable(step.skill, step.requires) if r.robot_id != robot.robot_id and r.robot_id not in busy]
        if not others:
            return "continue"
        target = min(others, key=lambda r: (h.now(r.robot_id), r.robot_id))
        holding = robot.robot_id in task.held_objects
        try:
            h.update_task(task_id, "handoff", step_id=step_id, robot_id=target.robot_id, force=holding)
        except Exception as exc:  # conflict: keep trying on the same robot
            self.notes.append(f"handoff refused: {exc}")
            return "continue"
        return "restart" if holding else "continue"

    def _repick(self, task_id: str, step_id: str, robot: "RobotInstance", fb: Feedback) -> str:
        h = self.h
        evidence = fb.failure_reason.evidence if fb.failure_reason else {}
        dropped_at = evidence.get("dropped_at")
        task = h.get_task(task_id)
        step = task.step(step_id)
        args = h.tasks.resolve_args(task, step)
        sku = args.get("sku")
        here = robot.adapter.state(robot).get("location")
        if not dropped_at or not sku:
            return self._restart_item(task_id, step_id)
        patch = {**self._patches.get((task_id, step_id), {}), "keep_gripper_closed": True}
        if dropped_at != here:
            self._adhoc(task_id, robot.robot_id, "navigate_to", {"location": dropped_at}, recovery="keep_gripper_closed")
        obs = h.observe(robot.robot_id)
        pick_step = next((s for s in task.steps if s.skill == "pick" and s.group == step.group), None)
        if pick_step is None:
            return self._restart_item(task_id, step_id)
        plan = h.plan_guidance(task_id, pick_step.step_id, robot.robot_id, observation=obs, extra_patch=patch, use_memory=self.cfg.use_memory,
                               modalities=self.cfg.modalities)
        plan.args["location"] = dropped_at
        req = ExecutionRequest(robot_id=robot.robot_id, skill="pick", args=plan.args, guidance=plan.guidance, task_id=task_id,
                               recovery="keep_gripper_closed", memory=plan.applied_memory, tags=plan.tags)
        res = h.execute(req, evaluate_criteria=not self.cfg.trust_policy)
        if not res.success:
            return self._restart_item(task_id, step_id)
        if dropped_at != here:
            self._adhoc(task_id, robot.robot_id, "navigate_to", {"location": here}, recovery="keep_gripper_closed")
        return "continue"

    def _reposition(self, task_id: str, step_id: str, robot: "RobotInstance") -> str:
        h = self.h
        task = h.get_task(task_id)
        args = h.tasks.resolve_args(task, task.step(step_id))
        sku = args.get("sku")
        if not sku or task.held_objects.get(robot.robot_id):
            return "continue"
        obs = h.observe(robot.robot_id, camera="wrist")
        dets = [d for d in obs.detections if d.ref_id == sku]
        from ..eep.types import ObjectReference, SpatialCue

        g = Guidance(instruction=f"pick up the misplaced {sku} again", options={"keep_gripper_closed": True})
        if dets:
            g.spatial.append(SpatialCue(kind="box", coords=list(dets[0].bbox), frame=dets[0].camera, role="target"))
        g.references.append(ObjectReference(ref_id=sku))
        res = self._adhoc(task_id, robot.robot_id, "pick", {"sku": sku, "location": args.get("location")}, g, recovery="reposition")
        return "continue" if res.success else "failed"

    def _search_elsewhere(self, task_id: str, step_id: str, robot: "RobotInstance", args_override: dict[str, Any]) -> str:
        h = self.h
        task = h.get_task(task_id)
        step = task.step(step_id)
        args = h._safe_args(task, step)
        sku = args.get("sku")
        dom = h.domain(task.domain) if task.domain else None
        if not sku or dom is None or "search" not in robot.skills:
            return "continue"
        here = robot.adapter.state(robot).get("location")
        candidates = [p for p in dom.records.places if p != here]
        item = dom.records.catalog.get(sku)
        g = Guidance(instruction=f"find the {item.name if item else sku}", options={"verify_close_range": True})
        if item is not None and "object_reference" in robot.features:
            from ..eep.types import ObjectReference

            g.references.append(ObjectReference(ref_id=sku, image=item.image, description=item.name))
        res = self._adhoc(task_id, robot.robot_id, "search", {"sku": sku, "candidates": candidates}, g, recovery="search_elsewhere")
        if res.success and res.action_result.details.get("location"):
            args_override["location"] = res.action_result.details["location"]
        return "continue"

    def _needs_restart(self, task_id: str, step_id: str, fb: Feedback) -> bool:
        """The object for this work item is gone from the gripper with no known drop location."""
        task = self.h.get_task(task_id)
        step = task.step(step_id)
        code = fb.failure_reason.code if fb.failure_reason else None
        if step.skill == "pick" and code in ("target_not_found", "wrong_object"):
            loc = step.args.get("location")
            if isinstance(loc, str) and loc.startswith("@"):
                search_id = loc[1:].split(".", 1)[0]
                self._patches[(task_id, search_id)] = {**self._patches.get((task_id, search_id), {}), "verify_close_range": True,
                                                       "use_reference": True}
                return self._restart_item(task_id, step_id, from_step=search_id) == "restart"
        if step.skill in HOLD_SKILLS and code == "not_holding" and not (fb.failure_reason and fb.failure_reason.evidence.get("dropped_at")):
            return self._restart_item(task_id, step_id) == "restart"
        if step.skill == "handover" and code == "wrong_object" and step.status == StepStatus.FAILED and step.attempts >= 2:
            return self._restart_item(task_id, step_id) == "restart"
        return False

    def _restart_item(self, task_id: str, step_id: str, from_step: str | None = None) -> str:
        h = self.h
        task = h.get_task(task_id)
        step = task.step(step_id)
        if not step.group:
            return "continue"
        rkey = (task_id, step.group)
        if self._restarts.get(rkey, 0) >= self.cfg.max_restarts:
            return "continue"
        self._restarts[rkey] = self._restarts.get(rkey, 0) + 1
        group_steps = [s.step_id for s in task.steps if s.group == step.group]
        if from_step and from_step in group_steps:
            group_steps = group_steps[group_steps.index(from_step):]
        for sid in group_steps:
            if task.step(sid).skill == "pick":
                self._patches[(task_id, sid)] = {**self._patches.get((task_id, sid), {}), "use_reference": True, "verify_close_range": True,
                                                 "keep_gripper_closed": True}
        h.update_task(task_id, "reopen", step_id=",".join(group_steps), note=f"restart work item {step.group} from {group_steps[0]}")
        self.recoveries.append(f"{step_id}:restart->{group_steps[0]}")
        return "restart"

    def _handle_rejection(self, task_id: str, step_id: str, robot: "RobotInstance", fb: Feedback) -> bool:
        reasons = " ".join(fb.failure_reason.evidence.get("reasons", [])) if fb.failure_reason else ""
        key = (task_id, step_id)
        if "person is in the workspace" in reasons:
            self._patches[key] = {**self._patches.get(key, {}), "pause_on_intrusion": True}
            return True
        self.notes.append(f"{step_id} rejected: {reasons}")
        return False

    # --------------------------------------------------------- verification

    def _final_verification(self, task_id: str) -> bool:
        """Close-range re-check of persistent effects; returns True if work was reopened."""
        h = self.h
        results = h.verify_task(task_id)
        failed = [r for r in results if r["changed"] == "failed"]
        if not failed:
            return False
        self.notes.append(f"final verification failed for {[r['step_id'] for r in failed]}")
        reopened = False
        for r in failed:
            step = h.get_task(task_id).step(r["step_id"])
            if step.skill in ("place", "hang", "wipe", "spread"):
                reopened |= self._rework_in_place(task_id, step.step_id)
            else:
                reopened |= self._restart_item(task_id, step.step_id) == "restart"
        task = h.get_task(task_id)
        if not reopened and task.status not in TERMINAL_TASK_STATUSES:
            h.update_task(task_id, "fail", note="final verification failed and no rework is possible")
        return reopened

    def _rework_in_place(self, task_id: str, step_id: str) -> bool:
        """Go back to the step's location, pick a misplaced item up again if needed, and redo the step."""
        h = self.h
        task = h.get_task(task_id)
        step = task.step(step_id)
        robot = h.fleet.get(step.assigned_robot)
        args = h.tasks.resolve_args(task, step)
        loc = args.get("location")
        if loc and robot.adapter.state(robot).get("location") != loc:
            self._adhoc(task_id, robot.robot_id, "navigate_to", {"location": loc}, recovery="rework")
        if step.skill in ("place", "hang"):
            if self._reposition(task_id, step_id, robot) != "continue":
                return False
        key = (task_id, step_id)
        self._patches[key] = {**self._patches.get(key, {}), "verify_close_range": True, "camera": "wrist",
                              "use_placement_point": True}
        if step.skill == "wipe":
            self._patches[key].pop("camera", None)
            self._patches[key]["constraints"] = list(recovery_lib.RECOVERIES["mention_edges"].patch["constraints"])
        if step.skill == "spread":
            self._patches[key].pop("camera", None)
            self._patches[key].update(recovery_lib.RECOVERIES["add_arm_assignment"].patch)
        h.update_task(task_id, "retry_step", step_id=step_id, note="rework after final verification")
        self.recoveries.append(f"{step_id}:final_check->rework")
        return self._run_step(task_id, step_id) == "ok" or h.get_task(task_id).status not in TERMINAL_TASK_STATUSES


def _already_applied(option: recovery_lib.Recovery, plan: GuidancePlan, robot: "RobotInstance") -> bool:
    """True when a patch-only recovery would not change the next request."""
    if option.action is not None or not option.patch:
        return False
    g = plan.guidance
    if option.name == "add_reference":
        return bool(g.references) or "object_reference" not in robot.features
    if option.name == "add_target_box":
        return any(c.kind == "box" for c in g.spatial) and not option.patch.get("refresh_observation")
    if option.name == "use_wrist_camera":
        return g.camera == "wrist" or "wrist_camera" not in robot.features
    if option.name == "add_placement_point":
        return any(c.role == "placement" for c in g.spatial)
    return all(plan.patch.get(k) == v for k, v in option.patch.items())


def run_task(harness: "Harness", task_id: str, config: OrchestratorConfig | None = None, advisor: Advisor | None = None) -> RunResult:
    return ScriptedOrchestrator(harness, config, advisor).run(task_id)


__all__ = [
    "Advisor",
    "DUAL_SYSTEM",
    "GENIEMASTER",
    "OrchestratorConfig",
    "RunResult",
    "ScriptedOrchestrator",
    "VANILLA_VLA",
    "run_task",
]
