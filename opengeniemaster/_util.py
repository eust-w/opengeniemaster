"""Small shared helpers: ids, clocks, JSON files and hashing."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # POSIX advisory locks; on platforms without fcntl the lock is a no-op
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "file_lock",
    "last_jsonl_row",
    "IdFactory",
    "Clock",
    "WallClock",
    "ManualClock",
    "canonical_json",
    "stable_hash",
    "read_json",
    "write_json_atomic",
    "append_jsonl",
    "read_jsonl",
]


class IdFactory:
    """Generates readable ids such as ``task-a3f2-0007``.

    A seeded factory is deterministic, which keeps simulation runs and tests
    reproducible. Unseeded factories use a random session tag so that ids
    created by different processes sharing one state directory do not clash.
    """

    def __init__(self, seed: int | None = None, session: str | None = None) -> None:
        rng = random.Random(seed) if seed is not None else random.SystemRandom()
        self.session = session or f"{rng.getrandbits(24):06x}"
        self._counters: dict[str, Iterator[int]] = {}

    def make(self, kind: str) -> str:
        counter = self._counters.setdefault(kind, itertools.count(1))
        return f"{kind}-{self.session}-{next(counter):04d}"


class Clock:
    """Time source used for records. Simulations substitute their own."""

    def now(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError


class WallClock(Clock):
    def now(self) -> float:
        return time.time()


class ManualClock(Clock):
    def __init__(self, start: float = 0.0, source: Callable[[], float] | None = None) -> None:
        self._t = start
        self._source = source

    def now(self) -> float:
        return self._source() if self._source else self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def stable_hash(obj: Any, length: int = 12) -> str:
    return hashlib.sha1(canonical_json(obj).encode("utf-8")).hexdigest()[:length]


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json_atomic(path: str | os.PathLike[str], obj: Any) -> None:
    """Write JSON via a temp file + rename so readers never see partial files."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(path: str | os.PathLike[str], obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, default=str))
        fh.write("\n")


def read_jsonl(path: str | os.PathLike[str]) -> list[Any]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}:{line_no}: invalid JSON line: {exc}") from exc
    return rows


@contextlib.contextmanager
def file_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    """Exclusive advisory lock so that several processes can share one state directory."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def last_jsonl_row(path: str | os.PathLike[str], chunk: int = 65536) -> Any:
    """The last complete JSON line of a file (None if empty or missing)."""
    p = Path(path)
    if not p.exists():
        return None
    with p.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - chunk))
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None
