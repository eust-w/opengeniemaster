"""Task criteria: completion detectors evaluated after execution.

The policy's own "done" signal is not trusted. After each step the harness
asks the robot's perception service to evaluate the step's criteria and turns
disagreement into a failure the agent can act on.
"""

from __future__ import annotations

from typing import Any

# criterion -> argument keys taken from the step arguments
CRITERIA_ARGS: dict[str, tuple[str, ...]] = {
    "at_location": ("location",),
    "holding_target": ("sku",),
    "on_slot": ("sku", "location", "slot"),
    "on_hook": ("sku", "location", "slot"),
    "delivered": ("sku", "location"),
    "surface_clean": ("location",),
    "cloth_coverage": ("location",),
    "in_bin": ("sku", "location"),
    "item_found": ("sku", "location"),
    "trash_cleared": ("location",),
}

# Failure code reported when a criterion fails although the policy reported success.
CRITERIA_FAILURE: dict[str, str] = {
    "at_location": "navigation_blocked",
    "holding_target": "wrong_object",
    "on_slot": "misplaced",
    "on_hook": "misplaced",
    "delivered": "wrong_object",
    "surface_clean": "incomplete",
    "cloth_coverage": "incomplete",
    "in_bin": "misplaced",
    "item_found": "wrong_object",
    "trash_cleared": "incomplete",
}

# Skills whose effect persists in the world and can be re-verified later.
PERSISTENT_EFFECT = {"place", "hang", "handover", "wipe", "spread"}


def criterion_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    keys = CRITERIA_ARGS.get(name, ())
    out = {k: args[k] for k in keys if k in args}
    if name == "in_bin" and "location" not in out:
        out["location"] = "bin"
    return out


def failure_code(name: str, robot_state: dict[str, Any]) -> str:
    code = CRITERIA_FAILURE.get(name, "criteria_failed")
    if name == "holding_target" and not robot_state.get("holding", True):
        return "grasp_failed"
    return code
