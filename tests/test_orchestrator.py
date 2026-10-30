import unittest
from dataclasses import replace

from opengeniemaster.bench.methods import METHODS, Method, run_trial
from opengeniemaster.harness.admission import SharedAdmission
from opengeniemaster.harness.orchestrator import DUAL_SYSTEM, GENIEMASTER, VANILLA_VLA, ScriptedOrchestrator
from opengeniemaster.harness.practice import capability_gaps, plan_practice
from opengeniemaster.memory.reflection import RuleReflector, reflect
from opengeniemaster.memory.store import MemoryStore
from opengeniemaster.sim.scenes import BENCH_TASKS

from helpers import create_scenario_task, sim


def success_rate(task, method, seeds, **kw):
    return sum(run_trial(task, s, method, **kw).success for s in seeds) / len(seeds)


class OrchestratorTest(unittest.TestCase):
    def test_every_benchmark_task_runs_to_a_terminal_state(self):
        for name in BENCH_TASKS:
            for cfg in (GENIEMASTER, DUAL_SYSTEM, VANILLA_VLA):
                with self.subTest(task=name, method=cfg.name):
                    h, sc = sim(name, 2, admission=SharedAdmission(safety_interlock=cfg is GENIEMASTER))
                    task = create_scenario_task(h, sc)
                    res = ScriptedOrchestrator(h, cfg).run(task.task_id)
                    self.assertIn(res.status, ("completed", "failed"))
                    self.assertGreater(res.tool_calls, 0)

    def test_harness_beats_baselines(self):
        seeds = range(12)
        for task in ("restocking", "item_retrieval", "office_supply_search"):
            with self.subTest(task=task):
                gm = success_rate(task, METHODS["geniemaster_cold"], seeds)
                dual = success_rate(task, METHODS["dual_system"], seeds)
                vanilla = success_rate(task, METHODS["vanilla_vla"], seeds)
                self.assertGreaterEqual(gm, 0.6)
                self.assertGreater(gm, dual + 0.3)
                self.assertGreater(gm, vanilla + 0.3)

    def test_multi_robot_parallelism_shortens_makespan(self):
        single = replace(GENIEMASTER, multi_robot=False)
        times_multi, times_single = [], []
        for seed in range(6):
            h, sc = sim("restocking", seed, outage_rate=0.0, intrusion_rate=0.0)
            times_multi.append(ScriptedOrchestrator(h, GENIEMASTER).run(create_scenario_task(h, sc).task_id).makespan_s)
            h, sc = sim("restocking", seed, outage_rate=0.0, intrusion_rate=0.0)
            times_single.append(ScriptedOrchestrator(h, single).run(create_scenario_task(h, sc).task_id).makespan_s)
        self.assertLess(sum(times_multi), 0.7 * sum(times_single))

    def test_service_outage_is_resumed_from_continuity(self):
        for seed in range(40):
            h, sc = sim("restocking", seed, outage_rate=1.0, intrusion_rate=0.0)
            task = create_scenario_task(h, sc)
            res = ScriptedOrchestrator(h, GENIEMASTER).run(task.task_id)
            outage_hits = [r for r in h.memory.temporary.records if r.failure_code == "service_unavailable"]
            if outage_hits:
                self.assertTrue(any("wait_for_service" in r or "handoff" in r for r in res.recoveries), res.recoveries)
                self.assertTrue(any("resumed" in n or "handoff" in n for n in res.notes), res.notes)
                return
        self.fail("no scenario hit a service outage")

    def test_memory_learning_improves_first_time_success(self):
        lean = replace(GENIEMASTER, max_attempts=2, max_restarts=0)
        learner = Method("learner", "learner", lean, uses_memory=True)
        cold = Method("cold", "cold", replace(lean, use_memory=False))
        memory = MemoryStore()
        for seed in range(500, 520):
            run_trial("office_supply_search", seed, learner, memory=memory)
            if seed % 5 == 4:
                reflect(memory, SharedAdmission(), RuleReflector())
        self.assertGreaterEqual(len(memory.long_term.admitted()), 3)
        seeds = range(600, 624)
        with_mem = sum(run_trial("office_supply_search", s, learner, memory=memory).success for s in seeds)
        without = sum(run_trial("office_supply_search", s, cold).success for s in seeds)
        self.assertGreater(with_mem, without)

    def test_capability_gaps_and_practice(self):
        h, sc = sim("restocking", 0)
        for seed in range(3):
            h2, sc2 = sim("restocking", seed, memory=h.memory)
            ScriptedOrchestrator(h2, GENIEMASTER).run(create_scenario_task(h2, sc2).task_id)
        gaps = capability_gaps(h)
        self.assertTrue(gaps)
        practicable = next((g for g in gaps if g.skill in ("pick", "hang", "place") and g.example_args.get("sku")), None)
        if practicable is None:
            self.skipTest("no practicable gap in this sample")
        tid = plan_practice(h, practicable)
        self.assertIsNotNone(tid)
        task = h.get_task(tid)
        self.assertEqual(task.priority, -1)
        self.assertTrue(any(s.skill == "pick" for s in task.steps))


if __name__ == "__main__":
    unittest.main()
