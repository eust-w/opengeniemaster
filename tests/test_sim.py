import unittest

from opengeniemaster.eep.types import ExecutionMode, ExecutionRequest, Guidance, ObjectReference, SpatialCue
from opengeniemaster.sim.policy import PolicyCard, SimParams
from opengeniemaster.sim.scenes import BENCH_TASKS, build
from opengeniemaster.sim.world import Intrusion, Outage, Place, SimRobot, SimWorld, Thing

FEATURES = {"mobile", "dual_arm", "head_camera", "wrist_camera", "head_waist", "arm_ik", "grasp_planning", "guided_policy", "object_reference"}


def tiny_world(seed=0, *, things=(), slot_info=None, intrusions=(), outages=(), flaws=None, trained=("a", "b")):
    places = [Place("dock", "dock", (0, 0)), Place("shelf", "shelf", (3, 0), slots=["s1", "s2", "s3", "s4"],
                                                    slot_info=dict(slot_info or {}))]
    robots = [SimRobot("r1", "wheeled_humanoid", set(FEATURES), "shelf")]
    return SimWorld(seed=seed, places=places, things=list(things), robots=robots,
                    policy=PolicyCard(trained_skus=set(trained), flaws=dict(flaws or {})), intrusions=list(intrusions), outages=list(outages))


def run(world, skill, args, guidance=None, mode=ExecutionMode.LANGUAGE):
    return world.execute("r1", ExecutionRequest(robot_id="r1", skill=skill, args=args, guidance=guidance or Guidance()), mode)


