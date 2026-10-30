"""Recovery vocabulary shared by the harness, orchestrators and reflection.

When execution feedback reports a failure, the harness suggests recoveries
from this playbook. A recovery either patches the guidance of the next
attempt (e.g. use the wrist camera) or performs an action first (wait for
the service, hand off, put back a wrong object, re-pick a dropped one).

Reflection later turns recoveries that repeatedly *worked* into long-term
instructions using each recovery's ``lesson`` template, which is how a fix
found by trial and error becomes the first thing tried next time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Recovery:
    name: str
    description: str
    patch: dict[str, Any] = field(default_factory=dict)
    action: str | None = None  # recheck | wait | handoff | put_back | repick | reposition | search
    scope: str = "domain"  # where a consolidated lesson belongs
    kind: str = "guidance"
    lesson: str = ""  # long-term instruction template: {skill}, {context}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.patch:
            out["patch"] = dict(self.patch)
        if self.action:
            out["action"] = self.action
        return out


SPREAD_DETAIL = (
    "Left arm takes the far-left corner and right arm the far-right corner; pull both corners across together, "
    "then smooth the edges toward you."
)

RECOVERIES: dict[str, Recovery] = {
    r.name: r
    for r in [
        Recovery("retry", "Retry the same step unchanged."),
        Recovery("recheck", "Re-run the completion check at close range before acting again.", action="recheck",
                 patch={"verify_close_range": True}),
        Recovery("use_wrist_camera", "Approach and localize the target with the wrist camera at close range.",
                 patch={"camera": "wrist"}, scope="embodiment",
                 lesson="For {skill}{context}, the head camera cannot localize the target reliably; approach and localize with the wrist camera first."),
        Recovery("add_target_box", "Observe again and give the policy a target box from the fresh detection.",
                 patch={"use_box": True, "refresh_observation": True},
                 lesson="For {skill}{context}, observe first and give the policy a target box from the fresh detection."),
        Recovery("add_reference", "Condition the policy on the catalog reference image of the target.",
                 patch={"use_reference": True},
                 lesson="For {skill}{context}, condition the policy on the catalog reference image; look-alike items are common."),
        Recovery("verify_close_range", "Confirm the target and the result at close range.",
                 patch={"verify_close_range": True}, kind="criteria",
                 lesson="Verify {skill} targets and results{context} at close range before declaring success."),
        Recovery("keep_gripper_closed", "Go back for the dropped object and keep the gripper closed while retracting and travelling.",
                 patch={"keep_gripper_closed": True}, action="repick", scope="embodiment",
                 lesson="Keep the gripper closed during retraction and travel; release only at placement."),
        Recovery("pause_on_intrusion", "Pause while a person or hand is in the workspace, then recheck and resume.",
                 patch={"pause_on_intrusion": True}, kind="safety",
                 lesson="Pause when a person or hand enters the workspace, then recheck before resuming."),
        Recovery("wait_for_service", "Wait for the robot's action service to restart, then resume from the last completed step.",
                 action="wait", scope="embodiment", kind="recovery",
                 lesson="When a robot's action service is unavailable, wait for the restart and resume from the last completed step."),
        Recovery("handoff", "Hand the remaining steps of this work item to another capable robot.", action="handoff", kind="coordination",
                 lesson="If {skill}{context} fails on one robot, hand the remaining steps to another capable robot."),
        Recovery("put_back", "Release the wrongly held object where it was taken, then retry.", action="put_back"),
        Recovery("reposition", "Pick the misplaced object up again and place it with a placement point.", action="reposition",
                 patch={"use_placement_point": True}),
        Recovery("add_placement_point", "Mark the planogram slot with a placement point.", patch={"use_placement_point": True},
                 lesson="For {skill}{context}, mark the placement point of the target slot."),
        Recovery("add_arm_assignment", "Give the policy an explicit arm assignment and corner order.",
                 patch={"detailed": SPREAD_DETAIL, "corner_points": True}, scope="embodiment",
                 lesson="For {skill}{context}: " + SPREAD_DETAIL),
        Recovery("mention_edges", "Ask the policy to wipe along the edges and corners too.",
                 patch={"constraints": ["Wipe along the edges and corners as well, not only the center."]},
                 lesson="For {skill}{context}, wipe along the edges and corners as well; check the edges at close range."),
        Recovery("search_elsewhere", "Search other candidate locations for the target.", action="search",
                 patch={"use_reference": True}),
    ]
}

# Ordered recovery options per failure code (skill-specific overrides first).
PLAYBOOK: dict[str, list[str]] = {
    "target_not_localized": ["use_wrist_camera", "add_target_box", "retry"],
    "target_not_found": ["add_reference", "use_wrist_camera", "search_elsewhere", "retry"],
    "wrong_object": ["add_reference", "add_target_box", "verify_close_range", "retry"],
    "grasp_failed": ["retry", "add_target_box", "use_wrist_camera"],
    "not_holding": ["keep_gripper_closed", "retry"],
    "dropped": ["keep_gripper_closed", "retry"],
    "misplaced": ["recheck", "reposition", "retry"],
    "workspace_intrusion": ["pause_on_intrusion", "retry"],
    "service_unavailable": ["wait_for_service", "handoff"],
    "unreachable": ["handoff"],
    "incomplete": ["retry"],
    "navigation_blocked": ["retry", "handoff"],
    "precondition_failed": ["retry"],
    "hand_occupied": ["put_back"],
    "criteria_failed": ["recheck", "retry"],
    "timeout": ["retry"],
    "admission_rejected": [],
    "unknown_skill": ["handoff"],
}

SKILL_PLAYBOOK: dict[tuple[str, str], list[str]] = {
    ("spread", "incomplete"): ["add_arm_assignment", "retry"],
    ("wipe", "incomplete"): ["mention_edges", "retry"],
    ("hang", "misplaced"): ["recheck", "use_wrist_camera", "reposition"],
    ("hang", "target_not_localized"): ["use_wrist_camera", "add_placement_point", "retry"],
    ("search", "target_not_found"): ["add_reference", "search_elsewhere", "retry"],
    ("search", "wrong_object"): ["verify_close_range", "add_reference", "retry"],
    ("handover", "wrong_object"): ["verify_close_range", "retry"],
    ("place", "misplaced"): ["recheck", "reposition", "retry"],
}


def get(name: str) -> Recovery:
    return RECOVERIES[name]


def plan(code: str | None, skill: str, *, tried: Iterable[str] = (), order_hint: Iterable[str] | None = None,
         limit: int = 4) -> list[Recovery]:
    """Ordered untried recovery options for a failure."""
    if not code:
        return []
    names = list(SKILL_PLAYBOOK.get((skill, code), PLAYBOOK.get(code, ["retry"])))
    if order_hint:
        hinted = [n for n in order_hint if n in RECOVERIES]
        names = hinted + [n for n in names if n not in hinted]
    tried_set = set(tried)
    out = []
    for n in names:
        if n in tried_set and n != "retry":
            continue
        out.append(RECOVERIES[n])
    # "retry" only once per step
    if "retry" in tried_set:
        out = [r for r in out if r.name != "retry"]
    return out[:limit]
