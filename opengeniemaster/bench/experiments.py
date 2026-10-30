"""Simulation experiments mirroring the evaluations reported for GenieMaster.

Every experiment runs on the semantic simulator. The numbers they produce
test the *mechanics* of this implementation (does the harness exploit
guidance, recover, coordinate and learn as designed?) under the simulator's
documented assumptions. They are not comparable to the paper's physical
results and must not be presented as such.
"""

from __future__ import annotations

import random
from dataclasses import replace
from statistics import mean
from typing import Any, Callable

from ..embodiment.profiles import CAPABILITY_LADDER
from ..harness.admission import SharedAdmission
from ..harness.orchestrator import GENIEMASTER, ScriptedOrchestrator
from ..harness.practice import capability_gaps, plan_practice
from ..memory.reflection import RuleReflector, reflect
from ..memory.store import MemoryStore
from ..runtime import harness_from_scenario
from ..sim.scenes import BENCH_TASKS, build
from .methods import METHODS, Method, TrialResult, run_trial, summarize, warm_up

Progress = Callable[[str], None] | None


def _say(progress: Progress, msg: str) -> None:
    if progress:
        progress(msg)


# ---------------------------------------------------------------------------
# E1: overall end-to-end performance (six tasks x methods)
# ---------------------------------------------------------------------------


def overall(trials: int = 100, *, seed: int = 0, tasks: list[str] | None = None, methods: list[str] | None = None,
            warmup: int = 10, progress: Progress = None) -> dict[str, Any]:
    tasks = tasks or list(BENCH_TASKS)
    methods = methods or ["vanilla_vla", "dual_system", "geniemaster_cold", "geniemaster", "oracle"]
    table: dict[str, dict[str, Any]] = {}
    per_trial: list[dict[str, Any]] = []
    for task in tasks:
        table[task] = {}
        for name in methods:
            method = METHODS[name]
            memory = MemoryStore() if method.uses_memory else None
            learned = 0
            if memory is not None and warmup:
                warm_up(task, method, memory, trials=warmup)
                learned = len(memory.long_term.admitted())
            results = [run_trial(task, seed + i, method, memory=memory) for i in range(trials)]
            summary = summarize(results)
            if memory is not None:
                summary["memory_entries"] = learned
            table[task][name] = summary
            per_trial += [r.to_dict() for r in results]
            _say(progress, f"overall {task:22s} {name:17s} SR={summary['sr']:6.2f} TC={summary['tc']:6.2f}")
    overall_row = {}
    for name in methods:
        rows = [table[t][name] for t in tasks]
        overall_row[name] = {"sr": round(mean(r["sr"] for r in rows), 2), "tc": round(mean(r["tc"] for r in rows), 2)}
    return {"experiment": "overall", "trials": trials, "warmup": warmup, "seed": seed, "methods": methods, "tasks": tasks,
            "table": table, "overall": overall_row, "trials_detail": per_trial}


# ---------------------------------------------------------------------------
# E2: capability composition C1..C5 (fixed-location retrieval)
# ---------------------------------------------------------------------------


def capabilities(trials: int = 100, *, seed: int = 0, progress: Progress = None) -> dict[str, Any]:
    rows = []
    method = METHODS["geniemaster_cold"]
    for label, features in CAPABILITY_LADDER:
        kwargs = dict(n_items=1, fixed_location=True, back_row_rate=0.35, novel_ratio=0.3, intrusion_rate=0.0, outage_rate=0.0,
                      deliver_by="place", fleet=(("wh-01", "wheeled_humanoid"),), robot_features={"wh-01": set(features)})
        results = [run_trial("item_retrieval", seed + i, method, scenario_kwargs=kwargs) for i in range(trials)]
        s = summarize(results)
        rows.append({"config": label, "features": sorted(features), **s})
        _say(progress, f"capabilities {label:32s} SR={s['sr']:6.2f}")
    return {"experiment": "capabilities", "trials": trials, "seed": seed, "rows": rows}


# ---------------------------------------------------------------------------
# E3: novel-product generalization through in-context guidance
# ---------------------------------------------------------------------------


def generalization(trials: int = 25, *, seed: int = 0, scales: tuple[int, ...] = (10, 30, 1000), progress: Progress = None) -> dict[str, Any]:
    text_only = replace(GENIEMASTER, name="text_only", use_memory=False, modalities=("language", "detailed"), max_attempts=1, recover=False,
                        observe_before_pick=False, verify_final=False)
    guided = replace(text_only, name="eep_guided", modalities=("language", "detailed", "reference"))
    method = Method("generalization", "generalization", text_only)
    rows = []
    for scale in scales:
        for split, flag in (("seen", {"seen_only": True}), ("novel", {"novel_only": True})):
            for cfg in (text_only, guided):
                kwargs = dict(n_items=1, fixed_location=True, training_scale=scale, intrusion_rate=0.0, outage_rate=0.0, deliver_by="place",
                              require_group=False, flaws={}, **flag)
                results = [run_trial("item_retrieval", seed + i, method, scenario_kwargs=kwargs, config=cfg) for i in range(trials)]
                s = summarize(results)
                rows.append({"training_skus": scale, "split": split, "policy": cfg.name, "sr": s["sr"], "n": s["n"]})
                _say(progress, f"generalization scale={scale:5d} {split:5s} {cfg.name:10s} SR={s['sr']:6.2f}")
    return {"experiment": "generalization", "trials": trials, "seed": seed, "rows": rows}


