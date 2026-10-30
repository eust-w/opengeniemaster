"""Guidance builder: turn records, observations and memory into policy context.

GenieMaster's agent-policy co-design means guidance only helps when the
policy was trained to use it. The builder therefore checks the robot's
features before adding object references or spatial cues, and it composes:

* object reference images from the merchant database (visual goal conditioning),
* target boxes from a fresh detection (spatial guidance),
* placement points from the planogram slot,
* detailed language and execution options from long-term memory and from
  the recovery being attempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..domain.records import slot_center
from ..eep.types import Guidance, ObjectReference, Observation, SpatialCue, StepState
from ..harness.recovery import SPREAD_DETAIL
from ..memory.store import MemoryEntry

if TYPE_CHECKING:  # pragma: no cover
    from ..domain.base import Domain
    from ..embodiment.robots import RobotInstance
    from ..memory.store import LongTermMemory

ALL_MODALITIES = ("language", "detailed", "reference", "box", "point")
KEEP_CLOSED_TEXT = "Keep the gripper closed during retraction and travel; release only at placement."
PAUSE_TEXT = "Pause if a person or hand enters the workspace, then recheck before resuming."


@dataclass
class GuidancePlan:
    guidance: Guidance
    args: dict[str, Any]
    applied_memory: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    patch: dict[str, Any] = field(default_factory=dict)
    wants_observation: bool = False
    verify_close_range: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guidance": self.guidance.to_dict(),
            "args": self.args,
            "applied_memory": self.applied_memory,
            "tags": self.tags,
            "wants_observation": self.wants_observation,
            "verify_close_range": self.verify_close_range,
            "notes": self.notes,
        }


def merge_patches(entries: list[MemoryEntry], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Least specific first so that more specific entries (and recoveries) win."""
    out: dict[str, Any] = {}
    for e in sorted(entries, key=lambda e: e.specificity):
        _merge(out, e.patch)
    if extra:
        _merge(out, extra)
    return out


