"""Replay-based offline tests ("Replay · Offline tests" in the architecture).

A run of the harness records every request that reached a robot and every
response. Replaying feeds the recorded responses back to a fresh harness with
the same orchestrator: if the orchestrator (or the guidance builder, admission,
recovery planner, ...) now issues a different request at any point, the
replay adapter reports the first divergence. This turns real deployment
logs into regression tests without robots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._util import IdFactory
from .domain import make_domain
from .embodiment.adapters import ReplayAdapter, ReplayMismatch
from .embodiment.robots import Fleet, make_robot
from .harness.events import EventLog
from .harness.harness import Harness
from .harness.orchestrator import GENIEMASTER, OrchestratorConfig, ScriptedOrchestrator
from .memory.store import MemoryEntry, MemoryStore
from .sim.scenes import build


@dataclass
class ReplayReport:
    ok: bool
    tasks: int
    executions_replayed: int
    executions_left: int
    error: str | None = None
    statuses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def replay(events: list[dict[str, Any]], *, scenario: str, seed: int = 0, memory_dir: str | Path | None = None,
           config: OrchestratorConfig = GENIEMASTER, strict: bool = True) -> ReplayReport:
    """Re-run the recorded tasks of a simulated scenario against recorded robot responses.

    ``memory_dir`` is read (not modified); pass the long-term memory that was in effect
    when the events were recorded, e.g. a copy taken before the run.
    """
    sc = build(scenario, seed)
    adapter = ReplayAdapter(events, strict=strict)
    fleet = Fleet()
    for robot_id, model in sc.fleet:
        fleet.add(make_robot(robot_id, model, adapter, features=sc.world.robots[robot_id].features, policy=sc.world.policy.public()))
    memory = MemoryStore()  # in-memory: replay must never write into the memory it reads
    if memory_dir:
        source = MemoryStore(memory_dir)
        memory.long_term.entries = {k: MemoryEntry.from_dict(e.to_dict()) for k, e in source.long_term.entries.items()}
        memory.long_term.version = source.long_term.version
    harness = Harness(domains=[make_domain(sc.domain, sc.records)], fleet=fleet, memory=memory, ids=IdFactory(seed=seed),
                      events=EventLog())
    creates = [e["data"] for e in events if e.get("type") == "task" and e["data"].get("action") == "create"]
    total = adapter.remaining()
    statuses = []
    try:
        for spec in creates:
            task = harness.create_task(spec["task"]["goal"], domain=spec.get("domain"), workflow=spec.get("workflow"),
                                       params=spec.get("params"), steps=spec.get("steps"), priority=spec["task"].get("priority", 0))
            result = ScriptedOrchestrator(harness, config).run(task.task_id)
            statuses.append(result.status)
    except ReplayMismatch as exc:
        return ReplayReport(False, len(statuses), total - adapter.remaining(), adapter.remaining(), str(exc), statuses)
    left = adapter.remaining()
    ok = left == 0 or not strict
    err = None if ok else f"{left} recorded executions were never requested"
    return ReplayReport(ok, len(statuses), total - left, left, err, statuses)
