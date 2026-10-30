"""Error codes shared by EEP peers.

Failure codes describe what happened physically (``FailureReason.code``);
protocol errors describe why a request could not be processed at all.
"""

from __future__ import annotations

# Physical / execution failure codes reported in FailureReason.code.
FAILURE_CODES = {
    "target_not_found": "The target is not present where the robot looked.",
    "target_not_localized": "The target may be present but could not be localized reliably.",
    "wrong_object": "The robot acted on an object other than the requested target.",
    "grasp_failed": "The grasp did not hold the object.",
    "dropped": "The object was dropped (during retraction, transit or release).",
    "not_holding": "The step required a held object but the gripper is empty.",
    "hand_occupied": "The step required a free hand but the robot is holding something.",
    "misplaced": "The object was released away from the intended placement.",
    "unreachable": "The target is outside the robot's reach or capabilities.",
    "workspace_intrusion": "A person or hand entered the workspace during execution.",
    "service_unavailable": "The robot's execution service is not responding.",
    "navigation_blocked": "The robot could not reach the location.",
    "criteria_failed": "The policy reported success but completion criteria did not pass.",
    "admission_rejected": "Shared admission refused the request.",
    "precondition_failed": "A precondition of the step was not satisfied.",
    "timeout": "The step exceeded its time budget.",
    "incomplete": "The step finished only partially.",
    "unknown_skill": "The robot does not implement the requested skill.",
}


class EEPError(Exception):
    """Protocol-level error (bad request, unknown robot, ...)."""

    code = "eep_error"

    def __init__(self, message: str, *, data: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data or {}

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.data:
            out["data"] = self.data
        return out


class InvalidRequest(EEPError):
    code = "invalid_request"


class NotFound(EEPError):
    code = "not_found"


class Conflict(EEPError):
    code = "conflict"


class ToolError(EEPError):
    """An unexpected error while running a tool; reported to the agent instead of aborting its loop."""

    code = "tool_error"
