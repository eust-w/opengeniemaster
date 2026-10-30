"""Methods compared in the simulation benchmark.

* ``vanilla_vla`` – one end-to-end policy given only the overall goal; no
  decomposition feedback, no verification, single robot.
* ``dual_system`` – a planner decomposes the task into language subtasks for
  the policy; trusts the policy's own success signal, retries once, single robot.
* ``geniemaster`` – this project's harness: multimodal guidance, criteria,
  recovery, continuity, multi-robot dispatch and long-term memory.
* ``geniemaster_cold`` – the same harness without long-term memory.
* ``oracle`` – the harness plus privileged ground truth from the simulator
  (an upper reference analogous to "human orchestration" in the paper).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from statistics import mean
from typing import TYPE_CHECKING, Any

from ..eep.types import ObjectReference, SpatialCue
from ..harness.admission import SharedAdmission
from ..harness.guidance import KEEP_CLOSED_TEXT, PAUSE_TEXT, GuidancePlan
from ..harness.orchestrator import DUAL_SYSTEM, GENIEMASTER, VANILLA_VLA, OrchestratorConfig, ScriptedOrchestrator
from ..harness.recovery import RECOVERIES, SPREAD_DETAIL
from ..memory.reflection import RuleReflector
from ..memory.store import MemoryStore
from ..runtime import harness_from_scenario
from ..sim.scenes import Scenario, build

if TYPE_CHECKING:  # pragma: no cover
    from ..embodiment.robots import RobotInstance
    from ..harness.harness import Harness


@dataclass(frozen=True)
class Method:
    name: str
    label: str
    config: OrchestratorConfig
    admission: dict[str, Any] = field(default_factory=dict)
    oracle: bool = False
    uses_memory: bool = False


METHODS: dict[str, Method] = {
    "vanilla_vla": Method("vanilla_vla", "Vanilla VLA", VANILLA_VLA, {"safety_interlock": False}),
    "dual_system": Method("dual_system", "Dual-system", DUAL_SYSTEM, {"safety_interlock": False}),
    "geniemaster_cold": Method("geniemaster_cold", "OpenGenieMaster (no memory)", replace(GENIEMASTER, name="geniemaster_cold", use_memory=False)),
    "geniemaster": Method("geniemaster", "OpenGenieMaster", GENIEMASTER, uses_memory=True),
    "oracle": Method("oracle", "Oracle orchestration", replace(GENIEMASTER, name="oracle", use_memory=False), oracle=True),
}


class OracleAdvisor:
    """Rewrites guidance with simulator ground truth (privileged upper reference)."""

    def __init__(self, scenario: Scenario) -> None:
        self.world = scenario.world

    def adjust(self, harness: "Harness", task_id: str, step_id: str, robot: "RobotInstance", plan: GuidancePlan) -> None:
        w = self.world
        step = harness.get_task(task_id).step(step_id)
        g = plan.guidance
        g.options["pause_on_intrusion"] = True
        if PAUSE_TEXT not in g.constraints:
            g.constraints.append(PAUSE_TEXT)
        g.options["verify_close_range"] = True
        sku = plan.args.get("sku")
        here = w.robot(robot.robot_id).location
        if step.skill == "pick" and sku:
            things = [t for t in w.things_at(plan.args.get("location") or here) if t.sku == sku]
            g.spatial = [c for c in g.spatial if c.role != "target"]
            if things:
                thing = things[0]
                g.spatial.append(SpatialCue(kind="box", coords=w._box(thing), frame="head", role="target"))
                if thing.vis_head < 0.7 and "wrist_camera" in robot.features:
                    g.camera = "wrist"
            if "object_reference" in robot.features and not g.references:
                g.references.append(ObjectReference(ref_id=sku))
            g.options["keep_gripper_closed"] = True
            if KEEP_CLOSED_TEXT not in g.constraints:
                g.constraints.append(KEEP_CLOSED_TEXT)
        elif step.skill == "hang":
            info = w.places[plan.args.get("location", here)].slot_info.get(plan.args.get("slot") or "", {})
            if info.get("occluder") and "wrist_camera" in robot.features:
                g.camera = "wrist"
        elif step.skill == "search" and sku:
            true_locs = sorted({t.location for t in w.things.values() if t.sku == sku and t.location in w.places})
            cands = list(plan.args.get("candidates", []))
            plan.args["candidates"] = true_locs + [c for c in cands if c not in true_locs]
        elif step.skill == "spread":
            g.detailed = SPREAD_DETAIL
            g.spatial += [SpatialCue(kind="point", coords=[0.1, 0.2], role="waypoint"), SpatialCue(kind="point", coords=[0.9, 0.2], role="waypoint")]
        elif step.skill == "wipe":
            for c in RECOVERIES["mention_edges"].patch["constraints"]:
                if c not in g.constraints:
                    g.constraints.append(c)


@dataclass
class TrialResult:
    task: str
    method: str
    seed: int
    success: bool
    completion: float
    makespan_s: float
    tool_calls: int
    attempts: int
    recoveries: int
    safety_violations: int
    detector_checks: int
    detector_errors: int
    status: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def run_trial(task: str, seed: int, method: Method, *, memory: MemoryStore | None = None, scenario_kwargs: dict[str, Any] | None = None,
              config: OrchestratorConfig | None = None) -> TrialResult:
    scenario = build(task, seed, **(scenario_kwargs or {}))
    harness = harness_from_scenario(scenario, memory=memory if method.uses_memory else None,
                                    admission=SharedAdmission(**method.admission), seed=seed)
    advisor = OracleAdvisor(scenario) if method.oracle else None
    results = []
    for spec in scenario.tasks:
        t = harness.create_task(spec.goal, domain=spec.domain, workflow=spec.workflow, params=spec.params, priority=spec.priority)
        results.append(ScriptedOrchestrator(harness, config or method.config, advisor).run(t.task_id))
    grade = scenario.grade()
    log = scenario.world.detector_log
    return TrialResult(
        task=task,
        method=method.name,
        seed=seed,
        success=grade.success,
        completion=round(grade.completion, 4),
        makespan_s=round(sum(r.makespan_s for r in results), 1),
        tool_calls=sum(r.tool_calls for r in results),
        attempts=sum(r.attempts for r in results),
        recoveries=sum(len(r.recoveries) for r in results),
        safety_violations=grade.safety_violations,
        detector_checks=len(log),
        detector_errors=sum(1 for d in log if d["passed"] != d["truth"]),
        status=",".join(r.status for r in results),
    )


def warm_up(task: str, method: Method, memory: MemoryStore, *, trials: int, seed0: int = 100_000,
            scenario_kwargs: dict[str, Any] | None = None, reflect_every: int = 1) -> list[TrialResult]:
    """Accumulate experience on held-out seeds, reflecting as the deployment would."""
    out = []
    admission = SharedAdmission(**method.admission)
    for i in range(trials):
        out.append(run_trial(task, seed0 + i, method, memory=memory, scenario_kwargs=scenario_kwargs))
        if reflect_every and (i + 1) % reflect_every == 0:
            from ..memory.reflection import reflect

            reflect(memory, admission, RuleReflector(min_support=admission.min_support))
    return out


def summarize(results: list[TrialResult]) -> dict[str, Any]:
    if not results:
        return {"n": 0}
    successes = [r for r in results if r.success]
    checks = sum(r.detector_checks for r in results)
    return {
        "n": len(results),
        "sr": round(100.0 * len(successes) / len(results), 2),
        "tc": round(100.0 * mean(r.completion for r in results), 2),
        "time_s": round(mean(r.makespan_s for r in results), 1),
        "calls_per_success": round(mean(r.tool_calls for r in successes), 1) if successes else None,
        "recoveries": round(mean(r.recoveries for r in results), 2),
        "safety_violations": sum(r.safety_violations for r in results),
        "detector_error_rate": round(sum(r.detector_errors for r in results) / checks, 4) if checks else None,
    }