def _merge(out: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if k in ("constraints", "prefer_features", "search_first", "recovery_order"):
            if not isinstance(v, list):
                continue
            cur = list(out.get(k, []))
            out[k] = cur + [x for x in v if isinstance(x, str) and x not in cur]
        elif k == "options":
            if isinstance(v, dict):
                out[k] = {**out.get(k, {}), **v}
        elif k == "detailed" and out.get(k) and v not in out[k]:
            out[k] = f"{out[k]} {v}"
        else:
            out[k] = v


class GuidanceBuilder:
    def __init__(self, memory: "LongTermMemory") -> None:
        self.memory = memory

    def context(self, domain: "Domain | None", step: StepState, robot: "RobotInstance", args: dict[str, Any]) -> dict[str, Any]:
        tags = set(domain.context_tags(args) if domain else [])
        sku = args.get("sku")
        trained = robot.policy.get("trained_skus")
        if sku and trained is not None and sku not in trained:
            tags.add("novel_for_policy")
        return {
            "domain": domain.name if domain else None,
            "skill": step.skill,
            "robot_model": robot.model,
            "features": sorted(robot.features),
            "tags": sorted(tags),
            "sku": sku,
        }

    def relevant(self, ctx: dict[str, Any]) -> list[MemoryEntry]:
        return self.memory.relevant(ctx)

    def plan(self, *, domain: "Domain | None", step: StepState, robot: "RobotInstance", args: dict[str, Any],
             observation: Observation | None = None, extra_patch: dict[str, Any] | None = None, use_memory: bool = True,
             modalities: tuple[str, ...] = ALL_MODALITIES) -> GuidancePlan:
        ctx = self.context(domain, step, robot, args)
        entries = self.relevant(ctx) if use_memory else []
        patch = merge_patches(entries, extra_patch)
        args = dict(args)
        records = domain.records if domain else None
        sku = args.get("sku")
        item = records.catalog.get(sku) if (records and isinstance(sku, str)) else None
        g = Guidance(instruction=step.instruction or f"{step.skill} {sku or ''}".strip())
        notes: list[str] = []
        features = robot.features

        # Visual goal conditioning: catalog reference image.
        if item is not None and step.skill in ("pick", "search") and "reference" in modalities:
            if "object_reference" in features and patch.get("use_reference", True):
                g.references.append(ObjectReference(ref_id=item.sku, image=item.image, description=item.name,
                                                    attributes=dict(item.attributes), source="merchant_database"))
                notes.append("added catalog reference image")
            elif patch.get("use_reference") and "object_reference" not in features:
                notes.append("reference requested but this robot's policy is not reference-conditioned")

        # Spatial guidance: target box from a fresh detection at the pick location.
        wants_obs = False
        needs_box = step.skill == "pick" and ("box" in modalities or "guided_policy" not in features)
        if needs_box:
            if observation is not None and observation.location == args.get("location", observation.location):
                dets = [d for d in observation.detections if sku and d.ref_id == sku]
                if dets:
                    best = max(dets, key=lambda d: d.confidence)
                    g.spatial.append(SpatialCue(kind="box", coords=list(best.bbox), frame=best.camera, role="target",
                                                label=item.name if item else str(sku)))
                    notes.append(f"target box from {best.camera} detection ({best.confidence:.2f})")
                else:
                    notes.append("no detection of the target in the latest observation")
            else:
                wants_obs = True

        # Placement point from the planogram slot.
        if step.skill in ("place", "hang") and "point" in modalities and patch.get("use_placement_point", True):
            slot = args.get("slot")
            place = records.place(args.get("location", "")) if records else None
            if slot and place is not None and slot in place.slots:
                g.spatial.append(SpatialCue(kind="point", coords=list(slot_center(place.slots, slot)), frame="head",
                                            role="placement", label=slot))
                notes.append(f"placement point for {slot}")

        if step.skill == "spread" and patch.get("corner_points"):
            g.spatial += [SpatialCue(kind="point", coords=[0.1, 0.2], role="waypoint", label="far-left corner"),
                          SpatialCue(kind="point", coords=[0.9, 0.2], role="waypoint", label="far-right corner")]

        # Detailed language and execution options.
        if "detailed" in modalities:
            if patch.get("detailed"):
                g.detailed = patch["detailed"]
            for c in patch.get("constraints", []):
                if c not in g.constraints:
                    g.constraints.append(c)
        if patch.get("camera"):
            g.camera = patch["camera"]
        if patch.get("arm"):
            g.arm = patch["arm"]
        if patch.get("approach"):
            g.approach = patch["approach"]
        if isinstance(patch.get("options"), dict):
            g.options.update(patch["options"])
        for flag in ("keep_gripper_closed", "pause_on_intrusion"):
            if flag in g.options and g.options[flag] is not True:
                del g.options[flag]  # memory can add safety behaviour, never remove it
        if patch.get("keep_gripper_closed"):
            g.options["keep_gripper_closed"] = True
            if "detailed" in modalities and KEEP_CLOSED_TEXT not in g.constraints:
                g.constraints.append(KEEP_CLOSED_TEXT)
        if patch.get("pause_on_intrusion"):
            g.options["pause_on_intrusion"] = True
            if "detailed" in modalities and PAUSE_TEXT not in g.constraints:
                g.constraints.append(PAUSE_TEXT)
        if patch.get("verify_close_range"):
            g.options["verify_close_range"] = True

        # Location hints for searches.
        if step.skill == "search" and patch.get("search_first"):
            first = [c for c in patch["search_first"]]
            cands = list(args.get("candidates", []))
            args["candidates"] = first + [c for c in cands if c not in first]
            notes.append(f"search {first} first (memory)")

        if step.skill == "spread" and g.detailed is None and "detailed" in modalities:
            g.detailed = "Use both arms to spread the cloth evenly over the table."
        if g.detailed == SPREAD_DETAIL:
            notes.append("explicit arm assignment and corner order")

        return GuidancePlan(
            guidance=g,
            args=args,
            applied_memory=[e.entry_id for e in entries],
            tags=ctx["tags"],
            patch=patch,
            wants_observation=wants_obs,
            verify_close_range=bool(patch.get("verify_close_range")),
            notes=notes,
        )
