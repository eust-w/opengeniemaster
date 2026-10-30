import tempfile
import unittest
from pathlib import Path

from opengeniemaster.harness.admission import SharedAdmission, entry_id_for
from opengeniemaster.memory.reflection import LLMReflector, Proposal, RuleReflector, parse_proposals, reflect
from opengeniemaster.memory.store import ExecutionRecord, MemoryEntry, MemoryStore


def rec(rid, *, success, skill="hang", code=None, task="t1", step="s1", attempt=1, recovery=None, tags=("hook", "small"), domain="store",
        model="cart", evidence=None, args=None, **kw):
    return ExecutionRecord(record_id=rid, t=0.0, robot_id="r1", robot_model=model, skill=skill, success=success,
                           outcome="success" if success else "failure", domain=domain, task_id=task, step_id=step, attempt=attempt,
                           recovery=recovery, failure_code=code, tags=list(tags), evidence=dict(evidence or {}), args=dict(args or {}), **kw)


class StoreTest(unittest.TestCase):
    def test_temporary_memory_persists_records_contradictions_and_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            m = MemoryStore(Path(d))
            m.temporary.add(rec("a", success=True))
            m.temporary.add(rec("b", success=False, code="dropped"))
            m.temporary.mark_contradicted("a", "dropped in transit")
            m.temporary.advance(1)
            again = MemoryStore(Path(d))
            self.assertEqual(len(again.temporary), 2)
            self.assertTrue(again.temporary.get("a").contradicted)
            self.assertEqual([r.record_id for r in again.temporary.pending()], ["b"])

    def test_long_term_versions_rollback_and_diff(self):
        with tempfile.TemporaryDirectory() as d:
            m = MemoryStore(Path(d))
            lt = m.long_term
            lt.add(MemoryEntry("e1", "domain", "guidance", "first lesson", when={"skill": "pick"}), reason="r")
            lt.add(MemoryEntry("e2", "embodiment", "guidance", "second lesson", when={"skill": "hang"}), reason="r")
            lt.retire("e1", reason="bad")
            self.assertEqual(lt.version, 3)
            self.assertEqual([e.entry_id for e in lt.admitted()], ["e2"])
            diff = lt.diff(1, 3)
            self.assertEqual([e["entry_id"] for e in diff["added"]], ["e2"])
            self.assertEqual(diff["changed"][0]["entry_id"], "e1")
            lt.rollback(1)
            self.assertEqual([e.entry_id for e in lt.admitted()], ["e1"])
            again = MemoryStore(Path(d))
            self.assertEqual([e.entry_id for e in again.long_term.admitted()], ["e1"])
            self.assertEqual(len(again.long_term.history()), 4)

    def test_matching_and_specificity(self):
        lt = MemoryStore().long_term
        lt.add(MemoryEntry("gen", "domain", "guidance", "general pick rule", when={"skill": "pick"}), reason="r")
        lt.add(MemoryEntry("spec", "domain", "guidance", "pick rule for look-alikes", when={"skill": "pick", "tags_all": ["look_alike"]}),
               reason="r")
        lt.add(MemoryEntry("other", "domain", "guidance", "hang rule", when={"skill": "hang"}), reason="r")
        hits = lt.relevant({"skill": "pick", "tags": ["look_alike", "small"]})
        self.assertEqual([e.entry_id for e in hits], ["spec", "gen"])
        self.assertEqual([e.entry_id for e in lt.relevant({"skill": "pick", "tags": []})], ["gen"])


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.m = MemoryStore()
        for rid in ("a", "b", "c"):
            self.m.temporary.add(rec(rid, success=False, code="target_not_localized"))
        self.adm = SharedAdmission(min_support=2)

    def proposal(self, **kw):
        base = dict(scope="embodiment", kind="guidance", text="Use the wrist camera at small hooks.",
                    when={"skill": "hang", "tags_all": ["hook"]}, patch={"camera": "wrist"}, evidence=["a", "b"], source="reflection:rule")
        base.update(kw)
        return Proposal(**base)

    def test_admit_then_merge_duplicate(self):
        d1 = self.adm.admit(self.proposal(), self.m)
        self.assertEqual((d1.admitted, d1.action), (True, "admit"))
        d2 = self.adm.admit(self.proposal(evidence=["c"], source="agent"), self.m)
        self.assertEqual(d2.action, "merge")
        self.assertEqual(self.m.long_term.entries[d1.entry_id].evidence, ["a", "b", "c"])
        self.assertEqual(len(self.m.long_term.admitted()), 1)

    def test_rejections(self):
        cases = {
            "unknown evidence": self.proposal(evidence=["zzz", "a"]),
            "support": self.proposal(evidence=["a"]),
            "safety": self.proposal(patch={"pause_on_intrusion": False}, evidence=["a", "b"]),
            "patch key": self.proposal(patch={"teleport": True}),
            "scope": self.proposal(scope="galaxy"),
            "when key": self.proposal(when={"weather": "sunny"}),
        }
        for name, p in cases.items():
            with self.subTest(name):
                self.assertFalse(self.adm.check_proposal(p, self.m).admitted)

    def test_conflict_and_supersede(self):
        first = self.adm.admit(self.proposal(), self.m)
        conflicting = self.proposal(patch={"camera": "head"}, text="Use the head camera at small hooks.")
        self.assertFalse(self.adm.admit(conflicting, self.m).admitted)
        superseding = self.proposal(patch={"camera": "head"}, text="Use the head camera at small hooks.", evidence=["a", "b", "c"],
                                    supersedes=first.entry_id)
        self.assertTrue(self.adm.admit(superseding, self.m).admitted)
        self.assertEqual(self.m.long_term.entries[first.entry_id].status, "retired")

    def test_more_specific_duplicate_merges_into_general_entry(self):
        general = self.adm.admit(self.proposal(when={"skill": "hang"}), self.m)
        specific = self.adm.admit(self.proposal(when={"skill": "hang", "tags_all": ["hook"], "robot_model": "cart"},
                                                evidence=["c", "a"]), self.m)
        self.assertEqual(specific.action, "merge")
        self.assertEqual(specific.entry_id, general.entry_id)
        self.assertEqual(len(self.m.long_term.admitted()), 1)
        broader = self.adm.admit(self.proposal(when={}, text="Use the wrist camera at hooks everywhere."), self.m)
        self.assertEqual(broader.action, "admit")  # a more general rule is a new entry

    def test_entry_ids_are_content_addressed(self):
        self.assertEqual(entry_id_for(self.proposal()), entry_id_for(self.proposal(evidence=["c"])))


