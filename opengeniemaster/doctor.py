"""Pre-flight checks (``ogm doctor``): is this installation and fleet ready to hand to a coding agent?

Without a fleet file the checks cover the local installation (Python, the
optional Anthropic SDK, the state directory). With ``--fleet`` every robot's
execution service is probed: reachable, authenticated, speaking the same
protocol version, controlling the robot it is configured as, and calibrated.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .eep.types import PROTOCOL_VERSION

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", **data: Any) -> None:
        self.checks.append(Check(name, status, detail, data))

    @property
    def ok(self) -> bool:
        return all(c.status != FAIL for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [asdict(c) for c in self.checks]}

    def render(self) -> str:
        marks = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}
        lines = [f"{marks[c.status]} {c.name}" + (f": {c.detail}" if c.detail else "") for c in self.checks]
        fails = sum(c.status == FAIL for c in self.checks)
        warns = sum(c.status == WARN for c in self.checks)
        lines.append("")
        lines.append("ready" if not fails else "not ready")
        lines[-1] += f" ({fails} failed, {warns} warnings, {len(self.checks)} checks)"
        return "\n".join(lines)


def check_environment(report: DoctorReport, state_dir: str | Path | None = None) -> None:
    v = sys.version_info
    report.add("python", OK, f"{v.major}.{v.minor}.{v.micro}")
    if importlib.util.find_spec("anthropic") is None:
        report.add("anthropic SDK", WARN, "not installed; only needed for `ogm agent` and `ogm reflect --llm` (pip install -e '.[claude]')")
    else:
        report.add("anthropic SDK", OK, "installed")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            report.add("anthropic credentials", WARN, "ANTHROPIC_API_KEY is not set")
    if state_dir is not None:
        path = Path(state_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, prefix=".doctor-", delete=True):
                pass
        except OSError as exc:
            report.add("state dir", FAIL, f"{path} is not writable: {exc}")
        else:
            report.add("state dir", OK, f"{path} is writable")


def check_fleet(report: DoctorReport, fleet_path: str | Path, *, timeout: float = 5.0) -> None:
    import json

    from .runtime import harness_from_fleet_file

    path = Path(fleet_path)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.add("fleet file", FAIL, f"cannot read {path}: {exc}")
        return
    rows = spec.get("robots") if isinstance(spec, dict) else None
    if not isinstance(rows, list) or not rows:
        report.add("fleet file", FAIL, f"{path} lists no robots")
        return
    for row in rows:
        if row.get("token_env") and not os.environ.get(row["token_env"]):
            report.add(f"robot {row.get('robot_id', '?')}: token", WARN, f"environment variable {row['token_env']} is not set")
    try:
        harness = harness_from_fleet_file(path)  # no state dir: the doctor must not create task or memory files
    except Exception as exc:  # any malformed field ends the check with a readable message
        report.add("fleet file", FAIL, f"{type(exc).__name__}: {exc}")
        return
    report.add("fleet file", OK, f"{len(rows)} robots, domains: {', '.join(sorted(harness.domains))}")
    ids = [row.get("robot_id") for row in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        report.add("fleet file", FAIL, f"duplicate robot ids: {', '.join(map(str, dupes))}")

    robots = list(harness.fleet)
    for robot in robots:
        adapter = robot.adapter
        if hasattr(adapter, "timeout"):  # probes must be quick, unlike real actions
            adapter.timeout = min(float(adapter.timeout), timeout)
        if hasattr(adapter, "retries"):
            adapter.retries = 0
    # Probe every robot at once: a fleet with several unreachable robots should not wait for each timeout in turn.
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(robots)))) as pool:
        results = list(pool.map(_probe, robots))
    for robot, (status, detail, data) in zip(robots, results, strict=True):
        report.add(f"robot {robot.robot_id}", status, detail, **data)


def _probe(robot: Any) -> tuple[str, str, dict[str, Any]]:
    health = robot.adapter.health(robot)
    if not health.get("ok"):
        status = health.get("http_status")
        hint = " (check the bearer token)" if status in (401, 403) else ""
        return FAIL, f"unreachable: {health.get('error', 'health check failed')}{hint}", health
    problems = []
    if health.get("robot_id") not in (None, robot.robot_id):
        problems.append(f"service controls '{health['robot_id']}', not '{robot.robot_id}'")
    protocol = health.get("protocol")
    if protocol and protocol != PROTOCOL_VERSION:
        problems.append(f"protocol {protocol} != {PROTOCOL_VERSION}")
    if problems:
        return FAIL, "; ".join(problems), health
    state = robot.adapter.state(robot)
    if "error" in state:
        return FAIL, f"reachable but /state failed: {state['error']}", {}
    if state.get("stopped"):
        return WARN, "reachable, but the robot reports it is stopped", state
    if not robot.calibration.valid:
        return WARN, f"reachable, but calibration {robot.calibration.version} is marked invalid", {}
    return OK, f"reachable, {protocol or 'protocol unknown'}, location={state.get('location')}", {}


def run(*, fleet: str | Path | None = None, state_dir: str | Path | None = None, timeout: float = 5.0) -> DoctorReport:
    report = DoctorReport()
    check_environment(report, state_dir)
    if fleet is not None:
        check_fleet(report, fleet, timeout=timeout)
    return report
