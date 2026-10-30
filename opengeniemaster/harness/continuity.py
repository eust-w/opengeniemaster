"""Continuity: persistent task state across invocations, robots and interruptions.

Physical work outlives a single tool call. The task store keeps, per task,
which steps are completed, which robot each step is assigned to, what each
robot is holding, which dependencies are still open and what work is
pending, so that after an interruption (or a robot swap) the next invocation
resumes at the next incomplete step instead of restarting the plan.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .._util import IdFactory, read_json, write_json_atomic
from ..eep.errors import Conflict, InvalidRequest, NotFound
from ..eep.types import (
    DONE_STEP_STATUSES,
    TERMINAL_TASK_STATUSES,
    ContinuityState,
    StepState,
    StepStatus,
    StepTemplate,
    TaskStatus,
)

_REF = re.compile(r"^@([A-Za-z0-9_.:-]+)\.([A-Za-z0-9_]+)$")
ACTIVE_STEP_STATUSES = frozenset({StepStatus.PENDING, StepStatus.READY, StepStatus.ASSIGNED, StepStatus.RUNNING, StepStatus.FAILED})


class TaskStore:
    def __init__(self, ids: IdFactory, path: Path | None = None) -> None:
        self.ids = ids
        self.path = path
        self.tasks: dict[str, ContinuityState] = {}
        if path is not None:
            for row in (read_json(path / "tasks.json", {}) or {}).get("tasks", []):
                t = ContinuityState.from_dict(row)
                self.tasks[t.task_id] = t

    def save(self) -> None:
        if self.path is not None:
            write_json_atomic(self.path / "tasks.json", {"tasks": [t.to_dict() for t in self.tasks.values()]})

    # ---------------------------------------------------------------- create

    def create(self, goal: str, steps: list[StepTemplate], *, t: float, domain: str | None = None, workflow: str | None = None,
               params: dict[str, Any] | None = None, priority: int = 0) -> ContinuityState:
        if not steps:
            raise InvalidRequest("a task needs at least one step")
        ids = [s.step_id for s in steps]
        if len(set(ids)) != len(ids):
            raise InvalidRequest(f"duplicate step ids in {ids}")
        for s in steps:
            for d in s.depends_on:
                if d not in ids:
                    raise InvalidRequest(f"step {s.step_id} depends on unknown step {d}")
        _check_acyclic(steps)
        _check_references(steps)
        task = ContinuityState(
            task_id=self.ids.make("task"),
            goal=goal,
            domain=domain,
            workflow=workflow,
            params=dict(params or {}),
            priority=priority,
            steps=[StepState(step_id=s.step_id, skill=s.skill, args=dict(s.args), instruction=s.instruction, depends_on=list(s.depends_on),
                             criteria=list(s.criteria), requires=list(s.requires), optional=s.optional, group=s.group)
                   for s in steps],
            created_at=t,
            updated_at=t,
        )
        self.tasks[task.task_id] = task
        self.refresh(task)
        return task

    # --------------------------------------------------------------- queries

    def get(self, task_id: str) -> ContinuityState:
        try:
            return self.tasks[task_id]
        except KeyError:
            raise NotFound(f"unknown task '{task_id}'") from None

    def list(self, status: Iterable[TaskStatus] | None = None) -> list[ContinuityState]:
        wanted = set(status) if status else None
        tasks = [t for t in self.tasks.values() if wanted is None or t.status in wanted]
        return sorted(tasks, key=lambda t: (-t.priority, t.created_at, t.task_id))

    def ready_steps(self, task: ContinuityState) -> list[StepState]:
        if task.status not in (TaskStatus.ACTIVE,):
            return []
        return [s for s in task.steps if s.status in (StepStatus.READY, StepStatus.ASSIGNED)]

    def failed_steps(self, task: ContinuityState) -> list[StepState]:
        return [s for s in task.steps if s.status == StepStatus.FAILED]

    def resolve_args(self, task: ContinuityState, step: StepState) -> dict[str, Any]:
        """Resolve ``"@<step_id>.<key>"`` references from earlier step results."""
        out = {}
        for k, v in step.args.items():
            if isinstance(v, str):
                m = _REF.match(v)
                if m:
                    try:
                        ref = task.step(m.group(1))
                    except KeyError:
                        raise Conflict(f"step {step.step_id} refers to unknown step '{m.group(1)}'") from None
                    if m.group(2) not in ref.result:
                        raise Conflict(f"step {step.step_id} needs {v}, but {ref.step_id} has no result '{m.group(2)}' yet")
                    v = ref.result[m.group(2)]
            out[k] = v
        return out

    # --------------------------------------------------------------- updates

    def refresh(self, task: ContinuityState) -> ContinuityState:
        done = {s.step_id for s in task.steps if s.status in DONE_STEP_STATUSES}
        for s in task.steps:
            if s.status in (StepStatus.PENDING, StepStatus.READY):
                s.status = StepStatus.READY if all(d in done for d in s.depends_on) else StepStatus.PENDING
        task.completed_steps = [s.step_id for s in task.steps if s.status == StepStatus.SUCCEEDED]
        task.pending_work = [s.step_id for s in task.steps if s.status in ACTIVE_STEP_STATUSES]
        task.dependencies = {
            s.step_id: [d for d in s.depends_on if d not in done]
            for s in task.steps
            if s.status in ACTIVE_STEP_STATUSES and any(d not in done for d in s.depends_on)
        }
        task.assignments = {s.step_id: s.assigned_robot for s in task.steps if s.assigned_robot and s.status in ACTIVE_STEP_STATUSES}
        if task.status not in TERMINAL_TASK_STATUSES:
            required = [s for s in task.steps if not s.optional]
            if required:
                complete = all(s.status in DONE_STEP_STATUSES for s in required)
            else:  # only optional steps: complete once every step has run its course
                complete = all(s.status in DONE_STEP_STATUSES or s.status in (StepStatus.FAILED, StepStatus.CANCELLED)
                               for s in task.steps)
            if complete:
                task.status = TaskStatus.COMPLETED
                task.pending_work = []
        return task

    def assign(self, task: ContinuityState, step_id: str, robot_id: str, *, t: float) -> StepState:
        step = task.step(step_id)
        if step.status in DONE_STEP_STATUSES:
            raise Conflict(f"step {step_id} is already {step.status.value}")
        targets = [step]
        if step.group:
            targets = [s for s in task.steps if s.group == step.group and s.status in ACTIVE_STEP_STATUSES]
        for s in targets:
            s.assigned_robot = robot_id
            if s.status == StepStatus.READY:
                s.status = StepStatus.ASSIGNED
        task.assigned_robot = robot_id
        task.updated_at = t
        self.refresh(task)
        return step

    def start(self, task: ContinuityState, step_id: str, robot_id: str, *, t: float) -> StepState:
        step = task.step(step_id)
        if step.assigned_robot != robot_id:
            self.assign(task, step_id, robot_id, t=t)
        step.status = StepStatus.RUNNING
        step.attempts += 1
        step.started_at = step.started_at if step.started_at is not None else t
        task.assigned_robot = robot_id
        task.updated_at = t
        if task.status in (TaskStatus.INTERRUPTED, TaskStatus.PAUSED):
            task.status = TaskStatus.ACTIVE
        return step

    def finish(self, task: ContinuityState, step_id: str, *, success: bool, t: float, result: dict[str, Any] | None = None,
               failure: str | None = None) -> StepState:
        step = task.step(step_id)
        step.status = StepStatus.SUCCEEDED if success else StepStatus.FAILED
        step.finished_at = t
        if result:
            step.result.update(result)
        step.last_failure = None if success else failure
        if not success and not step.optional and task.status == TaskStatus.COMPLETED:
            # e.g. a close-range re-check found the result wrong: the task is no longer done
            task.status = TaskStatus.ACTIVE
            task.notes.append(f"{step_id} failed after completion ({failure}); task reopened")
        task.updated_at = t
        self.refresh(task)
        return step

    def set_held(self, task: ContinuityState, robot_id: str, obj: str | None) -> None:
        if obj:
            task.held_objects[robot_id] = obj
        else:
            task.held_objects.pop(robot_id, None)

    def retry_step(self, task: ContinuityState, step_id: str, *, t: float) -> StepState:
        step = task.step(step_id)
        if step.status in DONE_STEP_STATUSES:
            step.status = StepStatus.PENDING
        elif step.status in (StepStatus.FAILED, StepStatus.RUNNING, StepStatus.CANCELLED):
            step.status = StepStatus.ASSIGNED if step.assigned_robot else StepStatus.PENDING
        if step.status == StepStatus.ASSIGNED and not all(task.step(d).status in DONE_STEP_STATUSES for d in step.depends_on):
            step.status = StepStatus.PENDING
        if task.status in (TaskStatus.FAILED, TaskStatus.COMPLETED):
            task.status = TaskStatus.ACTIVE
        task.updated_at = t
        self.refresh(task)
        return step

    def reopen(self, task: ContinuityState, step_ids: Iterable[str], *, t: float, note: str | None = None) -> None:
        ids = list(step_ids)
        unknown = [sid for sid in ids if not any(s.step_id == sid for s in task.steps)]
        if unknown:
            raise NotFound(f"task {task.task_id} has no steps {unknown}")
        for sid in ids:
            step = task.step(sid)
            step.status = StepStatus.PENDING
            step.result = {}
        if task.status in TERMINAL_TASK_STATUSES:
            task.status = TaskStatus.ACTIVE
        if note:
            task.notes.append(note)
        task.updated_at = t
        self.refresh(task)

    def skip_step(self, task: ContinuityState, step_id: str, *, t: float, note: str | None = None) -> StepState:
        step = task.step(step_id)
        step.status = StepStatus.SKIPPED
        step.finished_at = t
        if note:
            task.notes.append(f"{step_id}: {note}")
        task.updated_at = t
        self.refresh(task)
        return step

    def set_status(self, task: ContinuityState, status: TaskStatus, *, t: float, note: str | None = None) -> ContinuityState:
        task.status = status
        if status == TaskStatus.CANCELLED:
            for s in task.steps:
                if s.status in ACTIVE_STEP_STATUSES:
                    s.status = StepStatus.CANCELLED
        if note:
            task.notes.append(note)
        task.updated_at = t
        self.refresh(task)
        return task

    def interrupt(self, task: ContinuityState, *, t: float, note: str, status: TaskStatus = TaskStatus.INTERRUPTED) -> ContinuityState:
        """INTERRUPTED (e.g. a service outage) may be resumed by the orchestrator; PAUSED waits for an explicit resume."""
        for s in task.steps:
            if s.status == StepStatus.RUNNING:
                s.status = StepStatus.ASSIGNED if s.assigned_robot else StepStatus.READY
        return self.set_status(task, status, t=t, note=note)

    def handoff(self, task: ContinuityState, from_robot: str | None, to_robot: str, *, t: float, step_id: str | None = None) -> list[str]:
        """Move unfinished work of ``from_robot`` (or one step's work item) to ``to_robot``."""
        moved = []
        group = task.step(step_id).group if step_id else None
        for s in task.steps:
            if s.status not in ACTIVE_STEP_STATUSES:
                continue
            if step_id is not None:
                if not (s.step_id == step_id or (group and s.group == group)):
                    continue
            elif s.assigned_robot != from_robot:
                continue
            s.assigned_robot = to_robot
            if s.status in (StepStatus.RUNNING, StepStatus.FAILED):
                s.status = StepStatus.ASSIGNED
            moved.append(s.step_id)
        if moved:
            task.assigned_robot = to_robot
            task.notes.append(f"handoff {from_robot or '?'} -> {to_robot}: {', '.join(moved)}")
            if task.status == TaskStatus.INTERRUPTED:
                task.status = TaskStatus.ACTIVE
        task.updated_at = t
        self.refresh(task)
        return moved


def _check_references(steps: list[StepTemplate]) -> None:
    """``@step.key`` arguments must name a step this step (transitively) depends on."""
    deps = {s.step_id: set(s.depends_on) for s in steps}

    def ancestors(sid: str) -> set[str]:
        seen: set[str] = set()
        stack = list(deps[sid])
        while stack:
            d = stack.pop()
            if d not in seen:
                seen.add(d)
                stack.extend(deps.get(d, ()))
        return seen

    for s in steps:
        for key, value in s.args.items():
            if isinstance(value, str) and value.startswith("@"):
                m = _REF.match(value)
                if not m:
                    raise InvalidRequest(f"step {s.step_id} argument '{key}': malformed reference {value!r} (use '@<step_id>.<key>')")
                if m.group(1) not in ancestors(s.step_id):
                    raise InvalidRequest(f"step {s.step_id} argument '{key}' refers to '{m.group(1)}', which is not one of its dependencies")


def _check_acyclic(steps: list[StepTemplate]) -> None:
    deps = {s.step_id: set(s.depends_on) for s in steps}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(n: str, path: list[str]) -> None:
        if n in done:
            return
        if n in visiting:
            raise InvalidRequest(f"dependency cycle: {' -> '.join(path + [n])}")
        visiting.add(n)
        for d in deps[n]:
            visit(d, path + [n])
        visiting.discard(n)
        done.add(n)

    for sid in deps:
        visit(sid, [])
