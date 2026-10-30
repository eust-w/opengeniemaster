"""Human-in-the-loop robot backend (Wizard-of-Oz execution service).

Runs the robot-side EEP execution service, but every request is shown to a
human operator who performs it with a teleoperated or manually commanded
robot and reports the outcome. This lets you pilot the harness, memory and
reflection loop against the real world before a policy is wired in, and the
recorded episodes double as human-takeover data for the policy flywheel.

    python examples/human_backend.py --robot-id g2-01 --host 0.0.0.0 --port 8765 --token "$TOKEN"

Then point a fleet file at http://<host>:8765 and start ``ogm mcp --fleet``.
To integrate a real policy, copy this file and replace each ``input()``
exchange with calls to your VLA server, controllers and detectors.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opengeniemaster.eep.types import (  # noqa: E402
    ActionResult,
    CriterionResult,
    ExecutionMode,
    ExecutionRequest,
    FailureReason,
    Feedback,
    Observation,
    Outcome,
)
from opengeniemaster.embodiment.service import RobotBackend, RobotService, serve  # noqa: E402


class HumanBackend(RobotBackend):
    def __init__(self, robot_id: str, location: str = "dock", ask=input, say=print) -> None:
        self.robot_id = robot_id
        self.location = location
        self.holding: str | None = None
        self.t0 = time.time()
        self.ask = ask
        self.say = say

    def _now(self) -> float:
        return round(time.time() - self.t0, 2)

    def _state(self) -> dict[str, Any]:
        return {"location": self.location, "holding": self.holding is not None, "held_label": self.holding, "clock_s": self._now()}

    def execute(self, req: ExecutionRequest, mode: ExecutionMode) -> Feedback:
        start = time.time()
        self.say(f"\n=== {self.robot_id}: {req.skill} {json.dumps(req.args, ensure_ascii=False)} (mode={mode.value})")
        g = req.guidance
        self.say(f"instruction: {g.instruction}")
        for line in ([g.detailed] if g.detailed else []) + g.constraints:
            self.say(f"  - {line}")
        if g.references:
            self.say(f"  reference: {[r.ref_id or r.image for r in g.references]}")
        if g.spatial:
            self.say(f"  spatial: {[(c.kind, c.role, c.coords) for c in g.spatial]}")
        if g.options:
            self.say(f"  options: {g.options}")
        answer = self.ask("done? [y / n <failure_code> <message>]: ").strip()
        ok = answer.lower().startswith("y")
        failure = None
        if not ok:
            parts = answer.split(maxsplit=2)
            code = parts[1] if len(parts) > 1 else "incomplete"
            failure = FailureReason(code=code, message=parts[2] if len(parts) > 2 else "reported by operator")
        elif req.skill == "navigate_to":
            self.location = req.args.get("location", self.location)
        elif req.skill == "pick":
            self.holding = req.args.get("sku") or "object"
        elif req.skill in ("place", "hang", "handover", "spread"):
            self.holding = None
        return Feedback(
            request_id=req.request_id or "",
            robot_id=self.robot_id,
            success=ok,
            observation=Observation(robot_id=self.robot_id, t=self._now(), location=self.location, robot_state=self._state()),
            action_result=ActionResult(skill=req.skill, mode=mode, outcome=Outcome.SUCCESS if ok else Outcome.FAILURE,
                                       duration_s=round(time.time() - start, 2), details={"operator": True}),
            failure_reason=failure,
            progress=1.0 if ok else 0.0,
            task_id=req.task_id,
            step_id=req.step_id,
        )

    def observe(self, camera: str = "head", location: str | None = None) -> Observation:
        return Observation(robot_id=self.robot_id, t=self._now(), camera=camera, location=self.location, robot_state=self._state())

    def check(self, criterion: str, args: dict[str, Any], close_range: bool = False) -> CriterionResult:
        answer = self.ask(f"criterion {criterion} {json.dumps(args, ensure_ascii=False)} satisfied? [y/n]: ").strip().lower()
        return CriterionResult(name=criterion, passed=answer.startswith("y"), confidence=1.0, evidence={"operator": True})

    def state(self) -> dict[str, Any]:
        return self._state()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--robot-id", default="robot-01")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to accept connections from other machines")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default=None, help="bearer token the harness must send (strongly recommended off localhost)")
    args = p.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        print("warning: listening beyond localhost without --token; anyone on the network can command this robot",
              file=sys.stderr)
    serve(RobotService(HumanBackend(args.robot_id), token=args.token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
