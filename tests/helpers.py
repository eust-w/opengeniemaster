"""Shared test fixtures."""

from __future__ import annotations

from opengeniemaster.eep.types import ExecutionRequest, Guidance
from opengeniemaster.runtime import harness_from_scenario
from opengeniemaster.sim.scenes import build


def sim(name: str = "restocking", seed: int = 0, **kwargs):
    memory = kwargs.pop("memory", None)
    admission = kwargs.pop("admission", None)
    scenario = build(name, seed, **kwargs)
    harness = harness_from_scenario(scenario, memory=memory, admission=admission, seed=seed)
    return harness, scenario


def create_scenario_task(harness, scenario):
    spec = scenario.tasks[0]
    return harness.create_task(spec.goal, domain=spec.domain, workflow=spec.workflow, params=spec.params)


def req(robot_id: str, skill: str, args: dict | None = None, **kwargs) -> ExecutionRequest:
    guidance = kwargs.pop("guidance", None) or Guidance(instruction=f"{skill} {args or {}}")
    return ExecutionRequest(robot_id=robot_id, skill=skill, args=dict(args or {}), guidance=guidance, **kwargs)