class WorldTest(unittest.TestCase):
    def test_scenarios_are_deterministic(self):
        for name in BENCH_TASKS:
            a, b = build(name, 5), build(name, 5)
            self.assertEqual(sorted((t.oid, t.sku, t.location) for t in a.world.things.values()),
                             sorted((t.oid, t.sku, t.location) for t in b.world.things.values()))
            self.assertEqual(a.tasks[0].params, b.tasks[0].params)

    def test_occluded_hook_needs_wrist_camera(self):
        def rate(camera):
            ok = 0
            for seed in range(200):
                w = tiny_world(seed, things=[Thing("o1", "a", "held:r1")],
                               slot_info={"s1": {"vis_head": 0.35, "vis_wrist": 0.95, "occluder": "price_tag"}})
                w.robots["r1"].holding = "o1"
                fb = run(w, "hang", {"location": "shelf", "slot": "s1"}, Guidance(camera=camera))
                ok += fb.success
            return ok / 200

        head, wrist = rate("head"), rate("wrist")
        self.assertLess(head, 0.5)
        self.assertGreater(wrist, 0.8)

    def test_failure_evidence_names_camera_and_occluder(self):
        for seed in range(50):
            w = tiny_world(seed, things=[Thing("o1", "a", "held:r1")],
                           slot_info={"s1": {"vis_head": 0.35, "vis_wrist": 0.95, "occluder": "price_tag"}})
            w.robots["r1"].holding = "o1"
            fb = run(w, "hang", {"location": "shelf", "slot": "s1"})
            if not fb.success and fb.failure_reason.code == "target_not_localized":
                self.assertEqual(fb.failure_reason.evidence["camera"], "head")
                self.assertEqual(fb.failure_reason.evidence["occluder"], "price_tag")
                return
        self.fail("expected at least one localization failure")

    def test_reference_image_beats_language_for_lookalikes(self):
        def rate(guidance):
            ok = 0
            for seed in range(200):
                things = [Thing("o1", "a", "shelf", "s1", group="g"), Thing("o2", "b", "shelf", "s2", group="g")]
                w = tiny_world(seed, things=things)
                fb = run(w, "pick", {"sku": "a", "location": "shelf"}, guidance)
                ok += fb.success and w.things["o1"].location == "held:r1"
            return ok / 200

        lang = rate(Guidance(instruction="pick a"))
        ref = rate(Guidance(instruction="pick a", references=[ObjectReference(ref_id="a")]))
        self.assertGreater(ref, lang + 0.25)

    def test_box_cue_selects_the_boxed_object(self):
        things = [Thing("o1", "a", "shelf", "s1", group="g"), Thing("o2", "b", "shelf", "s2", group="g")]
        w = tiny_world(1, things=things)
        box = w._box(w.things["o2"])
        hits = 0
        for seed in range(30):
            w = tiny_world(seed, things=[Thing("o1", "a", "shelf", "s1", group="g"), Thing("o2", "b", "shelf", "s2", group="g")])
            run(w, "pick", {"sku": "a", "location": "shelf"}, Guidance(spatial=[SpatialCue(kind="box", coords=box)]))
            hits += w.things["o2"].location == "held:r1"
        self.assertGreater(hits, 20)  # a wrong box leads the policy to the wrong object

    def test_release_on_retract_flaw_and_keep_closed(self):
        def drops(guidance):
            n = 0
            for seed in range(200):
                w = tiny_world(seed, things=[Thing("o1", "a", "shelf", "s1")], flaws={"release_on_retract": 0.5})
                fb = run(w, "pick", {"sku": "a", "location": "shelf"}, guidance)
                if fb.success:
                    nav = run(w, "navigate_to", {"location": "dock"}, mode=ExecutionMode.PRIMITIVE)
                    n += "gripper_opened_in_transit" in nav.action_result.details.get("events", [])
            return n

        self.assertGreater(drops(Guidance()), 50)
        self.assertEqual(drops(Guidance(options={"keep_gripper_closed": True})), 0)
        self.assertEqual(drops(Guidance(constraints=["Keep the gripper closed during retraction and travel."])), 0)

    def test_intrusion_pause_versus_abort(self):
        aborted = paused_ok = 0
        for seed in range(100):
            w = tiny_world(seed, things=[Thing("o1", "a", "shelf", "s1")], intrusions=[Intrusion("shelf", 0.0, 30.0)])
            fb = run(w, "pick", {"sku": "a", "location": "shelf"})
            aborted += (not fb.success and fb.failure_reason.code == "workspace_intrusion")
            w2 = tiny_world(seed, things=[Thing("o1", "a", "shelf", "s1")], intrusions=[Intrusion("shelf", 0.0, 30.0)])
            fb2 = run(w2, "pick", {"sku": "a", "location": "shelf"}, Guidance(options={"pause_on_intrusion": True}))
            paused_ok += fb2.success
            self.assertGreaterEqual(w2.robots["r1"].clock, 30.0)
        self.assertGreater(aborted, 50)
        self.assertGreater(paused_ok, 80)

    def test_service_outage_reports_error_with_retry_hint(self):
        w = tiny_world(things=[Thing("o1", "a", "shelf", "s1")], outages=[Outage("r1", 0.0, 50.0)])
        fb = run(w, "pick", {"sku": "a", "location": "shelf"})
        self.assertEqual(fb.failure_reason.code, "service_unavailable")
        self.assertEqual(fb.action_result.outcome.value, "error")
        self.assertGreater(fb.failure_reason.evidence["retry_after_s"], 0)
        w.wait("r1", 60)
        self.assertTrue(run(w, "observe", {}).success)

    def test_detectors_err_from_afar_but_not_close(self):
        far_fp = close_fp = 0
        for seed in range(300):
            w = tiny_world(seed, things=[Thing("o1", "a", "shelf", "s2")])
            res, _ = w.check("r1", "on_slot", {"sku": "a", "location": "shelf", "slot": "s1"}, close_range=False)
            far_fp += res.passed
            res, _ = w.check("r1", "on_slot", {"sku": "a", "location": "shelf", "slot": "s1"}, close_range=True)
            close_fp += res.passed
        self.assertGreater(far_fp, 50)
        self.assertLess(close_fp, 25)

    def test_grading(self):
        sc = build("restocking", 0)
        self.assertFalse(sc.grade().success)
        for goal in sc.goals:
            thing = next(t for t in sc.world.things.values() if t.sku == goal.sku)
            thing.location, thing.slot = goal.location, goal.slot
        grade = sc.grade()
        self.assertTrue(grade.success)
        self.assertEqual(grade.completion, 1.0)


if __name__ == "__main__":
    unittest.main()
