"""Command line interface: ``ogm`` / ``python -m opengeniemaster``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import __version__

DEFAULT_STATE = ".ogm"


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _state(args: argparse.Namespace) -> Path | None:
    return Path(args.state_dir) if getattr(args, "state_dir", None) else None


def _sim_harness(args: argparse.Namespace):
    from .harness.events import EventLog
    from .runtime import harness_from_scenario
    from .sim.scenes import build

    scenario = build(args.scenario, args.seed)
    state = _state(args)
    events = EventLog(state / "events.jsonl") if state else EventLog()
    return harness_from_scenario(scenario, state_dir=state, seed=None if state else args.seed, events=events), scenario


# --------------------------------------------------------------------------- commands


def cmd_scenarios(args: argparse.Namespace) -> int:
    from .sim.scenes import SCENARIOS, build

    for name in sorted(SCENARIOS):
        sc = build(name, 0)
        print(f"{name:22s} domain={sc.domain:7s} robots={','.join(r for r, _ in sc.fleet):24s} "
              f"task={sc.tasks[0].goal if sc.tasks else '(dynamic orders)'}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .harness.orchestrator import GENIEMASTER, ScriptedOrchestrator
    from .memory.store import MemoryStore
    from .runtime import harness_from_scenario
    from .sim.scenes import build

    memory = MemoryStore(Path(args.state_dir) / "memory") if args.state_dir else MemoryStore()
    print(f"OpenGenieMaster {__version__} demo: scenario={args.scenario}, rounds={args.rounds}, trials/round={args.trials}")
    print("Each round runs the scenario with the scripted GenieMaster-style agent, then reflects into long-term memory.\n")
    for rnd in range(1, args.rounds + 1):
        ok = 0
        recoveries = 0
        for i in range(args.trials):
            seed = args.seed + 100 * rnd + i
            sc = build(args.scenario, seed)
            h = harness_from_scenario(sc, memory=memory, seed=seed)
            for spec in sc.tasks:
                task = h.create_task(spec.goal, domain=spec.domain, workflow=spec.workflow, params=spec.params)
                res = ScriptedOrchestrator(h, GENIEMASTER).run(task.task_id)
                recoveries += len(res.recoveries)
                if args.verbose:
                    print(f"  [round {rnd} trial {i}] {res.status:9s} steps {res.steps_succeeded}/{res.steps_total} "
                          f"time {res.makespan_s:6.1f}s recoveries {res.recoveries}")
            ok += int(sc.grade().success)
        report = h.reflect()
        print(f"round {rnd}: success {ok}/{args.trials}, recoveries {recoveries}, memory admitted {len(report.admitted)} "
              f"-> {len(memory.long_term.admitted())} entries")
    print("\nLong-term memory:")
    for e in memory.long_term.admitted():
        print(f"  [{e.scope}/{e.kind}] {e.text}  (when={e.when}, support={len(e.evidence)})")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .agents.mcp_server import MCPServer

    logging.basicConfig(stream=sys.stderr, level=getattr(logging, args.log_level.upper(), logging.WARNING))
    if args.fleet:
        from .runtime import harness_from_fleet_file

        harness = harness_from_fleet_file(args.fleet, state_dir=_state(args))
    else:
        harness, _ = _sim_harness(args)
    MCPServer(harness).serve()
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from .agents.llm_agent import ClaudeAgent

    harness, scenario = _sim_harness(args)
    goal = args.goal or (scenario.tasks[0].goal if scenario.tasks else "Inspect the store and report what needs restocking.")

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "tool_use":
            print(f"-> {data['name']} {json.dumps(data['input'], ensure_ascii=False)[:160]}", file=sys.stderr)
        elif kind == "refusal":
            print(f"!! refusal ({data.get('category')})", file=sys.stderr)

    agent = ClaudeAgent(harness, model=args.model, effort=args.effort, max_turns=args.max_turns, on_event=on_event)
    run = agent.run(goal)
    print(run.final_text)
    grade = scenario.grade()
    print(f"\n[ground truth] success={grade.success} completion={grade.completion:.2f} tool_calls={run.tool_calls}", file=sys.stderr)
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import experiments
    from .bench.report import render

    out = Path(args.out) if args.out else None
    names = list(experiments.EXPERIMENTS) if args.experiment == "all" else [args.experiment]
    progress = (lambda m: print(m, file=sys.stderr)) if args.verbose else None
    sections = []
    for name in names:
        kwargs: dict[str, Any] = {"seed": args.seed, "progress": progress}
        if args.trials and name in ("overall", "capabilities", "generalization", "memory"):
            kwargs["trials"] = args.trials
        if name == "deployment" and args.hours:
            kwargs["hours"] = args.hours
        result = experiments.EXPERIMENTS[name](**kwargs)
        md = render(result)
        sections.append(md)
        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            (out / f"{name}.md").write_text(md + "\n", encoding="utf-8")
        print(md + "\n")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    from .memory.store import MemoryStore

    if not args.state_dir:
        print("--state-dir is required", file=sys.stderr)
        return 2
    mem = MemoryStore(Path(args.state_dir) / "memory")
    lt = mem.long_term
    if args.action in ("show", "retire"):
        if not args.entry_id:
            print(f"memory {args.action} needs --entry-id (see `ogm memory list`)", file=sys.stderr)
            return 2
        if args.entry_id not in lt.entries:
            print(f"unknown entry '{args.entry_id}'", file=sys.stderr)
            return 2
    if args.action == "rollback" and args.to_version is None:
        print("memory rollback needs --to-version (see `ogm memory history`)", file=sys.stderr)
        return 2
    try:
        if args.action == "list":
            _print({"version": lt.version, "stats": mem.stats(), "entries": [e.brief() for e in lt.admitted()]})
        elif args.action == "show":
            _print(lt.entries[args.entry_id].to_dict())
        elif args.action == "history":
            _print(lt.history())
        elif args.action == "diff":
            _print(lt.diff(args.from_version, args.to_version if args.to_version is not None else lt.version))
        elif args.action == "rollback":
            lt.rollback(args.to_version)
            _print({"version": lt.version, "entries": len(lt.admitted())})
        elif args.action == "retire":
            lt.retire(args.entry_id, reason=args.reason or "retired by operator")
            _print({"version": lt.version, "retired": args.entry_id})
        elif args.action == "records":
            _print([r.brief() for r in mem.temporary.records[-args.limit:]])
    except KeyError as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    from .harness.admission import SharedAdmission
    from .memory.reflection import LLMReflector, RuleReflector, reflect
    from .memory.store import MemoryStore

    if not args.state_dir:
        print("--state-dir is required", file=sys.stderr)
        return 2
    mem = MemoryStore(Path(args.state_dir) / "memory")
    admission = SharedAdmission(min_support=args.min_support)
    if args.llm:
        from .agents.llm_agent import ClaudeTextModel

        reflector: Any = LLMReflector(ClaudeTextModel(model=args.model))
    else:
        reflector = RuleReflector(min_support=args.min_support)
    _print(reflect(mem, admission, reflector, dry_run=args.dry_run).to_dict())
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .harness.events import EventLog
    from .replay import replay

    report = replay(EventLog.load(args.events), scenario=args.scenario, seed=args.seed, memory_dir=args.memory_dir)
    _print(report.to_dict())
    return 0 if report.ok else 1


def cmd_dataset(args: argparse.Namespace) -> int:
    from .flywheel import export
    from .harness.events import EventLog
    from .memory.store import TemporaryMemory

    records = TemporaryMemory(Path(args.memory_dir)).records if args.memory_dir else []
    _print(export(EventLog.load(args.events), args.out, records=records, include_failures=not args.positives_only))
    return 0


def cmd_robot_service(args: argparse.Namespace) -> int:
    from .embodiment.service import RobotService, SimBackend, serve
    from .sim.scenes import build

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    sc = build(args.scenario, args.seed)
    if args.robot not in sc.world.robots:
        print(f"unknown robot {args.robot}; choose from {sorted(sc.world.robots)}", file=sys.stderr)
        return 2
    serve(RobotService(SimBackend(sc.world, args.robot), token=args.token), host=args.host, port=args.port)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    from .eep import types
    from .eep.schema import schema_for

    names = ["ExecutionRequest", "Guidance", "Feedback", "ContinuityState", "CapabilityContract", "Observation", "FailureReason",
             "StepTemplate"]
    schemas = {n: schema_for(getattr(types, n)) for n in names}
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for n, s in schemas.items():
            (out / f"{n}.schema.json").write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {len(schemas)} schemas to {out}")
    else:
        _print({"protocol": types.PROTOCOL_VERSION, "schemas": schemas})
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ogm", description="OpenGenieMaster: open embodied continual harness (EEP over MCP).")
    p.add_argument("--version", action="version", version=f"opengeniemaster {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def sim_opts(sp: argparse.ArgumentParser, default: str = "restocking") -> None:
        sp.add_argument("--scenario", default=default, help="simulated scenario (see `ogm scenarios`)")
        sp.add_argument("--seed", type=int, default=0)
        sp.add_argument("--state-dir", default=None, help="persist memory, tasks and events here (e.g. .ogm)")

    sp = sub.add_parser("scenarios", help="list simulated scenarios")
    sp.set_defaults(func=cmd_scenarios)

    sp = sub.add_parser("demo", help="run rounds of a scenario with reflection between rounds")
    sim_opts(sp)
    sp.add_argument("--rounds", type=int, default=3)
    sp.add_argument("--trials", type=int, default=5)
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("mcp", help="serve EEP as an MCP server over stdio (for Claude Code, Codex, ...)")
    sim_opts(sp)
    sp.add_argument("--fleet", default=None, help="JSON fleet file for real robots (HTTP execution services)")
    sp.add_argument("--log-level", default="warning")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("agent", help="run the built-in Claude agent on a simulated scenario (needs the anthropic SDK)")
    sim_opts(sp)
    sp.add_argument("goal", nargs="?", default=None)
    sp.add_argument("--model", default="claude-opus-5-5")
    sp.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    sp.add_argument("--max-turns", type=int, default=60)
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("bench", help="run simulation experiments")
    sp.add_argument("--experiment", default="overall", choices=["overall", "capabilities", "generalization", "memory", "deployment", "all"])
    sp.add_argument("--trials", type=int, default=None)
    sp.add_argument("--hours", type=float, default=None)
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--out", default=None, help="directory for JSON + Markdown results")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("memory", help="inspect and manage long-term memory")
    sp.add_argument("action", choices=["list", "show", "history", "diff", "rollback", "retire", "records"])
    sp.add_argument("--state-dir", default=DEFAULT_STATE)
    sp.add_argument("--entry-id", default=None)
    sp.add_argument("--from-version", type=int, default=0)
    sp.add_argument("--to-version", type=int, default=None)
    sp.add_argument("--reason", default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_memory)

    sp = sub.add_parser("reflect", help="consolidate pending execution records into long-term memory")
    sp.add_argument("--state-dir", default=DEFAULT_STATE)
    sp.add_argument("--min-support", type=int, default=2)
    sp.add_argument("--llm", action="store_true", help="reflect with Claude instead of the rule reflector")
    sp.add_argument("--model", default="claude-opus-5-5")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_reflect)

    sp = sub.add_parser("replay", help="offline regression test: replay recorded robot responses")
    sp.add_argument("events")
    sp.add_argument("--scenario", default="restocking")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--memory-dir", default=None)
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("dataset", help="export policy-training episodes (filter, weight, relabel) from an event log")
    sp.add_argument("events")
    sp.add_argument("--out", required=True)
    sp.add_argument("--memory-dir", default=None, help="memory directory to drop contradicted successes")
    sp.add_argument("--positives-only", action="store_true")
    sp.set_defaults(func=cmd_dataset)

    sp = sub.add_parser("robot-service", help="serve one simulated robot through the robot-side HTTP contract")
    sp.add_argument("--scenario", default="store_deployment")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--robot", default="wh-01")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--token", default=None)
    sp.set_defaults(func=cmd_robot_service)

    sp = sub.add_parser("schema", help="print or write EEP JSON schemas")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_schema)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
