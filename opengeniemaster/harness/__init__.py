"""Harness core: continuity, admission, criteria, guidance, recovery, orchestration.

Attributes are resolved lazily so that low-level modules (e.g. reflection,
which uses the recovery vocabulary) can import submodules without importing
the whole harness.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "Harness": "harness",
    "HarnessConfig": "harness",
    "SharedAdmission": "admission",
    "Decision": "admission",
    "TaskStore": "continuity",
    "EventLog": "events",
    "GuidanceBuilder": "guidance",
    "GuidancePlan": "guidance",
    "ScriptedOrchestrator": "orchestrator",
    "OrchestratorConfig": "orchestrator",
    "RunResult": "orchestrator",
    "GENIEMASTER": "orchestrator",
    "DUAL_SYSTEM": "orchestrator",
    "VANILLA_VLA": "orchestrator",
    "run_task": "orchestrator",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f".{module}", __name__), name)
