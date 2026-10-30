"""Capability gaps and idle-time practice ("自主加练").

Repeated failures, unstable outcomes or unusually slow executions in a context
mark a capability gap. During idle time the agent can schedule targeted
practice: re-run the failing interaction in a controlled way (take the item
and put it back), grade it with the same criteria, and let reflection
consolidate what worked.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import TYPE_CHECKING, Any, Callable

from ..eep.types import StepTemplate
from ..memory.reflection import salient

if TYPE_CHECKING:  # pragma: no cover
    from .harness import Harness

PRACTICABLE = {"pick", "place", "hang"}


@dataclass
class CapabilityGap:
    domain: str | None
    skill: str
    robot_model: str
    tags: list[str]
    attempts: int
    failures: int
    failure_rate: float
    mean_attempts_per_success: float
    slow: bool
    common_failures: list[str]
    example_args: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> float:
        return self.failure_rate + 0.25 * max(0.0, self.mean_attempts_per_success - 1.0) + (0.2 if self.slow else 0.0)

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["severity"] = round(self.severity, 3)
        return out


def capability_gaps(harness: "Harness", *, window: int = 400, min_attempts: int = 3, failure_threshold: float = 0.3) -> list[CapabilityGap]:
    records = [r for r in harness.memory.temporary.records[-window:] if r.kind == "execution" and r.failure_code != "admission_rejected"]
    groups: dict[tuple, list] = defaultdict(list)
    for r in records:
        groups[(r.domain, r.skill, r.robot_model, tuple(sorted(salient(r.tags))))].append(r)
    durations: dict[str, list[float]] = defaultdict(list)
    for r in records:
        if r.success:
            durations[r.skill].append(r.duration_s)
    gaps = []
    for (domain, skill, model, tags), recs in groups.items():
        if len(recs) < min_attempts:
            continue
        failures = [r for r in recs if not r.success or r.contradicted]
        successes = [r for r in recs if r.success and not r.contradicted]
        rate = len(failures) / len(recs)
        per_success = len(recs) / max(1, len(successes))
        typical = median(durations[skill]) if durations[skill] else 0.0
        mine = [r.duration_s for r in successes]
        slow = bool(mine) and typical > 0 and median(mine) > 1.8 * typical
        if rate < failure_threshold and per_success < 1.6 and not slow:
            continue
        codes = Counter(r.failure_code or ("contradicted" if r.contradicted else "?") for r in failures)
        example = (failures or recs)[-1].args
        gaps.append(CapabilityGap(domain, skill, model, list(tags), len(recs), len(failures), round(rate, 3), round(per_success, 2), slow,
                                  [c for c, _ in codes.most_common(3)], dict(example)))
    gaps.sort(key=lambda g: g.severity, reverse=True)
    return gaps


def plan_practice(harness: "Harness", gap: CapabilityGap, *, repetitions: int = 2,
                  available: Callable[[str], bool] | None = None) -> str | None:
    """Create a practice task that re-runs the failing interaction; returns its task id.

    ``available(sku)`` lets the caller consult an inventory system so that
    practice never targets an item that is not on its shelf.
    """
    if gap.skill not in PRACTICABLE or not gap.domain:
        return None
    args = gap.example_args
    sku, loc = args.get("sku"), args.get("location")
    dom = harness.domain(gap.domain)
    if not sku or not loc or dom is None:
        return None
    if available is not None and not available(sku):
        return None
    item = dom.records.catalog.get(sku)
    home_loc = item.home if item and item.home else loc
    slot = item.slot if item and item.home == home_loc else args.get("slot")
    put_skill = "hang" if (item is not None and item.mount == "hook") else "place"
    steps: list[StepTemplate] = [StepTemplate("p-go", "navigate_to", {"location": home_loc}, f"Go to {home_loc} to practice", requires=["mobile"],
                                              group="p")]
    prev = "p-go"
    for i in range(1, repetitions + 1):
        steps.append(StepTemplate(f"p{i}-pick", "pick", {"sku": sku, "location": home_loc}, f"Practice: take the {sku}", depends_on=[prev],
                                  group="p"))
        put_args: dict[str, Any] = {"sku": sku, "location": home_loc}
        if slot:
            put_args["slot"] = slot
        steps.append(StepTemplate(f"p{i}-{put_skill}", put_skill, put_args, f"Practice: put the {sku} back",
                                  depends_on=[f"p{i}-pick"], group="p",
                                  criteria=["on_hook" if put_skill == "hang" else "on_slot"]))
        prev = f"p{i}-{put_skill}"
    task = harness.create_task(f"Practice {gap.skill} ({', '.join(gap.tags) or 'general'}) on {sku}", domain=gap.domain, steps=steps,
                               priority=-1)
    harness.update_task(task.task_id, "note", note=f"practice for gap: {gap.common_failures}")
    return task.task_id
