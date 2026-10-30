import unittest

from opengeniemaster._serde import SerdeError, from_dict, to_dict
from opengeniemaster.eep import (
    ContinuityState,
    ExecutionMode,
    ExecutionRequest,
    Feedback,
    Guidance,
    ObjectReference,
    SchemaError,
    SpatialCue,
    StepState,
    StepStatus,
    schema_for,
    validate,
)


class SerdeTest(unittest.TestCase):
    def test_request_round_trip(self):
        g = Guidance(instruction="pick the bottle", detailed="grasp the neck", constraints=["label outward"],
                     spatial=[SpatialCue(kind="box", coords=[0.1, 0.2, 0.3, 0.4])],
                     references=[ObjectReference(ref_id="sku-1", attributes={"color": "blue"})],
                     options={"verify_close_range": True})
        req = ExecutionRequest(robot_id="r1", skill="pick", args={"sku": "sku-1"}, guidance=g, mode=ExecutionMode.LANGUAGE)
        data = req.to_dict()
        self.assertEqual(data["mode"], "language")
        self.assertNotIn("task_id", data)  # None fields are dropped
        self.assertEqual(ExecutionRequest.from_dict(data), req)

    def test_continuity_round_trip_keeps_published_fields(self):
        cs = ContinuityState(task_id="t1", goal="g", completed_steps=["a"], pending_work=["b"], assigned_robot="r1",
                             held_objects={"r1": "cola"}, dependencies={"b": ["a"]},
                             steps=[StepState(step_id="a", skill="pick", status=StepStatus.SUCCEEDED), StepState(step_id="b", skill="place")])
        summary = cs.summary()
        for key in ("task_id", "completed_steps", "assigned_robot", "pending_work", "held_objects", "dependencies"):
            self.assertIn(key, summary)
        self.assertEqual(ContinuityState.from_dict(cs.to_dict()), cs)

    def test_unknown_fields_are_ignored_and_bad_types_rejected(self):
        g = from_dict(Guidance, {"instruction": "x", "future_field": 1})
        self.assertEqual(g.instruction, "x")
        with self.assertRaises(SerdeError):
            from_dict(Guidance, {"constraints": "not a list"})
        with self.assertRaises(SerdeError):
            from_dict(ExecutionRequest, {"skill": "pick"})  # robot_id missing
        with self.assertRaises(SerdeError):
            from_dict(ExecutionRequest, {"robot_id": "r", "skill": "pick", "mode": "telepathy"})

    def test_enums_and_sets_serialize(self):
        self.assertEqual(to_dict({"s": StepStatus.READY, "x": {3, 1, 2}}), {"s": "ready", "x": [1, 2, 3]})


class SchemaTest(unittest.TestCase):
    def test_schema_from_dataclass_validates(self):
        schema = schema_for(ExecutionRequest)
        self.assertEqual(schema["type"], "object")
        self.assertIn("robot_id", schema["required"])
        validate(ExecutionRequest(robot_id="r", skill="pick").to_dict(), schema)

    def test_validation_reports_every_problem(self):
        schema = schema_for(ExecutionRequest)
        with self.assertRaises(SchemaError) as ctx:
            validate({"robot_id": 3, "skill": "pick", "mode": "telepathy"}, schema)
        self.assertEqual(len(ctx.exception.errors), 2)

    def test_feedback_schema_has_published_fields(self):
        props = schema_for(Feedback)["properties"]
        for key in ("observation", "action_result", "failure_reason"):
            self.assertIn(key, props)


if __name__ == "__main__":
    unittest.main()
