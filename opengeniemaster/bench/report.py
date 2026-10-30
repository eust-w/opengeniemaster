"""Markdown rendering of benchmark results."""

from __future__ import annotations

from typing import Any

from .methods import METHODS

DISCLAIMER = (
    "> Simulation results from the semantic simulator (`opengeniemaster.sim`). They check that the harness mechanisms work "
    "under the simulator's documented assumptions (`sim/policy.py`); they are not physical-robot results and are not "
    "comparable to the GenieMaster paper's numbers."
)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_fmt(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def render(result: dict[str, Any]) -> str:
    kind = result.get("experiment")
    fn = {"overall": _overall, "capabilities": _capabilities, "generalization": _generalization, "memory": _memory,
          "deployment": _deployment}.get(kind)
    if fn is None:
        raise ValueError(f"unknown experiment result '{kind}'")
    return fn(result)


def _label(name: str) -> str:
    return METHODS[name].label if name in METHODS else name


def _overall(r: dict[str, Any]) -> str:
    methods = r["methods"]
    headers = ["Task"] + [f"{_label(m)} SR / TC" for m in methods]
    rows = []
    for task in r["tasks"]:
        rows.append([task] + [f"{r['table'][task][m]['sr']:.2f} / {r['table'][task][m]['tc']:.2f}" for m in methods])
    rows.append(["**Overall**"] + [f"**{r['overall'][m]['sr']:.2f} / {r['overall'][m]['tc']:.2f}**" for m in methods])
    extra = []
    for task in r["tasks"]:
        cells = [task]
        for m in methods:
            s = r["table"][task][m]
            cells.append(f"{s['time_s']:.0f}s, {s['recoveries']:.1f} rec")
        extra.append(cells)
    return "\n\n".join([
        f"### Overall end-to-end performance ({r['trials']} trials per task and method; memory warm-up {r['warmup']} trials)",
        DISCLAIMER,
        _table(headers, rows),
        "Mean time per trial and recoveries per trial:",
        _table(["Task"] + [_label(m) for m in methods], extra),
    ])


def _capabilities(r: dict[str, Any]) -> str:
    rows = [[row["config"], row["sr"], row["recoveries"]] for row in r["rows"]]
    return "\n\n".join([
        f"### Capability composition ({r['trials']} trials per configuration, fixed-location retrieval)",
        DISCLAIMER,
        _table(["Configuration (cumulative)", "SR %", "Recoveries / trial"], rows),
    ])


def _generalization(r: dict[str, Any]) -> str:
    rows = [[row["training_skus"], row["split"], row["policy"], row["sr"]] for row in r["rows"]]
    return "\n\n".join([
        f"### Unseen-product generalization ({r['trials']} trials per cell)",
        DISCLAIMER,
        _table(["Training SKUs", "Split", "Policy guidance", "SR %"], rows),
    ])


def _memory(r: dict[str, Any]) -> str:
    rows = [[row["round"], row["entries_used"], row["sr"], row["control_sr"], row["recoveries"], row["control_recoveries"],
             row["time_s"], row["control_time_s"]] for row in r["rows"]]
    mem = [[e["kind"], e["scope"], e["text"], e["support"]] for e in r.get("final_memory", [])]
    return "\n\n".join([
        f"### Learning from experience ({r['task']}, {r['trials_per_round']} trials per round, budget={r['budget']})",
        DISCLAIMER,
        _table(["Round", "Memory entries in use", "SR % (memory)", "SR % (no memory)", "Recoveries (memory)", "Recoveries (no memory)",
                "Time s (memory)", "Time s (no memory)"], rows),
        "Long-term memory after the last round:",
        _table(["Kind", "Scope", "Instruction", "Support"], mem) if mem else "(empty)",
    ])


def _deployment(r: dict[str, Any]) -> str:
    rows = [
        ["Simulated hours", r["hours"]],
        ["Robots", ", ".join(r["robots"])],
        ["Customer orders", r["orders"]],
        ["Orders delivered correctly", f"{r['orders_correct']} ({_fmt(r['order_success_rate'])}%)"],
        ["Mean order latency (s)", r["mean_order_latency_s"]],
        ["Restock tasks (correct)", f"{r['restock_tasks']} ({r['restocks_correct']})"],
        ["Practice tasks in idle time", r["practice_tasks"]],
        ["Tasks interrupted by service outages and resumed", r["interruptions_resumed"]],
        ["Handoffs", r["handoffs"]],
        ["Safety violations", r["safety_violations"]],
        ["Executions", r["executions"]],
    ]
    refl = [[x["t_h"], len(x["admitted"]), x["entries"]] for x in r.get("reflections", [])]
    return "\n\n".join([
        "### Persistent multi-robot deployment",
        DISCLAIMER,
        _table(["Metric", "Value"], rows),
        "Reflection cycles:",
        _table(["Hour", "Admitted", "Entries"], refl) if refl else "(none)",
    ])
