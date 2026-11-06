"""Execution adapters: how the harness reaches a robot's execution services.

* :class:`SimAdapter` – in-process semantic simulator.
* :class:`HttpAdapter` – a robot-side EEP execution service over HTTP/JSON
  (see :mod:`opengeniemaster.embodiment.service`). The transport is
  injectable so that tests and other stacks (gRPC, ROS 2 bridges) can reuse
  the same message handling.
* :class:`ReplayAdapter` – returns recorded responses for offline tests.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Callable

from .._util import stable_hash
from ..eep.types import (
    ActionResult,
    CriterionResult,
    ExecutionMode,
    ExecutionRequest,
    FailureReason,
    Feedback,
    Observation,
    Outcome,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..sim.world import SimWorld
    from .robots import RobotInstance

Transport = Callable[[str, str, "dict[str, Any] | None", float], "dict[str, Any]"]

log = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP status when the service answered with an error


class ExecutionAdapter:
    """Interface every execution backend implements."""

    name = "base"

    def execute(self, robot: "RobotInstance", req: ExecutionRequest, mode: ExecutionMode) -> Feedback:
        raise NotImplementedError

    def observe(self, robot: "RobotInstance", camera: str = "head", location: str | None = None) -> Observation:
        raise NotImplementedError

    def check(self, robot: "RobotInstance", criterion: str, args: dict[str, Any], close_range: bool = False) -> CriterionResult:
        raise NotImplementedError

    def state(self, robot: "RobotInstance") -> dict[str, Any]:
        return {}

    def now(self, robot: "RobotInstance") -> float:
        return float(self.state(robot).get("clock_s", 0.0))

    def wait(self, robot: "RobotInstance", seconds: float) -> None:
        req = ExecutionRequest(robot_id=robot.robot_id, skill="wait", args={"seconds": seconds})
        self.execute(robot, req, ExecutionMode.PRIMITIVE)

    def stop(self, robot: "RobotInstance") -> bool:
        """Halt the robot. Returns True when the robot side confirmed the stop."""
        return True

    def resume(self, robot: "RobotInstance") -> bool:
        """Clear a stop. Returns True when the robot side confirmed it."""
        return True

    def health(self, robot: "RobotInstance") -> dict[str, Any]:
        """Liveness/protocol probe used by ``ogm doctor``; ``ok`` is False when the robot cannot be reached."""
        return {"ok": True, "robot_id": robot.robot_id}


def error_feedback(req: ExecutionRequest, mode: ExecutionMode, code: str, message: str, t: float = 0.0) -> Feedback:
    return Feedback(
        request_id=req.request_id or "",
        robot_id=req.robot_id,
        success=False,
        observation=Observation(robot_id=req.robot_id, t=t),
        action_result=ActionResult(skill=req.skill, mode=mode, outcome=Outcome.ERROR),
        failure_reason=FailureReason(code=code, message=message, recoverable=True),
        task_id=req.task_id,
        step_id=req.step_id,
    )


class SimAdapter(ExecutionAdapter):
    name = "sim"

    def __init__(self, world: "SimWorld") -> None:
        self.world = world

    def execute(self, robot: "RobotInstance", req: ExecutionRequest, mode: ExecutionMode) -> Feedback:
        return self.world.execute(robot.robot_id, req, mode)

    def observe(self, robot: "RobotInstance", camera: str = "head", location: str | None = None) -> Observation:
        return self.world.observe(robot.robot_id, camera=camera, location=location)

    def check(self, robot: "RobotInstance", criterion: str, args: dict[str, Any], close_range: bool = False) -> CriterionResult:
        result, _ = self.world.check(robot.robot_id, criterion, args, close_range=close_range)
        return result

    def state(self, robot: "RobotInstance") -> dict[str, Any]:
        r = self.world.robot(robot.robot_id)
        return {
            "location": r.location,
            "holding": r.holding is not None,
            "clock_s": round(r.clock, 2),
            "stopped": r.stopped,
        }

    def now(self, robot: "RobotInstance") -> float:
        return self.world.robot(robot.robot_id).clock

    def wait(self, robot: "RobotInstance", seconds: float) -> None:
        self.world.wait(robot.robot_id, seconds)

    def stop(self, robot: "RobotInstance") -> bool:
        self.world.robot(robot.robot_id).stopped = True
        return True

    def resume(self, robot: "RobotInstance") -> bool:
        self.world.robot(robot.robot_id).stopped = False
        return True


def urllib_transport(method: str, url: str, body: dict[str, Any] | None, timeout: float, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise AdapterError(f"{method} {url} -> HTTP {exc.code}: {detail[:300]}", status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"{method} {url} failed: {exc}") from exc
    if not payload:
        return {}
    try:
        out = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{method} {url} returned malformed JSON: {exc}") from exc
    if not isinstance(out, dict):
        raise AdapterError(f"{method} {url} returned {type(out).__name__}, expected a JSON object")
    return out


class HttpAdapter(ExecutionAdapter):
    """Talks to a robot-side execution service implementing ``/eep/v0/*``.

    Idempotent calls (observe, check, state, health, stop, resume) are retried
    with exponential backoff on transport failures and 5xx answers; ``execute``
    is never retried because a physical action must not run twice. ``state``
    is cached for ``state_ttl_s`` seconds so that the harness clock does not
    cost one HTTP round trip per robot on every event.
    """

    name = "http"

    def __init__(self, base_url: str, *, timeout: float = 60.0, transport: Transport | None = None, token: str | None = None,
                 retries: int = 2, backoff_s: float = 0.25, state_ttl_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.backoff_s = max(0.0, float(backoff_s))
        self.state_ttl_s = max(0.0, float(state_ttl_s))
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._transport = transport or (lambda m, u, b, t: urllib_transport(m, u, b, t, headers))
        self._sleep = sleep
        self._clock = clock
        self._last_t = 0.0
        self._state_cache: tuple[float, dict[str, Any]] | None = None

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, *, idempotent: bool = False) -> dict[str, Any]:
        attempts = 1 + (self.retries if idempotent else 0)
        for attempt in range(attempts):
            try:
                return self._transport(method, f"{self.base_url}{path}", body, self.timeout)
            except AdapterError as exc:
                status = _status_of(exc)
                retryable = status is None or status >= 500
                if not retryable or attempt + 1 >= attempts:
                    raise
                delay = self.backoff_s * (2 ** attempt)
                log.warning("%s %s failed (%s); retry %d/%d in %.2fs", method, path, exc, attempt + 1, self.retries, delay)
                if delay:
                    self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _invalidate(self) -> None:
        self._state_cache = None

    def execute(self, robot: "RobotInstance", req: ExecutionRequest, mode: ExecutionMode) -> Feedback:
        self._invalidate()
        try:
            out = self._call("POST", "/eep/v0/execute", {"request": req.to_dict(), "mode": mode.value})
            fb = Feedback.from_dict(out["feedback"])
        except AdapterError as exc:
            status = _status_of(exc)
            if status is not None and 400 <= status < 500:
                # The service is up but refused this request (bad token, malformed request, wrong robot).
                fb = error_feedback(req, mode, "precondition_failed", str(exc), self._last_t)
                fb.action_result.outcome = Outcome.REJECTED
                assert fb.failure_reason is not None
                fb.failure_reason.recoverable = False
                fb.failure_reason.evidence = {"http_status": status}
                return fb
            return error_feedback(req, mode, "service_unavailable", str(exc), self._last_t)
        except (KeyError, ValueError, TypeError) as exc:
            return error_feedback(req, mode, "service_unavailable", f"malformed response: {exc}", self._last_t)
        self._last_t = max(self._last_t, fb.observation.t)
        return fb

    def observe(self, robot: "RobotInstance", camera: str = "head", location: str | None = None) -> Observation:
        try:
            out = self._call("POST", "/eep/v0/observe", {"camera": camera, "location": location}, idempotent=True)
            obs = Observation.from_dict(out["observation"])
        except (AdapterError, KeyError, ValueError, TypeError) as exc:
            return Observation(robot_id=robot.robot_id, t=self._last_t, camera=camera, flags=["service_unavailable"],
                               robot_state={"error": str(exc)})
        self._last_t = max(self._last_t, obs.t)
        return obs

    def check(self, robot: "RobotInstance", criterion: str, args: dict[str, Any], close_range: bool = False) -> CriterionResult:
        try:
            out = self._call("POST", "/eep/v0/check", {"criterion": criterion, "args": args, "close_range": close_range}, idempotent=True)
            return CriterionResult.from_dict(out["result"])
        except (AdapterError, KeyError, ValueError, TypeError) as exc:
            return CriterionResult(name=criterion, passed=False, confidence=0.0, evidence={"error": str(exc)})

    def state(self, robot: "RobotInstance") -> dict[str, Any]:
        now = self._clock()
        if self._state_cache is not None and now - self._state_cache[0] < self.state_ttl_s:
            return dict(self._state_cache[1])
        try:
            state = self._call("GET", "/eep/v0/state", idempotent=True).get("state", {})
        except AdapterError as exc:
            return {"error": str(exc)}
        if not isinstance(state, dict):
            return {"error": f"malformed state: {type(state).__name__}"}
        self._state_cache = (now, state)
        return dict(state)

    def now(self, robot: "RobotInstance") -> float:
        try:
            t = float(self.state(robot).get("clock_s", self._last_t))
        except (TypeError, ValueError):
            t = self._last_t
        self._last_t = max(self._last_t, t)
        return self._last_t

    def health(self, robot: "RobotInstance") -> dict[str, Any]:
        try:
            out = self._call("GET", "/eep/v0/health", idempotent=True)
        except AdapterError as exc:
            return {"ok": False, "robot_id": robot.robot_id, "error": str(exc), "http_status": _status_of(exc)}
        return {"ok": bool(out.get("ok")), "robot_id": out.get("robot_id"), "protocol": out.get("protocol")}

    def stop(self, robot: "RobotInstance") -> bool:
        self._invalidate()
        try:
            self._call("POST", "/eep/v0/stop", {}, idempotent=True)
        except AdapterError as exc:
            log.error("safety stop of %s was NOT confirmed by %s: %s", robot.robot_id, self.base_url, exc)
            return False
        return True

    def resume(self, robot: "RobotInstance") -> bool:
        self._invalidate()
        try:
            self._call("POST", "/eep/v0/resume", {}, idempotent=True)
        except AdapterError as exc:
            log.error("resume of %s was not confirmed by %s: %s", robot.robot_id, self.base_url, exc)
            return False
        return True


def _status_of(exc: AdapterError) -> int | None:
    """HTTP status of a transport error; custom transports may only put it in the message."""
    if exc.status is not None:
        return exc.status
    m = re.search(r"\bHTTP (\d{3})\b", str(exc))
    return int(m.group(1)) if m else None


class ReplayMismatch(AdapterError):
    pass


def request_key(robot_id: str, req: ExecutionRequest) -> str:
    """Identity of a request for replay matching (ignores ids and timestamps)."""
    return stable_hash({"robot": robot_id, "skill": req.skill, "args": req.args, "guidance": req.guidance.to_dict()})


class ReplayAdapter(ExecutionAdapter):
    """Serves recorded responses in order, per robot.

    ``events`` are rows from an :class:`~opengeniemaster.harness.events.EventLog`.
    With ``strict=True`` a request that differs from the recording raises
    :class:`ReplayMismatch`, which is how offline regression tests detect
    behaviour changes in the harness or in an orchestrator.
    """

    name = "replay"

    def __init__(self, events: list[dict[str, Any]], *, strict: bool = True) -> None:
        self.strict = strict
        self._exec: dict[str, deque] = defaultdict(deque)
        self._obs: dict[str, deque] = defaultdict(deque)
        self._checks: dict[str, deque] = defaultdict(deque)
        self._clock: dict[str, float] = defaultdict(float)
        self._state: dict[str, dict[str, Any]] = defaultdict(dict)
        for ev in events:
            kind = ev.get("type")
            data = dict(ev.get("data", {}), _t=float(ev.get("t", 0.0)))
            rid = data.get("robot_id")
            if kind == "execute":
                self._exec[rid].append(data)
            elif kind == "observe":
                self._obs[rid].append(data)
            elif kind == "check":
                self._checks[rid].append(data)

    def remaining(self) -> int:
        return sum(len(q) for q in self._exec.values())

    def execute(self, robot: "RobotInstance", req: ExecutionRequest, mode: ExecutionMode) -> Feedback:
        queue = self._exec[robot.robot_id]
        if not queue:
            if self.strict:
                raise ReplayMismatch(f"no recorded execution left for {robot.robot_id} ({req.skill})")
            return error_feedback(req, mode, "service_unavailable", "replay exhausted", self._clock[robot.robot_id])
        row = queue.popleft()
        key = request_key(robot.robot_id, req)
        if self.strict and row.get("key") and row["key"] != key:
            raise ReplayMismatch(
                f"request differs from recording for {robot.robot_id}: recorded {row.get('skill')} {row.get('args')}, got {req.skill} {req.args}"
            )
        fb = Feedback.from_dict(row.get("raw_feedback") or row["feedback"])
        fb.request_id = req.request_id or fb.request_id
        fb.task_id, fb.step_id = req.task_id, req.step_id
        self._track(robot.robot_id, fb.observation, row["_t"])
        return fb

    def _track(self, robot_id: str, obs: Observation, t: float = 0.0) -> None:
        """Robot state as last reported by the recording (location, holding, clock)."""
        self._clock[robot_id] = max(self._clock[robot_id], obs.t, t)
        state = self._state[robot_id]
        if obs.location:
            state["location"] = obs.location
        if "holding" in obs.robot_state:
            state["holding"] = obs.robot_state["holding"]

    def observe(self, robot: "RobotInstance", camera: str = "head", location: str | None = None) -> Observation:
        queue = self._obs[robot.robot_id]
        if not queue:
            if self.strict:
                raise ReplayMismatch(f"no recorded observation left for {robot.robot_id}")
            return Observation(robot_id=robot.robot_id, t=self._clock[robot.robot_id], camera=camera)
        row = queue.popleft()
        obs = Observation.from_dict(row["observation"])
        self._track(robot.robot_id, obs, row["_t"])
        return obs

    def check(self, robot: "RobotInstance", criterion: str, args: dict[str, Any], close_range: bool = False) -> CriterionResult:
        queue = self._checks[robot.robot_id]
        if not queue:
            if self.strict:
                raise ReplayMismatch(f"no recorded check left for {robot.robot_id} ({criterion})")
            return CriterionResult(name=criterion, passed=False, confidence=0.0)
        row = queue.popleft()
        if self.strict and row.get("criterion") != criterion:
            raise ReplayMismatch(f"criterion differs from recording: recorded {row.get('criterion')}, got {criterion}")
        self._clock[robot.robot_id] = max(self._clock[robot.robot_id], row["_t"])
        return CriterionResult.from_dict(row["result"])

    def state(self, robot: "RobotInstance") -> dict[str, Any]:
        return dict(self._state[robot.robot_id], clock_s=self._clock[robot.robot_id])

    def now(self, robot: "RobotInstance") -> float:
        return self._clock[robot.robot_id]

    def wait(self, robot: "RobotInstance", seconds: float) -> None:
        self._clock[robot.robot_id] += seconds
