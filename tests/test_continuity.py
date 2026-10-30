import tempfile
import unittest
from pathlib import Path

from opengeniemaster._util import IdFactory
from opengeniemaster.eep import InvalidRequest, StepStatus, StepTemplate, TaskStatus
from opengeniemaster.eep.errors import Conflict
from opengeniemaster.harness.continuity import TaskStore


def chain():
    return [
        StepTemplate("go", "navigate_to", {"location": "storage"}, group="a"),
        StepTemplate("search", "search", {"sku": "x", "candidates": ["l1"]}, depends_on=["go"], group="a"),
        StepTemplate("pick", "pick", {"sku": "x", "location": "@search.location"}, depends_on=["search"], group="a"),
        StepTemplate("put", "place", {"sku": "x", "location": "shelf"}, depends_on=["pick"], group="a"),
        StepTemplate("wipe", "wipe", {"location": "table"}),
    ]


class TaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(IdFactory(seed=1))

    def test_rejects_cycles_unknown_deps_and_duplicates(self):
        with self.assertRaises(InvalidRequest):
            self.store.create("g", [StepTemplate("a", "wait", depends_on=["b"]), StepTemplate("b", "wait", depends_on=["a"])], t=0)
        with self.assertRaises(InvalidRequest):
            self.store.create("g", [StepTemplate("a", "wait", depends_on=["zzz"])], t=0)
        with self.assertRaises(InvalidRequest):
            self.store.create("g", [StepTemplate("a", "wait"), StepTemplate("a", "wait")], t=0)

    def test_ready_steps_follow_dependencies(self):
        task = self.store.create("g", chain(), t=0)
        self.assertEqual({s.step_id for s in self.store.ready_steps(task)}, {"go", "wipe"})
        self.assertEqual(task.dependencies["pick"], ["search"])
        self.assertEqual(set(task.pending_work), {"go", "search", "pick", "put", "wipe"})

    def test_group_assignment_and_result_references(self):
        task = self.store.create("g", chain(), t=0)
        self.store.assign(task, "go", "r1", t=1)
        self.assertEqual({task.step(s).assigned_robot for s in ("go", "search", "pick", "put")}, {"r1"})
        self.assertIsNone(task.step("wipe").assigned_robot)
        with self.assertRaises(Conflict):
            self.store.resolve_args(task, task.step("pick"))
        for sid, result in (("go", {}), ("search", {"location": "l1"})):
            self.store.start(task, sid, "r1", t=2)
            self.store.finish(task, sid, success=True, t=3, result=result)
        self.assertEqual(self.store.resolve_args(task, task.step("pick"))["location"], "l1")
        self.assertEqual(task.completed_steps, ["go", "search"])

    def test_completion_failure_retry_and_skip(self):
        task = self.store.create("g", [StepTemplate("a", "wait"), StepTemplate("b", "wait", depends_on=["a"], optional=True)], t=0)
        self.store.start(task, "a", "r1", t=1)
        self.store.finish(task, "a", success=False, t=2, failure="timeout")
        self.assertEqual(task.step("a").status, StepStatus.FAILED)
        self.assertEqual(task.step("a").last_failure, "timeout")
        self.store.retry_step(task, "a", t=3)
        self.assertEqual(task.step("a").status, StepStatus.ASSIGNED)
        self.store.start(task, "a", "r1", t=4)
        self.store.finish(task, "a", success=True, t=5)
        self.assertEqual(task.status, TaskStatus.COMPLETED)  # optional step does not block completion
        self.assertEqual(task.step("a").attempts, 2)

    def test_interrupt_and_handoff_resume_at_next_incomplete_step(self):
        task = self.store.create("g", chain(), t=0)
        self.store.assign(task, "go", "r1", t=0)
        self.store.start(task, "go", "r1", t=1)
        self.store.finish(task, "go", success=True, t=2)
        self.store.start(task, "search", "r1", t=3)
        self.store.interrupt(task, t=4, note="service down")
        self.assertEqual(task.status, TaskStatus.INTERRUPTED)
        self.assertEqual(task.step("search").status, StepStatus.ASSIGNED)
        moved = self.store.handoff(task, "r1", "r2", t=5)
        self.assertEqual(set(moved), {"search", "pick", "put"})
        self.assertEqual(task.status, TaskStatus.ACTIVE)
        self.assertEqual(task.completed_steps, ["go"])  # completed work is kept
        self.assertEqual(task.assigned_robot, "r2")

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            store = TaskStore(IdFactory(seed=1), Path(d))
            task = store.create("g", chain(), t=0)
            store.assign(task, "go", "r1", t=0)
            store.save()
            again = TaskStore(IdFactory(seed=2), Path(d))
            self.assertEqual(again.get(task.task_id).step("pick").assigned_robot, "r1")


if __name__ == "__main__":
    unittest.main()
