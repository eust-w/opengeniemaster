"""Event log of everything that crossed the agent-robot boundary.

Rows are ``{"seq", "t", "type", "data"}``. The log feeds replay-based
offline tests (``ReplayAdapter``) and the policy-data flywheel export.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._util import append_jsonl, last_jsonl_row, read_jsonl


class EventLog:
    def __init__(self, path: str | Path | None = None, *, keep_in_memory: bool = True) -> None:
        self.path = Path(path) if path is not None else None
        self.keep_in_memory = keep_in_memory
        self.rows: list[dict[str, Any]] = []
        last = last_jsonl_row(self.path) if self.path is not None else None
        self._seq = int(last.get("seq", 0)) if isinstance(last, dict) else 0  # continue an existing log

    def emit(self, kind: str, t: float, data: dict[str, Any]) -> None:
        self._seq += 1
        row = {"seq": self._seq, "t": round(t, 3), "type": kind, "data": data}
        if self.keep_in_memory:
            self.rows.append(row)
        if self.path is not None:
            append_jsonl(self.path, row)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["type"] == kind]

    @staticmethod
    def load(path: str | Path) -> list[dict[str, Any]]:
        return read_jsonl(path)
