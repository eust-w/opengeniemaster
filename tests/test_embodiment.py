import json
import threading
import unittest
import urllib.request

from opengeniemaster.embodiment.adapters import AdapterError, HttpAdapter
from opengeniemaster.embodiment.profiles import CAPABILITY_LADDER, PROFILES, skills_for
from opengeniemaster.embodiment.robots import Fleet, make_robot
from opengeniemaster.embodiment.service import RobotService, SimBackend, make_server
from opengeniemaster.eep.types import ExecutionMode, ExecutionRequest, Guidance
from opengeniemaster.sim.scenes import build


class ProfileTest(unittest.TestCase):
    def test_skill_bindings_follow_features(self):
        humanoid = PROFILES["wheeled_humanoid"].skills
        self.assertEqual(humanoid["pick"].service, "policy")
        self.assertIn("spread", humanoid)
        ik_only = skills_for(CAPABILITY_LADDER[0][1])
        self.assertEqual(ik_only["pick"].mode, ExecutionMode.ACTION)
        self.assertNotIn("handover", ik_only)
        self.assertNotIn("spread", PROFILES["single_arm_mobile"].skills)
        self.assertNotIn("reference", PROFILES["single_arm_mobile"].skills["pick"].guidance)

    def test_fleet_capability_queries(self):
        sc = build("tablecloth_spreading", 0)
        from opengeniemaster.embodiment.adapters import SimAdapter

        adapter = SimAdapter(sc.world)
        fleet = Fleet([make_robot("a", "wheeled_humanoid", adapter), make_robot("b", "single_arm_mobile", adapter)])
        self.assertEqual([r.robot_id for r in fleet.capable("spread", ["dual_arm"])], ["a"])
        self.assertEqual(len(fleet.capable("pick")), 2)


class HttpContractTest(unittest.TestCase):
    def setUp(self):
        self.sc = build("item_retrieval", 0)
        self.service = RobotService(SimBackend(self.sc.world, "wh-01"), token="s3cret")
        self.adapter = HttpAdapter("http://robot.local:8765", transport=self.service.as_transport())
        self.robot = make_robot("wh-01", "wheeled_humanoid", self.adapter)

    def test_execute_observe_check_state_via_contract(self):
        fb = self.adapter.execute(self.robot, ExecutionRequest(robot_id="wh-01", skill="navigate_to", args={"location": "shelf_A"},
                                                               guidance=Guidance(instruction="go")), ExecutionMode.PRIMITIVE)
        self.assertTrue(fb.success)
        obs = self.adapter.observe(self.robot)
        self.assertEqual(obs.location, "shelf_A")
        self.assertTrue(obs.detections)
        res = self.adapter.check(self.robot, "at_location", {"location": "shelf_A"})
        self.assertTrue(res.passed)
        self.assertEqual(self.adapter.state(self.robot)["location"], "shelf_A")

    def test_auth_and_errors(self):
        status, body = self.service.handle("GET", "/eep/v0/health", None, {})
        self.assertEqual(status, 401)
        status, body = self.service.handle("POST", "/eep/v0/execute", {"request": {"skill": "pick"}},
                                           {"Authorization": "Bearer s3cret"})
        self.assertEqual(status, 400)
        status, _ = self.service.handle("GET", "/nope", None, {"Authorization": "Bearer s3cret"})
        self.assertEqual(status, 404)

    def test_transport_failure_becomes_service_unavailable(self):
        def broken(method, url, body, timeout):
            raise AdapterError("connection refused")

        adapter = HttpAdapter("http://robot.local", transport=broken)
        fb = adapter.execute(self.robot, ExecutionRequest(robot_id="wh-01", skill="pick"), ExecutionMode.LANGUAGE)
        self.assertFalse(fb.success)
        self.assertEqual(fb.failure_reason.code, "service_unavailable")

    def test_real_socket_round_trip(self):
        try:
            server = make_server(self.service, "127.0.0.1", 0)
        except OSError as exc:  # sandboxes may forbid binding
            self.skipTest(f"cannot bind a local port: {exc}")
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/eep/v0/health", headers={"Authorization": "Bearer s3cret"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertTrue(json.loads(resp.read())["ok"])
            adapter = HttpAdapter(f"http://127.0.0.1:{port}", token="s3cret")
            self.assertIn("location", adapter.state(self.robot))
        finally:
            server.shutdown()
            server.server_close()


class HumanBackendExampleTest(unittest.TestCase):
    def test_operator_answers_flow_through_the_contract(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
        from human_backend import HumanBackend

        answers = iter(["y", "n grasp_failed slipped", "y"])
        backend = HumanBackend("g2-01", ask=lambda prompt: next(answers), say=lambda *a: None)
        adapter = HttpAdapter("http://g2", transport=RobotService(backend).as_transport())
        robot = make_robot("g2-01", "wheeled_humanoid", adapter)
        fb = adapter.execute(robot, ExecutionRequest(robot_id="g2-01", skill="navigate_to", args={"location": "shelf_A"}),
                             ExecutionMode.PRIMITIVE)
        self.assertTrue(fb.success)
        fb = adapter.execute(robot, ExecutionRequest(robot_id="g2-01", skill="pick", args={"sku": "cola-330"}), ExecutionMode.LANGUAGE)
        self.assertEqual(fb.failure_reason.code, "grasp_failed")
        self.assertTrue(adapter.check(robot, "at_location", {"location": "shelf_A"}).passed)
        self.assertEqual(adapter.state(robot)["location"], "shelf_A")


if __name__ == "__main__":
    unittest.main()
