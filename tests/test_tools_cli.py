import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from opengeniemaster.bench import experiments
from opengeniemaster.bench.report import render
from opengeniemaster.cli import main
from opengeniemaster.flywheel import build_episodes, export
from opengeniemaster.harness.orchestrator import GENIEMASTER, ScriptedOrchestrator
from opengeniemaster.replay import replay

from helpers import create_scenario_task, sim


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(argv))
    return code, out.getvalue()


class ReplayFlywheelTest(unittest.TestCase):
    def record(self, scenario="restocking", seed=4):
        h, sc = sim(scenario, seed)
        ScriptedOrchestrator(h, GENIEMASTER).run(create_scenario_task(h, sc).task_id)
        return h, h.events.rows

    def test_replay_reproduces_a_recorded_run(self):
        _, events = self.record()
        report = replay(events, scenario="restocking", seed=4)
        self.assertTrue(report.ok, report.error)
        self.assertGreater(report.executions_replayed, 4)
        self.assertEqual(report.executions_left, 0)

    def test_replay_detects_behaviour_change(self):
        _, events = self.record()
        changed = replace(GENIEMASTER, modalities=("language",), observe_before_pick=False)
        report = replay(events, scenario="restocking", seed=4, config=changed)
        self.assertFalse(report.ok)
        self.assertIn("differs", report.error or "")

    def test_dataset_export(self):
        h, events = self.record("office_supply_search", 1)
        episodes = build_episodes(events, records=h.memory.temporary.records)
        self.assertTrue(episodes)
        self.assertTrue(all(e["skill"] in ("pick", "place", "hang", "handover", "wipe", "spread") for e in episodes))
        positives = [e for e in episodes if e["success"]]
        self.assertTrue(all(e["weight"] > 0 for e in positives))
        contradicted = {r.record_id for r in h.memory.temporary.records if r.contradicted and r.success}
        rec_ids = {ev["data"]["record_id"]: ev["seq"] for ev in events if ev["type"] == "execute"}
        kept = {e["episode_id"] for e in positives}
        for rid in contradicted:
            self.assertNotIn(f"ep-{rec_ids[rid]:06d}", kept)
        with tempfile.TemporaryDirectory() as d:
            stats = export(events, Path(d) / "eps.jsonl", records=h.memory.temporary.records)
            self.assertEqual(stats["episodes"], len(episodes))
            self.assertTrue((Path(d) / "eps.stats.json").exists())


class CLITest(unittest.TestCase):
    def test_scenarios_and_schema(self):
        code, out = run_cli("scenarios")
        self.assertEqual(code, 0)
        self.assertIn("restocking", out)
        code, out = run_cli("schema")
        self.assertEqual(json.loads(out)["protocol"], "open-eep/0.1")

    def test_demo_persists_memory_and_memory_commands(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("demo", "--scenario", "office_supply_search", "--rounds", "2", "--trials", "4", "--state-dir", d)
            self.assertEqual(code, 0)
            self.assertIn("round 2", out)
            code, out = run_cli("memory", "list", "--state-dir", d)
            listing = json.loads(out)
            self.assertGreater(listing["stats"]["long_term_entries"], 0)
            code, out = run_cli("memory", "history", "--state-dir", d)
            self.assertTrue(json.loads(out))
            code, out = run_cli("memory", "rollback", "--state-dir", d, "--to-version", "0")
            self.assertEqual(json.loads(out)["entries"], 0)
            code, out = run_cli("reflect", "--state-dir", d, "--dry-run")
            self.assertEqual(code, 0)

    def test_bench_writes_reports(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("bench", "--experiment", "capabilities", "--trials", "3", "--out", d)
            self.assertEqual(code, 0)
            self.assertTrue((Path(d) / "capabilities.json").exists())
            self.assertIn("Capability composition", out)


class BenchTest(unittest.TestCase):
    def test_experiments_produce_renderable_results(self):
        results = [
            experiments.overall(trials=2, warmup=1, tasks=["restocking", "cleaning"]),
            experiments.capabilities(trials=2),
            experiments.generalization(trials=2, scales=(10, 1000)),
            experiments.memory_rounds(rounds=2, trials=2),
            experiments.deployment(hours=1.0),
        ]
        for r in results:
            md = render(r)
            self.assertIn("Simulation results", md)
        overall = results[0]
        self.assertEqual(set(overall["overall"]), {"vanilla_vla", "dual_system", "geniemaster_cold", "geniemaster", "oracle"})
        self.assertGreater(results[4]["orders"], 0)


if __name__ == "__main__":
    unittest.main()
