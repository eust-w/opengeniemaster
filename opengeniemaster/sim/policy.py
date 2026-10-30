"""Parametric assumptions of the semantic simulator.

The simulator does not model physics. Each skill succeeds or fails according
to the probabilities below, which depend on observable properties of the
request: which guidance modalities were provided, which camera was used,
whether look-alike or never-trained products are involved, and which robot
features are present. These numbers are design assumptions chosen to make
the failure mechanisms described for GenieMaster (occlusion, look-alike
products, novel SKUs, gripper release, people entering the workspace,
service outages) exist in simulation. They are not measurements and results
produced with them say nothing about physical robot performance.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..eep.types import Guidance, SpatialCue


@dataclass
class SimParams:
    # Navigation
    speed_mps: float = 0.6
    nav_overhead_s: float = 2.0
    nav_fail: float = 0.005
    # Perception
    head_observe_s: float = 1.0
    wrist_approach_s: float = 3.5
    det_confusion_head: float = 0.30  # look-alike mislabel rate, head camera
    det_confusion_wrist: float = 0.04  # look-alike mislabel rate, wrist camera close-up
    active_perception_vis: float = 0.88  # visibility after head/waist re-aim
    # Target identification by the policy
    id_box_guided: float = 0.97
    id_reference_max: float = 0.97
    id_language_seen: float = 0.93
    lookalike_language_factor: float = 0.55
    novel_text_max: float = 0.22
    box_localization_gain: float = 0.5  # fraction of localization failures removed by a box cue
    # Manipulation
    grasp_ik_only: float = 0.42
    grasp_planning: float = 0.62
    grasp_policy: float = 0.94
    small_target_factor: float = 0.93
    back_row_no_waist_factor: float = 0.55
    place_point_guided: float = 0.97
    place_language: float = 0.82
    release_drop: float = 0.01
    hang_base: float = 0.94
    handover: float = 0.98
    wipe_guided: float = 0.93
    wipe_language: float = 0.80
    edge_dirt_left: float = 0.35
    spread_base: float = 0.50
    spread_arm_assignment_bonus: float = 0.33
    spread_corner_points_bonus: float = 0.07
    intrusion_fail: float = 0.70
    # Long-horizon drift of an end-to-end policy given only the overall goal
    end_to_end_step_reliability: float = 0.90
    # Completion detectors
    det_fp_misplaced: float = 0.35
    det_fp_lookalike: float = 0.28
    det_fp_hard: float = 0.40  # hooks, edges, partial coverage seen from afar
    det_fp_other: float = 0.02
    det_fn: float = 0.04
    det_close_error: float = 0.03
    # Durations (seconds)
    pick_s: float = 9.0
    place_s: float = 7.0
    hang_s: float = 11.0
    handover_s: float = 5.0
    wipe_s: float = 18.0
    spread_s: float = 35.0
    verify_s: float = 4.0
    service_error_s: float = 2.0


@dataclass
class PolicyCard:
    """The deployed action policy. ``flaws`` are hidden from agents."""

    name: str = "sim-vla"
    trained_skus: set[str] = field(default_factory=set)
    training_scale: int = 1000
    flaws: dict[str, float] = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "name": self.name,
            "training_scale": self.training_scale,
            "trained_skus": sorted(self.trained_skus),
        }


def scale_factor(training_scale: int) -> float:
    """Assumed effect of training-set diversity on generalization (0..1)."""
    return min(1.0, max(0.0, math.log10(max(training_scale, 1)) / 3.0))


# ---------------------------------------------------------------------------
# What a guidance-trained policy understands from a Guidance message
# ---------------------------------------------------------------------------

_KEEP_CLOSED = re.compile(
    r"(keep|hold)\b[^.]*\bgripper\b[^.]*\b(closed|shut)|gripper\b[^.]*\b(closed|shut)\b[^.]*\b(until|during|while)",
    re.I,
)
_PAUSE = re.compile(
    r"\bpause\b[^.]*\b(person|people|human|hand|customer|guest)s?\b"
    r"|\b(person|people|human|hand)s?\b[^.]*\b(enter|enters|entering|appear|appears)\b[^.]*\b(pause|stop|wait)",
    re.I,
)
_WRIST = re.compile(r"\bwrist[- ]?(camera|cam|view)\b", re.I)
_CLOSE = re.compile(r"close[- ]range|up close|closer look|close distance|from close", re.I)
_LEFT_RIGHT = re.compile(r"\bleft\b[^.]*\bright\b|\bright\b[^.]*\bleft\b", re.I)
_CORNER = re.compile(r"\bcorners?\b", re.I)
_EDGE = re.compile(r"\bedges?\b|\bborders?\b", re.I)


def guidance_text(g: Guidance) -> str:
    return " ".join([g.instruction or "", g.detailed or "", *g.constraints])


def keep_gripper_closed(g: Guidance) -> bool:
    return bool(g.options.get("keep_gripper_closed")) or bool(_KEEP_CLOSED.search(guidance_text(g)))


def pause_on_intrusion(g: Guidance) -> bool:
    return bool(g.options.get("pause_on_intrusion")) or bool(_PAUSE.search(guidance_text(g)))


def wants_wrist_camera(g: Guidance) -> bool:
    if (g.camera or "").startswith("wrist") or str(g.options.get("camera", "")).startswith("wrist"):
        return True
    return bool(_WRIST.search(guidance_text(g)))


def verify_close_range(g: Guidance) -> bool:
    return bool(g.options.get("verify_close_range")) or bool(_CLOSE.search(guidance_text(g)))


def has_arm_assignment(g: Guidance) -> bool:
    text = guidance_text(g)
    return bool(_LEFT_RIGHT.search(text) and _CORNER.search(text))


def mentions_edges(g: Guidance) -> bool:
    return bool(_EDGE.search(guidance_text(g)))


def target_box(g: Guidance) -> SpatialCue | None:
    for cue in g.spatial:
        if cue.kind == "box" and cue.role == "target" and len(cue.coords) == 4:
            return cue
    return None


def placement_point(g: Guidance) -> SpatialCue | None:
    for cue in g.spatial:
        if cue.kind == "point" and cue.role == "placement" and len(cue.coords) >= 2:
            return cue
    return None


def corner_points(g: Guidance) -> list[SpatialCue]:
    return [c for c in g.spatial if c.kind == "point" and c.role in ("waypoint", "target") and len(c.coords) >= 2]


def reference_skus(g: Guidance) -> list[str]:
    out = []
    for ref in g.references:
        if ref.ref_id:
            out.append(ref.ref_id)
        elif ref.image and ref.image.startswith("sim://catalog/"):
            out.append(ref.image.rsplit("/", 1)[-1].split(".", 1)[0])
    return out
