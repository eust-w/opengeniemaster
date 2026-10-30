import unittest

from opengeniemaster.eep.types import Guidance, SpatialCue, StepStatus, TaskStatus
from opengeniemaster.eep.errors import Conflict, InvalidRequest, NotFound
from opengeniemaster.embodiment.robots import Calibration
from opengeniemaster.harness.admission import SharedAdmission
from opengeniemaster.sim.world import Intrusion

from helpers import create_scenario_task, req, sim


class CapabilityTest(unittest.TestCase):
    def test_capability_contract(self):
        h, _ = sim()
        cap = h.capabilities()
        self.assertEqual(cap["protocol"], "open-eep/0.1")
        self.assertEqual([d["name"] for d in cap["domains"]], ["store"])
        robots = {r["robot_id"]: r for r in cap["robots"]}
        self.assertEqual(set(robots), {"wh-01", "wh-02", "cart-01"})
        skills = {s["name"]: s for s in robots["wh-01"]["skills"]}
        self.assertEqual(skills["pick"]["service"], "policy")
        self.assertEqual(skills["navigate_to"]["service"], "primitive")
        self.assertIn("reference", skills["pick"]["guidance"])
        self.assertIn("trained_skus", robots["wh-01"]["policy"])


class ExecuteTest(unittest.TestCase):
    def test_admission_rejections(self):
        h, sc = sim()
        task = create_scenario_task(h, sc)
        first = task.steps[0]
        # dependency not done
        pick = next(s for s in task.steps if s.skill == "pick")
        fb = h.execute(req("wh-01", "pick", task_id=task.task_id, step_id=pick.step_id))
        self.assertEqual(fb.failure_reason.code, "admission_rejected")
        self.assertIn("dependencies", fb.failure_reason.message)
        # skill mismatch with the step
        fb = h.execute(req("wh-01", "wipe", {"location": "shelf_A"}, task_id=task.task_id, step_id=first.step_id))
        self.assertEqual(fb.failure_reason.code, "admission_rejected")
        # invalid calibration blocks manipulation
        h.fleet.get("wh-02").calibration = Calibration(valid=False)
        fb = h.execute(req("wh-02", "pick", {"sku": "cola-330"}))
        self.assertIn("calibration", fb.failure_reason.message)
        # malformed spatial cue
        fb = h.execute(req("wh-01", "pick", {"sku": "cola-330"}, guidance=Guidance(spatial=[SpatialCue(kind="box", coords=[0.1, 2.0, 0.3, 0.4])])))
        self.assertIn("normalized", fb.failure_reason.message)
        with self.assertRaises(NotFound):
            h.execute(req("ghost", "pick"))

    def test_rejected_request_does_not_touch_task_state(self):
        h, sc = sim()
        task = create_scenario_task(h, sc)
        pick = next(s for s in task.steps if s.skill == "pick")
        h.execute(req("wh-01", "pick", task_id=task.task_id, step_id=pick.step_id))
        self.assertEqual(h.get_task(task.task_id).step(pick.step_id).attempts, 0)

    def test_safety_interlock_requires_pause_when_person_seen(self):
        h, sc = sim("item_retrieval", 0)
        sc.world.intrusions.append(Intrusion("dock", 0.0, 1000.0))
        obs = h.observe("wh-01")
        self.assertIn("person_in_workspace", obs.flags)
        fb = h.execute(req("wh-01", "pick", {"sku": "cola-330"}))
        self.assertEqual(fb.failure_reason.code, "admission_rejected")
        self.assertTrue(any("pause_on_intrusion" in s for s in fb.suggestions))
        fb = h.execute(req("wh-01", "pick", {"sku": "cola-330"}, guidance=Guidance(options={"pause_on_intrusion": True})))
        self.assertNotEqual(fb.failure_reason.code if fb.failure_reason else None, "admission_rejected")
        h2, _ = sim("item_retrieval", 0, admission=SharedAdmission(safety_interlock=False))
        self.assertFalse(h2.admission.safety_interlock)

    def test_criteria_override_policy_success(self):
        h, sc = sim()
        world = sc.world
        h.execute(req("wh-01", "navigate_to", {"location": "shelf_A"}))
        target = next(t for t in world.things.values() if t.sku == "cola-zero-330")
        # force the policy to grab the look-alike by pointing a box at it
        box = world._box(target)
        for _ in range(10):
            fb = h.execute(req("wh-01", "pick", {"sku": "cola-330", "location": "shelf_A"},
                               guidance=Guidance(spatial=[SpatialCue(kind="box", coords=box, role="target")], options={"verify_close_range": True})))
            if fb.policy_success:
                break
            world.robots["wh-01"].holding = None
        if not fb.policy_success:
            self.skipTest("policy never grasped in 10 tries")
        self.assertFalse(fb.success)
        self.assertEqual(fb.failure_reason.code, "wrong_object")
        self.assertTrue(fb.suggestions)

    def test_step_execution_updates_continuity_and_memory(self):
        h, sc = sim()
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)
        step = h.ready_steps(task.task_id)[0]
        fb = h.execute(req(step.assigned_robot, step.skill, task_id=task.task_id, step_id=step.step_id))
        self.assertTrue(fb.success)
        self.assertIn(step.step_id, fb.continuity.completed_steps)
        rec = h.memory.temporary.records[-1]
        self.assertEqual((rec.task_id, rec.step_id, rec.success), (task.task_id, step.step_id, True))
        self.assertTrue(any(e["type"] == "execute" for e in h.events.rows))

    def test_dispatch_respects_groups_and_spreads_work(self):
        h, sc = sim()
        task = create_scenario_task(h, sc)
        assignments = h.dispatch(task.task_id)
        robots = {a["robot_id"] for a in assignments}
        self.assertEqual(len(robots), 3)  # three independent work items -> three robots
        task = h.get_task(task.task_id)
        for s in task.steps:
            group_robot = {x.assigned_robot for x in task.steps if x.group == s.group}
            self.assertEqual(len(group_robot), 1)

    def test_handoff_refuses_when_old_robot_holds_object(self):
        h, sc = sim("item_retrieval", 1)
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)
        task = h.get_task(task.task_id)
        task.held_objects["wh-01"] = "cola-330"
        step = next(s for s in task.steps if s.assigned_robot == "wh-01")
        with self.assertRaises(Conflict):
            h.update_task(task.task_id, "handoff", step_id=step.step_id, robot_id="wh-02")
        h.update_task(task.task_id, "handoff", step_id=step.step_id, robot_id="wh-02", force=True)
        self.assertEqual(h.get_task(task.task_id).step(step.step_id).assigned_robot, "wh-02")

    def test_stop_robot_interrupts_its_tasks(self):
        h, sc = sim()
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)
        rid = h.get_task(task.task_id).steps[0].assigned_robot
        out = h.stop_robot(rid, "test")
        self.assertIn(task.task_id, out["paused_tasks"])
        self.assertEqual(h.get_task(task.task_id).status, TaskStatus.PAUSED)
        fb = h.execute(req(rid, "navigate_to", {"location": "storage"}))
        self.assertEqual(fb.failure_reason.code, "admission_rejected")
        with self.assertRaises(Conflict):  # resuming the task while its robot is stopped needs force
            h.update_task(task.task_id, "resume")
        self.assertIn(task.task_id, h.resume_robot(rid)["paused_tasks"])
        self.assertEqual(h.get_task(task.task_id).status, TaskStatus.PAUSED)  # stays paused until resumed explicitly
        h.update_task(task.task_id, "resume")
        self.assertTrue(h.execute(req(rid, "navigate_to", {"location": "storage"})).success)

    def test_unknown_actions_and_workflows(self):
        h, sc = sim()
        task = create_scenario_task(h, sc)
        with self.assertRaises(InvalidRequest):
            h.update_task(task.task_id, "teleport")
        with self.assertRaises(NotFound):
            h.create_task("x", domain="store", workflow="dance")
        with self.assertRaises(InvalidRequest):
            h.create_task("x", domain="store", workflow="restock", params={"items": [{"nope": 1}]})

    def test_explicit_steps_and_recheck(self):
        h, sc = sim()
        steps = [{"step_id": "go", "skill": "navigate_to", "args": {"location": "shelf_A"}, "group": "g"},
                 {"step_id": "pick", "skill": "pick", "args": {"sku": "cola-330", "location": "shelf_A"}, "depends_on": ["go"], "group": "g"}]
        task = h.create_task("grab a cola", domain="store", steps=steps)
        self.assertEqual(task.step("pick").criteria, ["holding_target"])
        h.dispatch(task.task_id)
        rid = h.get_task(task.task_id).step("go").assigned_robot
        h.execute(req(rid, "navigate_to", task_id=task.task_id, step_id="go"))
        res = h.recheck_step(task.task_id, "go")
        self.assertTrue(res["passed"])
        self.assertEqual(h.get_task(task.task_id).step("go").status, StepStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