# ---------------------------------------------------------------------------
# E4: learning from experience (memory over rounds)
# ---------------------------------------------------------------------------


def memory_rounds(rounds: int = 5, trials: int = 10, *, seed: int = 0, task: str = "office_supply_search", budget: str = "lean",
                  progress: Progress = None, reflector_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Accumulated memory vs. an identical harness without memory, on the same seeds.

    ``budget="lean"`` allows two attempts per step and no work-item restarts,
    which measures whether lessons make execution right the first time;
    ``budget="default"`` uses the full recovery playbook, where memory mostly
    shows up as fewer recoveries and less time.
    """
    base = replace(GENIEMASTER, max_attempts=2, max_restarts=0) if budget == "lean" else GENIEMASTER
    method = Method("geniemaster", "OpenGenieMaster", base, uses_memory=True)
    control = Method("geniemaster_cold", "OpenGenieMaster (no memory)", replace(base, use_memory=False))
    memory = MemoryStore()
    admission = SharedAdmission()
    rows = []
    for r in range(1, rounds + 1):
        seeds = [seed + 1000 * r + i for i in range(trials)]
        with_mem = [run_trial(task, s, method, memory=memory) for s in seeds]
        without = [run_trial(task, s, control) for s in seeds]
        entries_used = len(memory.long_term.admitted())
        rep = reflect(memory, admission, reflector_factory() if reflector_factory else RuleReflector(min_support=admission.min_support))
        a, b = summarize(with_mem), summarize(without)
        rows.append({
            "round": r,
            "entries_used": entries_used,
            "sr": a["sr"],
            "control_sr": b["sr"],
            "recoveries": a["recoveries"],
            "control_recoveries": b["recoveries"],
            "time_s": a["time_s"],
            "control_time_s": b["time_s"],
            "calls_per_success": a["calls_per_success"],
            "detector_error_rate": a["detector_error_rate"],
            "control_detector_error_rate": b["detector_error_rate"],
            "admitted_after": rep.admitted,
        })
        _say(progress, f"memory round {r}: SR={a['sr']:6.2f} (control {b['sr']:6.2f}) recoveries={a['recoveries']:.2f} "
                       f"(control {b['recoveries']:.2f}) entries used={entries_used}")
    return {
        "experiment": "memory",
        "task": task,
        "budget": budget,
        "rounds": rounds,
        "trials_per_round": trials,
        "seed": seed,
        "rows": rows,
        "final_memory": [e.brief() for e in memory.long_term.admitted()],
    }


# ---------------------------------------------------------------------------
# E5: persistent multi-robot deployment
# ---------------------------------------------------------------------------


def deployment(hours: float = 24.0, *, seed: int = 0, orders_per_hour: float = 6.0, inspect_every_min: float = 30.0,
               reflect_every_h: float = 4.0, practice: bool = True, progress: Progress = None) -> dict[str, Any]:
    scenario = build("store_deployment", seed, hours=hours)
    world = scenario.world
    harness = harness_from_scenario(scenario, memory=MemoryStore(), seed=seed)
    orch = ScriptedOrchestrator(harness, GENIEMASTER)
    rng = random.Random(seed + 7)
    records = scenario.records
    sellable = sorted(s for s, it in records.catalog.items() if it.home)
    horizon = hours * 3600.0
    arrivals: list[tuple[float, str, dict[str, Any]]] = []
    t = 0.0
    while True:
        t += rng.expovariate(orders_per_hour / 3600.0)
        if t >= horizon:
            break
        arrivals.append((t, "order", {"n": rng.choice([1, 1, 2])}))
    k = 1
    while k * inspect_every_min * 60.0 < horizon:
        arrivals.append((k * inspect_every_min * 60.0, "inspection", {}))
        k += 1
    arrivals.sort(key=lambda a: a[0])

    orders: dict[str, dict[str, Any]] = {}
    restocks: dict[str, dict[str, Any]] = {}
    practice_tasks: list[str] = []
    reflections = []
    next_reflect = reflect_every_h * 3600.0
    idx = 0
    now = 0.0
    tick = 60.0
    pending_restock: set[str] = set()
    reserved: set[str] = set()

    def in_stock(sku: str) -> bool:
        item = records.catalog[sku]
        return any(th.sku == sku and th.location == item.home for th in world.things.values())

    used_deliveries: set[int] = set()

    def settle() -> None:
        """Grade tasks against ground truth at the moment they finish."""
        for tid, info in orders.items():
            status = harness.get_task(tid).status.value
            if info["accounted"] or status not in ("completed", "failed"):
                continue
            info["accounted"] = True
            ok = status == "completed"
            for sku in info["items"]:
                match = next((i for i, d in enumerate(world.deliveries)
                              if i not in used_deliveries and d["sku"] == sku and d["t"] >= info["arrival"]), None)
                if match is None:
                    ok = False
                else:
                    used_deliveries.add(match)
            info["correct"] = ok
            pending_restock.update(info["items"])
            reserved.difference_update(info["items"])
        for tid, info in restocks.items():
            status = harness.get_task(tid).status.value
            if "correct" in info or status not in ("completed", "failed"):
                continue
            info["correct"] = status == "completed" and all(
                any(th.sku == s and th.location == records.catalog[s].home and th.slot == records.catalog[s].slot
                    for th in world.things.values())
                for s in info["items"])

    max_practice = int(hours * 2)
    last_practice = -1e9

    def sold_out() -> list[str]:
        # Business record view: items sold since the last restock (the POS knows what left the shelf).
        return sorted(pending_restock)

    while now < horizon:
        for r in harness.fleet:  # idle robots wait until the current time before taking new work
            if harness.now(r.robot_id) < now:
                r.adapter.wait(r, now - harness.now(r.robot_id))
        while idx < len(arrivals) and arrivals[idx][0] <= now:
            at, kind, data = arrivals[idx]
            idx += 1
            if kind == "order":
                # The inventory system only offers what is on the shelf and not reserved by an open order.
                on_shelf = [s for s in sellable if in_stock(s) and s not in reserved]
                if not on_shelf:
                    continue
                data = {"items": rng.sample(on_shelf, min(data["n"], len(on_shelf)))}
                reserved.update(data["items"])
                task = harness.create_task(f"Customer order: {', '.join(data['items'])}", domain="store", workflow="retrieve",
                                           params={"items": data["items"], "deliver_to": "counter"}, priority=2)
                orders[task.task_id] = {"arrival": at, "items": data["items"], "accounted": False}
            else:
                todo = [s for s in sold_out() if any(th.sku == s and th.location == "storage" for th in world.things.values())]
                if todo:
                    task = harness.create_task(f"Restock after inspection: {', '.join(todo)}", domain="store", workflow="restock",
                                               params={"items": todo}, priority=1)
                    restocks[task.task_id] = {"arrival": at, "items": todo}
                    pending_restock.difference_update(todo)
        progressed = True
        while progressed:
            progressed = orch.run_ready(until=now + tick)
        settle()
        busy = any(t.status.value in ("active", "interrupted") for t in harness.tasks.list())
        if practice and not busy and len(practice_tasks) < max_practice and now - last_practice >= 900.0:
            gaps = capability_gaps(harness)
            if gaps:
                tid = plan_practice(harness, gaps[0], available=lambda sku: in_stock(sku) and sku not in reserved)
                if tid:
                    practice_tasks.append(tid)
                    last_practice = now
        if now >= next_reflect:
            rep = harness.reflect()
            reflections.append({"t_h": round(now / 3600.0, 2), "admitted": rep.admitted, "entries": len(harness.memory.long_term.admitted())})
            _say(progress, f"deployment t={now / 3600.0:5.1f}h reflection admitted {len(rep.admitted)} entries")
            next_reflect += reflect_every_h * 3600.0
        now += tick
    # drain remaining work (no new arrivals)
    while orch.run_ready():
        settle()
    settle()

    def latency(task_id: str, arrival: float) -> float | None:
        task = harness.get_task(task_id)
        ends = [s.finished_at for s in task.steps if s.finished_at is not None]
        return (max(ends) - arrival) if ends and task.status.value == "completed" else None

    order_ok = sum(1 for info in orders.values() if info.get("correct"))
    lat = [l for tid, info in orders.items() if (l := latency(tid, info["arrival"])) is not None]
    restock_ok = sum(1 for info in restocks.values() if info.get("correct"))
    statuses: dict[str, int] = {}
    for task in harness.tasks.list():
        statuses[task.status.value] = statuses.get(task.status.value, 0) + 1
    notes = [n for task in harness.tasks.list() for n in task.notes]
    return {
        "experiment": "deployment",
        "hours": hours,
        "seed": seed,
        "robots": [r.robot_id for r in harness.fleet],
        "orders": len(orders),
        "orders_correct": order_ok,
        "order_success_rate": round(100.0 * order_ok / len(orders), 2) if orders else None,
        "mean_order_latency_s": round(mean(lat), 1) if lat else None,
        "restock_tasks": len(restocks),
        "restocks_correct": restock_ok,
        "practice_tasks": len(practice_tasks),
        "task_status": statuses,
        "interruptions_resumed": sum(1 for n in notes if "service unavailable" in n),
        "handoffs": sum(1 for n in notes if n.startswith("handoff")),
        "safety_violations": len(world.safety_violations),
        "reflections": reflections,
        "memory_entries": [e.brief() for e in harness.memory.long_term.admitted()],
        "executions": harness.counters["execute"],
    }


EXPERIMENTS: dict[str, Callable[..., dict[str, Any]]] = {
    "overall": overall,
    "capabilities": capabilities,
    "generalization": generalization,
    "memory": memory_rounds,
    "deployment": deployment,
}

__all__ = ["EXPERIMENTS", "TrialResult", "capabilities", "deployment", "generalization", "memory_rounds", "overall"]
