"""Temporary memory (execution records) and long-term memory (instructions).

GenieMaster's continual harness keeps model parameters fixed and improves the
*context* used for guidance, assessment and orchestration:

    temporary memory (execution records)
        -> asynchronous reflection (review and consolidate)
        -> long-term memory (reusable instructions)
        -> next-day planning

Long-term memory is split into domain memory and embodiment memory, mirroring
the architecture figure. Every admitted change bumps a version and is
snapshotted so that a bad lesson can be inspected, diffed and rolled back.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .._serde import from_dict, to_dict
from .._util import append_jsonl, file_lock, read_json, read_jsonl, write_json_atomic

SCOPES = ("domain", "embodiment")
KINDS = ("guidance", "criteria", "recovery", "coordination", "safety")
WHEN_KEYS = ("domain", "skill", "robot_model", "features_all", "tags_any", "tags_all", "sku")


@dataclass
class ExecutionRecord:
    record_id: str
    t: float
    robot_id: str
    skill: str
    success: bool
    outcome: str
    robot_model: str = ""
    domain: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    goal: str | None = None
    instruction: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    guidance: dict[str, Any] = field(default_factory=dict)
    failure_code: str | None = None
    failure_message: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    criteria: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    attempt: int = 1
    recovery: str | None = None
    applied_memory: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    contradicted: bool = False
    contradiction: str | None = None
    kind: str = "execution"  # execution | note | takeover
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionRecord":
        return from_dict(cls, data)

    def brief(self) -> dict[str, Any]:
        out = {
            "record_id": self.record_id,
            "t": round(self.t, 1),
            "robot": f"{self.robot_id} ({self.robot_model})",
            "skill": self.skill,
            "args": self.args,
            "success": self.success,
            "attempt": self.attempt,
            "tags": self.tags,
        }
        if self.failure_code:
            out["failure"] = {"code": self.failure_code, "message": self.failure_message, "evidence": self.evidence}
        if self.recovery:
            out["recovery"] = self.recovery
        if self.contradicted:
            out["contradicted"] = self.contradiction or True
        if self.kind != "execution":
            out["kind"] = self.kind
            out["note"] = self.note
        if self.task_id:
            out["task"] = f"{self.task_id}/{self.step_id}" if self.step_id else self.task_id
        return out


@dataclass
class MemoryEntry:
    entry_id: str
    scope: str  # domain | embodiment
    kind: str  # guidance | criteria | recovery | coordination | safety
    text: str
    when: dict[str, Any] = field(default_factory=dict)
    patch: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    status: str = "admitted"  # admitted | retired
    source: str = "reflection"
    created_at: float = 0.0
    updated_at: float = 0.0
    version_added: int = 0
    applied: int = 0
    succeeded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        return from_dict(cls, data)

    @property
    def specificity(self) -> int:
        w = self.when
        try:
            return sum(1 for k in w if k not in ("tags_any", "tags_all", "features_all")) + len(w.get("tags_all", [])) + (
                1 if w.get("tags_any") else 0) + len(w.get("features_all", []))
        except TypeError:
            return 0

    def matches(self, ctx: dict[str, Any]) -> bool:
        try:
            return self._matches(ctx)
        except (TypeError, AttributeError):
            return False  # malformed entries never apply

    def _matches(self, ctx: dict[str, Any]) -> bool:
        w = self.when
        for key in ("domain", "robot_model", "sku"):
            if key in w and w[key] != ctx.get(key):
                return False
        if "skill" in w:
            skills = w["skill"] if isinstance(w["skill"], list) else [w["skill"]]
            if ctx.get("skill") not in skills:
                return False
        tags = set(ctx.get("tags", []))
        if w.get("tags_any") and not tags.intersection(w["tags_any"]):
            return False
        if w.get("tags_all") and not set(w["tags_all"]) <= tags:
            return False
        if w.get("features_all") and not set(w["features_all"]) <= set(ctx.get("features", [])):
            return False
        return True

    def brief(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "scope": self.scope,
            "kind": self.kind,
            "text": self.text,
            "when": self.when,
            "patch": self.patch,
            "support": len(self.evidence),
            "applied": self.applied,
            "succeeded": self.succeeded,
        }


class TemporaryMemory:
    """Append-only execution records with a reflection cursor.

    With a state directory, every write takes a file lock and first reads
    lines appended by other processes, so several processes (an MCP server,
    ``ogm reflect``, a scheduler) can share one directory.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: list[ExecutionRecord] = []
        self._index: dict[str, int] = {}
        self.cursor = 0
        self._offset = 0
        if path is not None:
            self._refresh()
            self._read_cursor()

    @property
    def _file(self) -> Path:
        assert self.path is not None
        return self.path / "records.jsonl"

    def _lock(self):
        assert self.path is not None
        return file_lock(self.path / ".records.lock")

    def _apply(self, row: dict[str, Any]) -> None:
        kind = row.get("type")
        if kind == "record":
            rec = ExecutionRecord.from_dict(row["data"])
            if rec.record_id in self._index:
                return
            self._index[rec.record_id] = len(self.records)
            self.records.append(rec)
        elif kind == "contradiction":
            idx = self._index.get(row.get("record_id", ""))
            if idx is not None:
                self.records[idx].contradicted = True
                self.records[idx].contradiction = row.get("reason")

    def _refresh(self) -> None:
        """Read complete lines appended since the last read (by any process)."""
        if self.path is None or not self._file.exists():
            return
        with self._file.open("rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        for line in data[: end + 1].splitlines():
            if line.strip():
                self._apply(json.loads(line))
        self._offset += end + 1

    def _maybe_refresh(self) -> None:
        if self.path is not None and self._file.exists() and self._file.stat().st_size > self._offset:
            self._refresh()

    def _read_cursor(self) -> None:
        state = read_json(self.path / "state.json", {}) if self.path is not None else {}
        self.cursor = min(int((state or {}).get("cursor", self.cursor)), len(self.records))

    def _append(self, row: dict[str, Any]) -> None:
        with self._lock():
            self._refresh()
            append_jsonl(self._file, row)
            self._refresh()

    def add(self, record: ExecutionRecord) -> ExecutionRecord:
        if self.path is None:
            self._index[record.record_id] = len(self.records)
            self.records.append(record)
            return record
        self._append({"type": "record", "data": record.to_dict()})
        return self.records[self._index[record.record_id]]

    def get(self, record_id: str) -> ExecutionRecord | None:
        idx = self._index.get(record_id)
        if idx is None:
            self._maybe_refresh()
            idx = self._index.get(record_id)
        return self.records[idx] if idx is not None else None

    def __contains__(self, record_id: str) -> bool:
        return self.get(record_id) is not None

    def __len__(self) -> int:
        self._maybe_refresh()
        return len(self.records)

    def mark_contradicted(self, record_id: str, reason: str) -> bool:
        rec = self.get(record_id)
        if rec is None or rec.contradicted:
            return False
        if self.path is None:
            rec.contradicted = True
            rec.contradiction = reason
        else:
            self._append({"type": "contradiction", "record_id": record_id, "reason": reason})
        return True

    def pending(self) -> list[ExecutionRecord]:
        self._maybe_refresh()
        if self.path is not None:
            self._read_cursor()
        return self.records[self.cursor:]

    def advance(self, upto: int | None = None) -> None:
        if self.path is None:
            self.cursor = len(self.records) if upto is None else max(self.cursor, min(upto, len(self.records)))
            return
        with self._lock():
            self._refresh()
            self._read_cursor()
            self.cursor = len(self.records) if upto is None else max(self.cursor, min(upto, len(self.records)))
            write_json_atomic(self.path / "state.json", {"cursor": self.cursor})

    def query(self, *, task_id: str | None = None, step_id: str | None = None, skill: str | None = None,
              success: bool | None = None, robot_id: str | None = None, limit: int | None = None) -> list[ExecutionRecord]:
        self._maybe_refresh()
        out = []
        for r in self.records:
            if task_id is not None and r.task_id != task_id:
                continue
            if step_id is not None and r.step_id != step_id:
                continue
            if skill is not None and r.skill != skill:
                continue
            if success is not None and r.success != success:
                continue
            if robot_id is not None and r.robot_id != robot_id:
                continue
            out.append(r)
        return out[-limit:] if limit else out


class LongTermMemory:
    """Versioned reusable instructions, split into domain and embodiment scope.

    With a state directory, every change is made under a file lock against
    the latest file contents, and readers pick up changes made by other
    processes (e.g. ``ogm reflect`` while an MCP server is running).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.entries: dict[str, MemoryEntry] = {}
        self.version = 0
        self._snapshots: dict[int, list[dict[str, Any]]] = {0: []}
        self._mtime: float | None = None
        if path is not None:
            self._load()

    # persistence -------------------------------------------------------------

    @property
    def _file(self) -> Path:
        assert self.path is not None
        return self.path / "long_term.json"

    def _load(self) -> None:
        assert self.path is not None
        data = read_json(self._file)
        if data:
            self.version = int(data.get("version", 0))
            self.entries = {}
            for row in data.get("entries", []):
                e = MemoryEntry.from_dict(row)
                self.entries[e.entry_id] = e
        self._mtime = self._file.stat().st_mtime_ns if self._file.exists() else None

    def _maybe_reload(self) -> None:
        if self.path is not None and self._file.exists() and self._file.stat().st_mtime_ns != self._mtime:
            self._load()

    @contextlib.contextmanager
    def _transaction(self):
        if self.path is None:
            yield
            return
        with file_lock(self.path / ".long_term.lock"):
            self._load()
            yield

    def _write(self) -> None:
        if self.path is not None:
            write_json_atomic(self._file, {"version": self.version, "entries": [e.to_dict() for e in self.entries.values()]})
            self._mtime = self._file.stat().st_mtime_ns

    def _commit(self, action: str, entry_id: str, reason: str, t: float) -> None:
        self.version += 1
        snap = [e.to_dict() for e in self.entries.values()]
        self._snapshots[self.version] = snap
        if self.path is not None:
            write_json_atomic(self.path / "versions" / f"v{self.version:04d}.json", {"version": self.version, "entries": snap})
            append_jsonl(self.path / "history.jsonl", {"version": self.version, "t": t, "action": action, "entry_id": entry_id, "reason": reason})
            self._write()

    def snapshot(self, version: int) -> list[dict[str, Any]]:
        if version in self._snapshots:
            return self._snapshots[version]
        if self.path is not None:
            data = read_json(self.path / "versions" / f"v{version:04d}.json")
            if data is not None:
                return data["entries"]
        if version == 0:
            return []
        raise KeyError(f"memory version {version} not found")

    def history(self) -> list[dict[str, Any]]:
        if self.path is None:
            return []
        return read_jsonl(self.path / "history.jsonl")

    # queries -----------------------------------------------------------------

    def admitted(self, scope: str | None = None, kind: str | None = None) -> list[MemoryEntry]:
        self._maybe_reload()
        out = [e for e in self.entries.values() if e.status == "admitted"]
        if scope:
            out = [e for e in out if e.scope == scope]
        if kind:
            out = [e for e in out if e.kind == kind]
        return sorted(out, key=lambda e: e.entry_id)

    def relevant(self, ctx: dict[str, Any], kinds: Iterable[str] | None = None, limit: int = 12) -> list[MemoryEntry]:
        kinds = set(kinds) if kinds else None
        hits = [e for e in self.admitted() if (kinds is None or e.kind in kinds) and e.matches(ctx)]
        hits.sort(key=lambda e: (e.specificity, e.succeeded - 0.5 * (e.applied - e.succeeded), -e.version_added), reverse=True)
        return hits[:limit]

    def search(self, text: str, limit: int = 20) -> list[MemoryEntry]:
        words = [w for w in text.lower().split() if w]
        scored = []
        for e in self.admitted():
            hay = (e.text + " " + str(e.when) + " " + str(e.patch)).lower()
            score = sum(1 for w in words if w in hay)
            if score or not words:
                scored.append((-score, e.entry_id, e))
        scored.sort()
        return [e for _, _, e in scored[:limit]]

    # changes -----------------------------------------------------------------

    def add(self, entry: MemoryEntry, *, reason: str, t: float = 0.0) -> MemoryEntry:
        with self._transaction():
            entry.status = "admitted"
            entry.created_at = entry.created_at or t
            entry.updated_at = t
            entry.version_added = self.version + 1
            self.entries[entry.entry_id] = entry
            self._commit("admit", entry.entry_id, reason, t)
        return entry

    def merge_evidence(self, entry_id: str, evidence: Iterable[str], *, t: float = 0.0) -> MemoryEntry:
        with self._transaction():
            e = self.entries[entry_id]
            new = [x for x in dict.fromkeys(evidence) if x not in e.evidence]
            if new:
                e.evidence.extend(new)
                e.updated_at = t
                self._commit("merge", entry_id, f"+{len(new)} evidence", t)
        return e

    def retire(self, entry_id: str, *, reason: str, t: float = 0.0) -> MemoryEntry:
        with self._transaction():
            e = self.entries[entry_id]
            if e.status != "retired":
                e.status = "retired"
                e.updated_at = t
                self._commit("retire", entry_id, reason, t)
        return e

    def record_use(self, entry_ids: Iterable[str], success: bool) -> None:
        ids = list(entry_ids)
        if not ids:
            return
        with self._transaction():
            changed = False
            for eid in ids:
                e = self.entries.get(eid)
                if e is None:
                    continue
                e.applied += 1
                if success:
                    e.succeeded += 1
                changed = True
            if changed:
                self._write()

    def rollback(self, version: int, *, t: float = 0.0) -> None:
        """Restore the entry set of an earlier version (recorded as a new version)."""
        with self._transaction():
            snap = self.snapshot(version)
            self.entries = {row["entry_id"]: MemoryEntry.from_dict(row) for row in snap}
            self._commit("rollback", f"v{version}", f"rollback to v{version}", t)

    def diff(self, a: int, b: int) -> dict[str, Any]:
        sa = {row["entry_id"]: row for row in self.snapshot(a)}
        sb = {row["entry_id"]: row for row in self.snapshot(b)}
        added = [sb[k] for k in sb if k not in sa]
        removed = [sa[k] for k in sa if k not in sb]
        changed = []
        for k in sa.keys() & sb.keys():
            if sa[k].get("status") != sb[k].get("status") or sa[k].get("evidence") != sb[k].get("evidence"):
                changed.append({"entry_id": k, "from": {"status": sa[k].get("status"), "support": len(sa[k].get("evidence", []))},
                                "to": {"status": sb[k].get("status"), "support": len(sb[k].get("evidence", []))}})
        return {"from": a, "to": b, "added": added, "removed": removed, "changed": changed}


class MemoryStore:
    """Temporary + long-term memory under one state directory (or in RAM)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.temporary = TemporaryMemory(self.path)
        self.long_term = LongTermMemory(self.path)

    def stats(self) -> dict[str, Any]:
        lt = self.long_term.admitted()
        return {
            "records": len(self.temporary),
            "pending_reflection": len(self.temporary.pending()),
            "long_term_version": self.long_term.version,
            "long_term_entries": len(lt),
            "domain_entries": sum(1 for e in lt if e.scope == "domain"),
            "embodiment_entries": sum(1 for e in lt if e.scope == "embodiment"),
        }
