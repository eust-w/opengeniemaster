"""Robot instances, calibration and the fleet registry (embodiment layer)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..eep.errors import NotFound
from ..eep.types import RobotDescriptor, SkillSpec
from .adapters import ExecutionAdapter
from .profiles import PROFILES, RobotProfile


@dataclass
class Calibration:
    version: str = "factory"
    valid: bool = True
    calibrated_at: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RobotInstance:
    robot_id: str
    profile: RobotProfile
    adapter: ExecutionAdapter
    calibration: Calibration = field(default_factory=Calibration)
    status: str = "idle"  # idle | busy | paused | stopped | offline
    policy: dict[str, Any] = field(default_factory=dict)  # public policy card
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.profile.model

    @property
    def features(self) -> frozenset[str]:
        return self.profile.features

    @property
    def skills(self) -> dict[str, SkillSpec]:
        return self.profile.skills

    def skill(self, name: str) -> SkillSpec | None:
        return self.profile.skills.get(name)

    def can(self, skill: str, requires: Iterable[str] = ()) -> bool:
        return skill in self.profile.skills and set(requires) <= set(self.profile.features)

    def descriptor(self) -> RobotDescriptor:
        state = self.adapter.state(self)
        policy = dict(self.policy)
        if "trained_skus" in policy:
            policy["trained_skus"] = sorted(policy["trained_skus"])
        return RobotDescriptor(
            robot_id=self.robot_id,
            model=self.model,
            features=sorted(self.features),
            skills=[self.profile.skills[k] for k in sorted(self.profile.skills)],
            status=self.status,
            location=state.get("location"),
            holding=(state.get("held_label") or "object") if state.get("holding") else None,
            calibrated=self.calibration.valid,
            adapter=self.adapter.name,
            policy=policy,
        )


class Fleet:
    def __init__(self, robots: Iterable[RobotInstance] = ()) -> None:
        self._robots: dict[str, RobotInstance] = {}
        for r in robots:
            self.add(r)

    def add(self, robot: RobotInstance) -> RobotInstance:
        self._robots[robot.robot_id] = robot
        return robot

    def get(self, robot_id: str) -> RobotInstance:
        try:
            return self._robots[robot_id]
        except KeyError:
            raise NotFound(f"unknown robot '{robot_id}'", data={"robots": sorted(self._robots)}) from None

    def __contains__(self, robot_id: str) -> bool:
        return robot_id in self._robots

    def __iter__(self):
        return iter(self._robots[k] for k in sorted(self._robots))

    def __len__(self) -> int:
        return len(self._robots)

    def ids(self) -> list[str]:
        return sorted(self._robots)

    def capable(self, skill: str, requires: Iterable[str] = ()) -> list[RobotInstance]:
        req = list(requires)
        return [r for r in self if r.can(skill, req) and r.status not in ("offline", "stopped")]

    def describe(self) -> list[RobotDescriptor]:
        return [r.descriptor() for r in self]


def make_robot(robot_id: str, model: str, adapter: ExecutionAdapter, *, features: Iterable[str] | None = None,
               policy: dict[str, Any] | None = None) -> RobotInstance:
    try:
        profile = PROFILES[model]
    except KeyError:
        raise NotFound(f"unknown robot model '{model}'", data={"models": sorted(PROFILES)}) from None
    if features is not None:
        profile = profile.with_features(set(features))
    return RobotInstance(robot_id=robot_id, profile=profile, adapter=adapter, policy=dict(policy or {}))
