"""Robot profiles: features and skill bindings of supported robot models.

The GenieMaster paper evaluates heterogeneous robots (AgiBot G1/G2 and ARX
arms). The built-in profiles here are generic stand-ins used by the simulator;
``analog_of`` documents which kind of platform each roughly corresponds to.
Real deployments register their own profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..eep.types import ExecutionMode, SkillSpec

# Robot features referenced by skills, workflows and memory entries.
FEATURES = {
    "mobile": "wheeled base that can navigate between locations",
    "dual_arm": "two manipulators (bimanual tasks, hold + look)",
    "head_camera": "head RGB camera",
    "wrist_camera": "wrist camera for close-range perception",
    "head_waist": "controllable head/waist for active perception",
    "arm_ik": "Cartesian arm control through inverse kinematics",
    "grasp_planning": "geometric grasp planner",
    "guided_policy": "action policy trained with spatial cues and detailed language",
    "object_reference": "action policy conditioned on object reference images",
}


def _skill(name: str, service: str, mode: ExecutionMode, description: str, params: dict, guidance: list[str]) -> SkillSpec:
    return SkillSpec(name=name, service=service, mode=mode, description=description, params=params, guidance=guidance)


_LOC = {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}
_PICK = {
    "type": "object",
    "properties": {
        "sku": {"type": "string", "description": "catalog id of the target"},
        "object": {"type": "string", "description": "tracked object id from an observation"},
        "location": {"type": "string"},
    },
}
_PLACE = {
    "type": "object",
    "properties": {"location": {"type": "string"}, "slot": {"type": "string"}, "sku": {"type": "string"}},
    "required": ["location"],
}


def skills_for(features: frozenset[str] | set[str]) -> dict[str, SkillSpec]:
    """Skill bindings implied by a feature set."""
    policy = "guided_policy" in features
    manip_mode = ExecutionMode.LANGUAGE if policy else ExecutionMode.ACTION
    manip_service = "policy" if policy else "control"
    guidance = ["language", "detailed", "box", "point"] + (["reference"] if "object_reference" in features else [])
    out = {
        "observe": _skill("observe", "perception", ExecutionMode.PRIMITIVE, "Capture detections from a camera at the current location.",
                          {"type": "object", "properties": {"camera": {"type": "string", "enum": ["head", "wrist"]}}}, []),
        "wait": _skill("wait", "primitive", ExecutionMode.PRIMITIVE, "Hold position for a number of seconds.",
                       {"type": "object", "properties": {"seconds": {"type": "number", "minimum": 0}}}, []),
        "pick": _skill("pick", manip_service, manip_mode, "Grasp the target object at the current location.", _PICK, guidance),
        "place": _skill("place", manip_service, manip_mode, "Release the held object at a location/slot.", _PLACE, ["language", "detailed", "point"]),
    }
    if "mobile" in features:
        out["navigate_to"] = _skill("navigate_to", "primitive", ExecutionMode.PRIMITIVE, "Drive to a named location.", _LOC, [])
        out["search"] = _skill(
            "search", "primitive", ExecutionMode.PRIMITIVE, "Visit candidate locations until the target is detected.",
            {"type": "object", "properties": {"sku": {"type": "string"}, "candidates": {"type": "array", "items": {"type": "string"}}},
             "required": ["sku", "candidates"]},
            ["reference", "detailed"],
        )
    if policy:
        out["hang"] = _skill("hang", "policy", ExecutionMode.LANGUAGE, "Hang the held item on a hook.", _PLACE, ["language", "detailed", "point"])
        out["handover"] = _skill("handover", "policy", ExecutionMode.LANGUAGE, "Hand the held object to a person at the location.",
                                 {"type": "object", "properties": {"location": {"type": "string"}, "recipient": {"type": "string"}}}, ["language"])
        out["wipe"] = _skill("wipe", "policy", ExecutionMode.LANGUAGE, "Wipe the surface at the location.", _LOC, ["language", "detailed"])
        if "dual_arm" in features:
            out["spread"] = _skill("spread", "policy", ExecutionMode.LANGUAGE, "Spread a cloth over the table with both arms.", _LOC,
                                   ["language", "detailed", "point"])
    return out


@dataclass(frozen=True)
class RobotProfile:
    model: str
    description: str
    features: frozenset[str]
    cameras: tuple[str, ...] = ("head", "wrist")
    arms: tuple[str, ...] = ("left", "right")
    analog_of: str = ""
    skills: dict[str, SkillSpec] = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self) -> None:
        if not self.skills:
            object.__setattr__(self, "skills", skills_for(self.features))

    def with_features(self, features: set[str] | frozenset[str], model: str | None = None) -> "RobotProfile":
        return RobotProfile(
            model=model or self.model,
            description=self.description,
            features=frozenset(features),
            cameras=self.cameras,
            arms=self.arms,
            analog_of=self.analog_of,
        )


PROFILES: dict[str, RobotProfile] = {
    "wheeled_humanoid": RobotProfile(
        model="wheeled_humanoid",
        description="Wheeled dual-arm humanoid with head, waist and wrist cameras.",
        features=frozenset({"mobile", "dual_arm", "head_camera", "wrist_camera", "head_waist", "arm_ik", "grasp_planning",
                            "guided_policy", "object_reference"}),
        analog_of="AgiBot Genie G1/G2-class wheeled humanoid",
    ),
    "dual_arm_cart": RobotProfile(
        model="dual_arm_cart",
        description="Mobile cart with two arms and wrist cameras; no active head/waist.",
        features=frozenset({"mobile", "dual_arm", "head_camera", "wrist_camera", "arm_ik", "grasp_planning", "guided_policy",
                            "object_reference"}),
        analog_of="ARX-style mobile dual-arm platform",
    ),
    "single_arm_mobile": RobotProfile(
        model="single_arm_mobile",
        description="Low-cost mobile manipulator with one arm; policy without reference conditioning.",
        features=frozenset({"mobile", "head_camera", "wrist_camera", "arm_ik", "grasp_planning", "guided_policy"}),
        arms=("right",),
        analog_of="generic single-arm mobile manipulator",
    ),
}

# Cumulative capability configurations (cf. the paper's C1..C5 study).
CAPABILITY_LADDER: list[tuple[str, frozenset[str]]] = [
    ("C1 arm IK", frozenset({"mobile", "head_camera", "wrist_camera", "dual_arm", "arm_ik"})),
    ("C2 + grasp planning", frozenset({"mobile", "head_camera", "wrist_camera", "dual_arm", "arm_ik", "grasp_planning"})),
    ("C3 + guidance-trained policy", frozenset({"mobile", "head_camera", "wrist_camera", "dual_arm", "arm_ik", "grasp_planning", "guided_policy"})),
    ("C4 + object references", frozenset({"mobile", "head_camera", "wrist_camera", "dual_arm", "arm_ik", "grasp_planning", "guided_policy",
                                          "object_reference"})),
    ("C5 + head/waist control", frozenset({"mobile", "head_camera", "wrist_camera", "dual_arm", "arm_ik", "grasp_planning", "guided_policy",
                                           "object_reference", "head_waist"})),
]
