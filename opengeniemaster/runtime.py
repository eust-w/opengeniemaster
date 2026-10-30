"""Wiring helpers: build a harness for a simulated scenario or a robot fleet file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._util import IdFactory
from .domain import make_domain
from .domain.records import BusinessRecords
from .embodiment.adapters import HttpAdapter, SimAdapter
from .embodiment.robots import Calibration, Fleet, make_robot
from .harness.admission import SharedAdmission
from .harness.events import EventLog
from .harness.harness import Harness, HarnessConfig
from .memory.store import MemoryStore
from .sim.scenes import Scenario, build


def harness_from_scenario(scenario: Scenario, *, memory: MemoryStore | None = None, admission: SharedAdmission | None = None,
                          state_dir: str | Path | None = None, seed: int | None = 0, events: EventLog | None = None,
                          config: HarnessConfig | None = None) -> Harness:
    adapter = SimAdapter(scenario.world)
    policy = scenario.world.policy.public()
    fleet = Fleet()
    for robot_id, model in scenario.fleet:
        sim_robot = scenario.world.robots[robot_id]
        fleet.add(make_robot(robot_id, model, adapter, features=sim_robot.features, policy=policy))
    domain = make_domain(scenario.domain, scenario.records)
    state = Path(state_dir) if state_dir is not None else None
    persistent = state is not None or (memory is not None and memory.path is not None)
    # Seeded ids keep in-memory runs reproducible; anything persisted gets a random session tag so runs never collide.
    ids = IdFactory(seed=seed) if seed is not None and not persistent else IdFactory()
    return Harness(
        domains=[domain],
        fleet=fleet,
        memory=memory if memory is not None else MemoryStore(state / "memory" if state else None),
        admission=admission,
        ids=ids,
        events=events,
        state_dir=state,
        config=config,
        persist_tasks=False,
    )


def harness_for_sim(name: str = "restocking", seed: int = 0, **kwargs: Any) -> tuple[Harness, Scenario]:
    state_dir = kwargs.pop("state_dir", None)
    memory = kwargs.pop("memory", None)
    scenario = build(name, seed, **kwargs)
    return harness_from_scenario(scenario, memory=memory, state_dir=state_dir, seed=seed), scenario


def harness_from_fleet_file(path: str | Path, *, state_dir: str | Path | None = None) -> Harness:
    """Build a harness for real robots described in a JSON fleet file.

    Example::

        {
          "domain": "store",
          "records": "records.json",
          "robots": [
            {"robot_id": "g2-01", "model": "wheeled_humanoid", "endpoint": "http://10.0.0.21:8765",
             "token_env": "G2_01_TOKEN", "calibration": {"version": "2026-10-20", "valid": true}}
          ]
        }
    """
    import os

    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(path).parent
    records_path = base / spec["records"] if isinstance(spec.get("records"), str) else None
    records = BusinessRecords.from_dict(json.loads(records_path.read_text(encoding="utf-8"))) if records_path else BusinessRecords()
    fleet = Fleet()
    for row in spec.get("robots", []):
        token = os.environ.get(row["token_env"]) if row.get("token_env") else None
        adapter = HttpAdapter(row["endpoint"], token=token, timeout=float(row.get("timeout_s", 60.0)))
        robot = make_robot(row["robot_id"], row["model"], adapter, features=row.get("features"), policy=row.get("policy"))
        cal = row.get("calibration") or {}
        robot.calibration = Calibration(version=str(cal.get("version", "factory")), valid=bool(cal.get("valid", True)),
                                        params=dict(cal.get("params", {})))
        fleet.add(robot)
    domains = [make_domain(spec.get("domain", "store"), records)]
    return Harness(domains=domains, fleet=fleet, state_dir=state_dir)
