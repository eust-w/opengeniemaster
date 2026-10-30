"""Policy-RSI data flywheel: execution logs -> filtered, weighted, relabeled episodes.

GenieMaster describes two improvement routes. Harness RSI (memory, guidance,
criteria) is implemented by the harness itself. Policy RSI updates the action
policy's weights from data gathered in deployment: autonomous executions,
human takeovers and recoveries, cleaned by *filtering, weighting and goal
relabeling* before supervised, imitation or reinforcement learning. This
module implements that data preparation; training and gated deployment of a
new policy version are left to the policy's own stack.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ._util import append_jsonl, write_json_atomic
from .memory.store import ExecutionRecord

POLICY_SKILLS = {"pick", "place", "hang", "handover", "wipe", "spread"}


def build_episodes(events: Iterable[dict[str, Any]], *, records: Iterable[ExecutionRecord] = (), include_failures: bool = True,
                   recovered_weight: float = 2.0) -> list[dict[str, Any]]:
    """Turn ``execute`` events into training episodes.

    * Filter: keep policy-service skills; drop rejected/errored requests and successes
      that later evidence contradicted.
    * Weight: successes that needed a recovery are hard examples (``recovered_weight``);
      repeated near-identical successes are down-weighted by 1/sqrt(count).
    * Relabel: a grasp that succeeded on the *wrong* object becomes a positive example
      for the object actually grasped (hindsight goal relabeling).
    """
    by_record = {r.record_id: r for r in records}
    rows = []
    for position, ev in enumerate(events):
        if ev.get("type") != "execute":
            continue
        data = ev["data"]
        req, fb = data.get("request", {}), data.get("feedback", {})
        skill = req.get("skill")
        if skill not in POLICY_SKILLS:
            continue
        outcome = fb.get("action_result", {}).get("outcome")
        if outcome in ("rejected", "error"):
            continue
        rec = by_record.get(data.get("record_id"))
        if rec is not None and rec.contradicted and fb.get("success"):
            continue
        rows.append((position, ev, req, fb, rec))
    keys = Counter((req.get("skill"), req.get("args", {}).get("sku"), req.get("args", {}).get("location"))
                   for _, _, req, fb, _ in rows if fb.get("success"))
    episodes = []
    for position, ev, req, fb, rec in rows:
        success = bool(fb.get("success"))
        reason = (fb.get("failure_reason") or {}).get("code")
        instruction = req.get("guidance", {}).get("instruction", "")
        episode: dict[str, Any] = {
            "episode_id": f"ep-{position:06d}",  # position in the log: unique even if several writers shared it
            "t": ev.get("t"),
            "robot_id": req.get("robot_id"),
            "skill": req.get("skill"),
            "args": req.get("args", {}),
            "instruction": instruction,
            "guidance": req.get("guidance", {}),
            "observation_after": fb.get("observation", {}),
            "action_result": fb.get("action_result", {}),
            "success": success,
            "source": "recovery" if req.get("recovery") else "autonomous",
        }
        operator = fb.get("action_result", {}).get("details", {}).get("operator")
        if operator or "human_takeover" in req.get("tags", []) or (rec is not None and rec.kind == "takeover"):
            episode["source"] = "human_takeover"
        if success:
            k = (req.get("skill"), req.get("args", {}).get("sku"), req.get("args", {}).get("location"))
            weight = 1.0 / math.sqrt(keys[k])
            if req.get("recovery"):
                weight *= recovered_weight
            episode["weight"] = round(weight, 4)
            episodes.append(episode)
            continue
        held = fb.get("observation", {}).get("robot_state", {}).get("held_label")
        if req.get("skill") == "pick" and reason == "wrong_object" and held and fb.get("policy_success"):
            relabeled = dict(episode)
            relabeled["episode_id"] += "-hr"
            relabeled["args"] = dict(episode["args"], sku=held)
            relabeled["instruction"] = f"Pick the {held}"
            relabeled["guidance"] = dict(episode["guidance"], instruction=f"Pick the {held}", references=[], spatial=[])
            relabeled["success"] = True
            relabeled["relabeled_from"] = req.get("args", {}).get("sku")
            relabeled["weight"] = 0.5
            episodes.append(relabeled)
        if include_failures:
            episode["weight"] = 0.0
            episode["failure"] = reason
            episodes.append(episode)
    return episodes


def export(events: Iterable[dict[str, Any]], out: str | Path, **kwargs: Any) -> dict[str, Any]:
    episodes = build_episodes(events, **kwargs)
    path = Path(out)
    if path.exists():
        path.unlink()
    for ep in episodes:
        append_jsonl(path, ep)
    stats = {
        "episodes": len(episodes),
        "positive": sum(1 for e in episodes if e["success"]),
        "relabeled": sum(1 for e in episodes if e.get("relabeled_from")),
        "from_recovery": sum(1 for e in episodes if e["source"] == "recovery"),
        "by_skill": dict(Counter(e["skill"] for e in episodes)),
        "path": str(path),
    }
    write_json_atomic(path.with_suffix(".stats.json"), stats)
    return stats