class ReflectionTest(unittest.TestCase):
    def run_rules(self, records, min_support=2):
        m = MemoryStore()
        for r in records:
            m.temporary.add(r)
        return m, RuleReflector(min_support=min_support).propose(m.temporary.pending(), m.temporary, m.long_term)

    def test_recovered_success_becomes_instruction(self):
        records = []
        for i in (1, 2):
            records.append(rec(f"f{i}", success=False, code="target_not_localized", task=f"t{i}", attempt=1,
                               evidence={"camera": "head", "occluder": "price_tag"}))
            records.append(rec(f"s{i}", success=True, task=f"t{i}", attempt=2, recovery="use_wrist_camera"))
        _, proposals = self.run_rules(records)
        self.assertEqual(len(proposals), 1)
        p = proposals[0]
        self.assertEqual(p.patch, {"camera": "wrist"})
        self.assertEqual(p.when["skill"], "hang")
        self.assertEqual(p.when["tags_all"], ["hook", "small"])
        self.assertEqual(p.when["robot_model"], "cart")
        self.assertEqual(sorted(p.evidence), ["f1", "f2", "s1", "s2"])
        self.assertIn("wrist camera", p.text)

    def test_single_occurrence_is_not_enough(self):
        records = [rec("f1", success=False, code="target_not_localized", attempt=1),
                   rec("s1", success=True, attempt=2, recovery="use_wrist_camera")]
        self.assertEqual(self.run_rules(records)[1], [])

    def test_contradictions_intrusions_drops_hints_and_mismatch(self):
        records = [
            rec("c1", success=True, skill="search", task="t1", contradiction="search result did not hold up", contradicted=True, tags=("look_alike",)),
            rec("c2", success=True, skill="search", task="t2", contradiction="search result did not hold up", contradicted=True, tags=("look_alike",)),
            rec("i1", success=False, skill="pick", code="workspace_intrusion"),
            rec("d1", success=False, skill="place", code="not_holding", evidence={"gripper_opened_in_transit": True}),
            rec("d2", success=False, skill="place", code="not_holding", evidence={"gripper_opened_in_transit": True}),
            rec("h1", success=True, skill="search", task="t3", args={"sku": "case"}, evidence={"location": "bar", "visited": ["desk", "bar"]}),
            rec("h2", success=True, skill="search", task="t4", args={"sku": "case"}, evidence={"location": "bar", "visited": ["desk", "bar"]}),
            rec("u1", success=False, skill="spread", code="unreachable", evidence={"requires": "dual_arm"}),
        ]
        _, proposals = self.run_rules(records)
        kinds = {(p.kind, tuple(sorted(p.patch))) for p in proposals}
        self.assertIn(("criteria", ("verify_close_range",)), kinds)
        self.assertIn(("safety", ("pause_on_intrusion",)), kinds)
        self.assertIn(("guidance", ("keep_gripper_closed",)), kinds)
        self.assertIn(("guidance", ("search_first",)), kinds)
        self.assertIn(("coordination", ("prefer_features",)), kinds)

    def test_reflect_pipeline_admits_and_advances_cursor(self):
        m, _ = self.run_rules([rec("i1", success=False, skill="pick", code="workspace_intrusion")])
        report = reflect(m, SharedAdmission(), RuleReflector())
        self.assertEqual(len(report.admitted), 1)
        self.assertEqual(m.temporary.pending(), [])
        self.assertEqual(report.version_after, 1)

    def test_llm_reflector_with_fake_model(self):
        class Fake:
            def __init__(self):
                self.prompts = []

            def complete(self, system, prompt, *, max_tokens=2000):
                self.prompts.append(prompt)
                return ('Here you go:\n```json\n{"proposals": [{"scope": "domain", "kind": "guidance", '
                        '"text": "Confirm the earphone case is silver-white and round before picking.", '
                        '"when": {"skill": "pick", "sku": "earphone-case"}, "evidence": ["f1"]}]}\n```')

        m = MemoryStore()
        m.temporary.add(rec("f1", success=False, skill="pick", code="wrong_object"))
        fake = Fake()
        report = reflect(m, SharedAdmission(agent_min_support=1), LLMReflector(fake))
        self.assertEqual(len(fake.prompts), 1)
        self.assertIn("f1", fake.prompts[0])
        # the reflection source needs min_support=2, so a single record is rejected
        self.assertEqual(report.admitted, [])
        self.assertIn("evidence", report.decisions[0]["reasons"][0])

    def test_parse_proposals_tolerates_noise(self):
        self.assertEqual(parse_proposals("no json here"), [])
        self.assertEqual(parse_proposals("{broken"), [])
        ps = parse_proposals('{"proposals": [{"text": "x is y", "evidence": ["a"]}, "junk"]}')
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0].scope, "domain")


if __name__ == "__main__":
    unittest.main()
