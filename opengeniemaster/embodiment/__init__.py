"""Embodiment layer: robot profiles, instances, calibration and execution adapters."""

from .adapters import AdapterError, ExecutionAdapter, HttpAdapter, ReplayAdapter, ReplayMismatch, SimAdapter
from .profiles import CAPABILITY_LADDER, FEATURES, PROFILES, RobotProfile, skills_for
from .robots import Calibration, Fleet, RobotInstance, make_robot

__all__ = [
    "AdapterError",
    "ExecutionAdapter",
    "HttpAdapter",
    "ReplayAdapter",
    "ReplayMismatch",
    "SimAdapter",
    "CAPABILITY_LADDER",
    "FEATURES",
    "PROFILES",
    "RobotProfile",
    "skills_for",
    "Calibration",
    "Fleet",
    "RobotInstance",
    "make_robot",
]
