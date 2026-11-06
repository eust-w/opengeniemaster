"""Event log of everything that crossed the agent-robot boundary.

Rows are ``{"seq", "t", "type", "data"}``. The log feeds replay-based
offline tests (``ReplayAdapter``) and the policy-data flywheel export.

A persistent log rotates once it reaches ``max_bytes``: ``events.jsonl``
becomes ``events.jsonl.1``, older files shift up to ``backups`` and the
oldest is dropped. Sequence numbers continue across rotations, and
:meth:`EventLog.load` can stitch the rotated files back together. The
in-memory copy keeps at most ``max_rows`` recent rows so that a server that
runs for days does not grow without bound.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._util import append_jsonl, file_lock, last_jsonl_row, read_jsonl

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_BACKUPS = 5
DEFAULT_MAX_ROWS = 50_000


def rotated_paths(path: str | Path) -> list[Path]:
    """Existing rotated files of ``path``, oldest first (``.N`` ... ``.1``)."""
    p = Path(path)
    found = []
    for candidate in p.parent.glob(p.name + ".*"):
        suffix = candidate.name[len(p.name) + 1:]
        if suffix.isdigit():
            found.append((int(suffix), candidate))
    return [c for _, c in sorted(found, reverse=True)]


class EventLog:
    def __init__(self, path: str | Path | None = None, *, keep_in_memory: bool = True, max_bytes: int | None = DEFAULT_MAX_BYTES,
                 backups: int = DEFAULT_BACKUPS, max_rows: int | None = DEFAULT_MAX_ROWS) -> None:
        self.path = Path(path) if path is not None else None
        self.keep_in_memory = keep_in_memory
        self.max_bytes = max_bytes if max_bytes and max_bytes > 0 else None
        self.backups = max(0, int(backups))
        self.max_rows = max_rows if max_rows and max_rows > 0 else None
        self.rows: list[dict[str, Any]] = []
        self.dropped_rows = 0  # rows trimmed from memory (still on disk when persistent)
        self._seq = self._last_seq()  # continue an existing (possibly just rotated) log

    def _last_seq(self) -> int:
        if self.path is None:
            return 0
        for candidate in [self.path, *reversed(rotated_paths(self.path))]:
            last = last_jsonl_row(candidate)
            if isinstance(last, dict):
                return int(last.get("seq", 0))
        return 0

    def emit(self, kind: str, t: float, data: dict[str, Any]) -> None:
        self._seq += 1
        row = {"seq": self._seq, "t": round(t, 3), "type": kind, "data": data}
        if self.keep_in_memory:
            self.rows.append(row)
            if self.max_rows is not None and len(self.rows) > self.max_rows:
                # trim in chunks so that a full log does not shift the list on every event
                drop = len(self.rows) - self.max_rows + max(1, self.max_rows // 10)
                del self.rows[:drop]
                self.dropped_rows += drop
        if self.path is not None:
            if self.max_bytes is None:
                append_jsonl(self.path, row)
            else:
                # several processes may share one state directory: rotate and append under one lock
                with file_lock(self.path.with_name(self.path.name + ".lock")):
                    self._rotate_if_needed()
                    append_jsonl(self.path, row)

    def _rotate_if_needed(self) -> None:
        assert self.path is not None and self.max_bytes is not None
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.max_bytes:
            return
        if self.backups == 0:
            self.path.unlink()
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        if oldest.exists():
            oldest.unlink()
        for n in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{n}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{n + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["type"] == kind]

    @staticmethod
    def load(path: str | Path, *, include_rotated: bool = False) -> list[dict[str, Any]]:
        """Rows of a log file; with ``include_rotated`` the rotated files come first, in sequence order."""
        if not include_rotated:
            return read_jsonl(path)
        rows: list[dict[str, Any]] = []
        for candidate in [*rotated_paths(path), Path(path)]:
            rows.extend(read_jsonl(candidate))
        return rows
