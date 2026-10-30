"""Semantic multi-robot world used as the default execution backend.

The world keeps hidden ground truth (where things really are, which hooks are
occluded, which grasps are loose) and exposes only EEP messages: execution
feedback, observations and detector results. Each robot has its own clock so
several robots can work in parallel in simulated time.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..domain.records import slot_center
from ..eep.types import (
    ActionResult,
    CriterionResult,
    Detection,
    ExecutionMode,
    ExecutionRequest,
    FailureReason,
    Feedback,
    Guidance,
    Observation,
    Outcome,
)
from . import policy as pol
from .policy import PolicyCard, SimParams


@dataclass
class Place:
    loc_id: str
    kind: str
    pos: tuple[float, float]
    slots: list[str] = field(default_factory=list)
    # Hidden per-slot facts, e.g. {"hook-3": {"vis_head": 0.35, "vis_wrist": 0.95, "occluder": "price_tag"}}
    slot_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)  # e.g. {"dirty": True}
    people: bool = False  # a person/recipient is normally here (counter, guest sofa, user desk)


@dataclass
class Thing:
    oid: str
    sku: str
    location: str  # place id | "held:<robot>" | "delivered:<place>"
    slot: str | None = None
    group: str | None = None  # look-alike family
    category: str = "item"
    tags: set[str] = field(default_factory=set)  # hidden: edge, occluded, small, back_row, trash
    vis_head: float = 0.95
    vis_wrist: float = 0.98
    occluder: str | None = None
    pos: tuple[float, float] = (0.5, 0.5)  # normalized image position in its location's view
    size: float = 0.08
    loose: bool = False
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimRobot:
    robot_id: str
    model: str
    features: set[str]
    location: str
    clock: float = 0.0
    holding: str | None = None  # oid
    last_drop: dict[str, Any] | None = None
    stopped: bool = False
    calls: int = 0


@dataclass
class Intrusion:
    location: str
    start: float
    end: float


@dataclass
class Outage:
    robot_id: str
    start: float
    end: float
    after_calls: int | None = None  # trigger on the robot's N-th execution instead of at a fixed time
    duration: float = 0.0


@dataclass
class _Step:
    success: bool
    duration: float
    failure: FailureReason | None = None
    camera: str = "head"
    progress: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)
    outcome: Outcome | None = None


class SimWorld:
    def __init__(
        self,
        *,
        seed: int,
        places: list[Place],
        things: list[Thing],
        robots: list[SimRobot],
        policy: PolicyCard,
        params: SimParams | None = None,
        intrusions: list[Intrusion] | None = None,
        outages: list[Outage] | None = None,
    ) -> None:
        self.rng = random.Random(seed)
        self.places = {p.loc_id: p for p in places}
        self.things = {t.oid: t for t in things}
        self.robots = {r.robot_id: r for r in robots}
        self.policy = policy
        self.p = params or SimParams()
        self.intrusions = list(intrusions or [])
        self.outages = list(outages or [])
        self.deliveries: list[dict[str, Any]] = []
        self.safety_violations: list[dict[str, Any]] = []
        self.detector_log: list[dict[str, Any]] = []  # ground-truth audit of detector verdicts (never shown to agents)
        self.calls: Counter[str] = Counter()
        self._next_oid = len(self.things) + 1
        for place in self.places.values():
            for thing in self.things_at(place.loc_id):
                self._assign_pos(thing)

    # ------------------------------------------------------------------ queries

    def things_at(self, loc: str) -> list[Thing]:
        return sorted((t for t in self.things.values() if t.location == loc), key=lambda t: t.oid)

    def robot(self, robot_id: str) -> SimRobot:
        return self.robots[robot_id]

    def now(self, robot_id: str | None = None) -> float:
        if robot_id is not None:
            return self.robots[robot_id].clock
        return max((r.clock for r in self.robots.values()), default=0.0)

    def lookalike(self, a: Thing, b: Thing) -> bool:
        return a.sku != b.sku and a.group is not None and a.group == b.group

    def distance(self, a: str, b: str) -> float:
        pa, pb = self.places[a].pos, self.places[b].pos
        return math.dist(pa, pb)

    def add_thing(self, thing: Thing) -> Thing:
        self.things[thing.oid] = thing
        self._assign_pos(thing)
        return thing

    def new_oid(self) -> str:
        oid = f"obj-{self._next_oid:03d}"
        self._next_oid += 1
        return oid

    # --------------------------------------------------------------- execution

    def execute(self, robot_id: str, req: ExecutionRequest, mode: ExecutionMode) -> Feedback:
        robot = self.robots[robot_id]
        self.calls[req.skill] += 1
        robot.calls += 1
        t0 = robot.clock
        outage = self._outage(robot_id, t0)
        if outage is not None:
            robot.clock += self.p.service_error_s
            step = _Step(
                success=False,
                duration=self.p.service_error_s,
                failure=FailureReason(
                    code="service_unavailable",
                    message="action service did not respond",
                    evidence={"retry_after_s": round(outage.end - t0, 1)},
                ),
                outcome=Outcome.ERROR,
                progress=0.0,
            )
            return self._feedback(robot, req, mode, step)
        if robot.stopped:
            step = _Step(
                success=False,
                duration=0.0,
                failure=FailureReason(code="precondition_failed", message="robot is stopped", recoverable=True),
                outcome=Outcome.REJECTED,
                progress=0.0,
            )
            return self._feedback(robot, req, mode, step)
        handler = getattr(self, f"_do_{req.skill}", None)
        if handler is None:
            step = _Step(
                success=False,
                duration=0.0,
                failure=FailureReason(code="unknown_skill", message=f"skill '{req.skill}' is not implemented", recoverable=False),
                outcome=Outcome.REJECTED,
                progress=0.0,
            )
            return self._feedback(robot, req, mode, step)
        step = handler(robot, req, mode)
        if req.guidance.options.get("end_to_end") and step.success and req.skill not in ("navigate_to", "wait", "observe"):
            # An end-to-end policy given only the overall goal sometimes loses track of the sequence.
            if self.rng.random() > self.p.end_to_end_step_reliability:
                step = _Step(
                    success=False,
                    duration=step.duration,
                    failure=FailureReason(code="incomplete", message="policy drifted from the intended subtask"),
                    progress=0.3,
                    camera=step.camera,
                )
        robot.clock += step.duration
        return self._feedback(robot, req, mode, step)

    def observe(self, robot_id: str, camera: str = "head", location: str | None = None) -> Observation:
        robot = self.robots[robot_id]
        self.calls["observe"] += 1
        if location is not None and location != robot.location:
            return Observation(
                robot_id=robot_id,
                t=robot.clock,
                camera=camera,
                location=robot.location,
                robot_state=self._robot_state(robot),
                flags=["location_not_in_view"],
            )
        if camera.startswith("wrist"):
            if "wrist_camera" not in robot.features:
                camera = "head"
            else:
                robot.clock += self.p.wrist_approach_s
        robot.clock += self.p.head_observe_s
        return self._observation(robot, camera)

    def check(self, robot_id: str, name: str, args: dict[str, Any], close_range: bool = False) -> tuple[CriterionResult, float]:
        """Run a completion detector; returns the verdict and the time it took."""
        robot = self.robots[robot_id]
        fn = getattr(self, f"_check_{name}", None)
        if fn is None:
            return CriterionResult(name=name, passed=False, confidence=0.0, evidence={"error": "unknown criterion"}), 0.0
        if close_range and "wrist_camera" not in robot.features:
            close_range = False
        duration = self.p.verify_s if close_range else 0.5
        robot.clock += duration
        passed, truth, evidence = fn(robot, args, close_range)
        self.detector_log.append({"criterion": name, "passed": passed, "truth": truth, "close_range": close_range, "t": robot.clock})
        evidence = dict(evidence, close_range=close_range)
        confidence = round(self.rng.uniform(0.9, 0.99) if close_range else self.rng.uniform(0.7, 0.95), 2)
        return CriterionResult(name=name, passed=passed, confidence=confidence, evidence=evidence), duration

    def wait(self, robot_id: str, seconds: float) -> None:
        self.robots[robot_id].clock += max(0.0, seconds)

    # ----------------------------------------------------------------- skills

    def _do_wait(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        seconds = float(req.args.get("seconds", 10.0))
        return _Step(success=True, duration=max(0.0, seconds))

    def _do_observe(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        camera = str(req.args.get("camera") or req.guidance.camera or "head")
        dur = self.p.head_observe_s + (self.p.wrist_approach_s if camera.startswith("wrist") else 0.0)
        return _Step(success=True, duration=dur, camera="wrist" if camera.startswith("wrist") else "head")

    def _do_navigate_to(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        dest = req.args.get("location")
        if dest not in self.places:
            return _Step(False, 0.5, FailureReason("navigation_blocked", f"unknown location '{dest}'", recoverable=False), progress=0.0)
        origin = robot.location
        dur = self.p.nav_overhead_s + self.distance(origin, dest) / self.p.speed_mps if origin != dest else 0.5
        if origin != dest and self.rng.random() < self.p.nav_fail:
            return _Step(False, dur / 2, FailureReason("navigation_blocked", "path blocked", evidence={"at": origin}), progress=0.5)
        details: dict[str, Any] = {}
        if robot.holding and origin != dest:
            thing = self.things[robot.holding]
            if thing.loose:
                # Release-on-retract flaw: the gripper opened and the object fell near the origin.
                thing.loose = False
                thing.location = origin
                thing.slot = None
                self._assign_pos(thing)
                robot.holding = None
                robot.last_drop = {"at": origin, "t": robot.clock}
                details["events"] = ["gripper_opened_in_transit"]
        robot.location = dest
        return _Step(True, dur, details=details)

    def _do_pick(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        g = req.guidance
        loc = robot.location
        expected = req.args.get("location")
        if expected and expected != loc:
            return _Step(False, 0.5, FailureReason("precondition_failed", f"robot is at {loc}, not {expected}", evidence={"at": loc}), progress=0.0)
        if robot.holding:
            return _Step(False, 0.5, FailureReason("hand_occupied", "gripper already holds an object", evidence={"holding": True}), progress=0.0)
        has_policy = "guided_policy" in robot.features and mode in (ExecutionMode.LANGUAGE, ExecutionMode.AFFORDANCE)
        cam = "wrist" if pol.wants_wrist_camera(g) and "wrist_camera" in robot.features else "head"
        dur = self.p.pick_s + (self.p.wrist_approach_s if cam == "wrist" else 0.0)
        blocked = self._intrusion_gate(robot, g, dur)
        if isinstance(blocked, _Step):
            return blocked
        dur += blocked

        cands = [t for t in self.things_at(loc)]
        box = pol.target_box(g) if (has_policy or mode in (ExecutionMode.ACTION, ExecutionMode.AFFORDANCE)) else None
        boxed = self._thing_at_box(loc, box.coords) if box else None
        if not has_policy:
            if boxed is None:
                return _Step(False, dur, FailureReason("target_not_localized", "IK grasp needs a target box", evidence={"camera": cam}), camera=cam, progress=0.0)
            chosen = boxed
            p_grasp = self.p.grasp_planning if "grasp_planning" in robot.features else self.p.grasp_ik_only
        else:
            if boxed is not None and self.rng.random() < self.p.id_box_guided:
                chosen = boxed
            else:
                chosen = self._policy_identify(robot, g, req.args, cands)
            p_grasp = self.p.grasp_policy
        if chosen is None:
            return _Step(False, dur, FailureReason("target_not_found", "no matching object in view", evidence={"camera": cam, "objects_in_view": len(cands)}), camera=cam, progress=0.0)

        vis = self._visibility(robot, chosen, cam)
        p_loc = vis if box is None else 1.0 - (1.0 - vis) * self.p.box_localization_gain
        if self.rng.random() >= p_loc:
            ev = {"camera": cam, "detection_confidence": round(vis * self.rng.uniform(0.75, 1.0), 2)}
            if chosen.occluder:
                ev["occluder"] = chosen.occluder
            return _Step(False, dur, FailureReason("target_not_localized", "target could not be localized reliably", evidence=ev), camera=cam, progress=0.1)

        if "small" in chosen.tags:
            p_grasp *= self.p.small_target_factor
        if "back_row" in chosen.tags and "head_waist" not in robot.features:
            p_grasp *= self.p.back_row_no_waist_factor
        if self.rng.random() >= p_grasp:
            return _Step(False, dur, FailureReason("grasp_failed", "object slipped during grasp", evidence={"camera": cam}), camera=cam, progress=0.4)

        robot.holding = chosen.oid
        robot.last_drop = None
        chosen.state["prev"] = (chosen.location, chosen.slot)
        chosen.location = f"held:{robot.robot_id}"
        chosen.slot = None
        flaw = self.policy.flaws.get("release_on_retract", 0.0) if has_policy else 0.0
        if flaw and not pol.keep_gripper_closed(g) and self.rng.random() < flaw:
            chosen.loose = True
        return _Step(True, dur, camera=cam, details={"grasped": True})

    def _do_place(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        return self._release(robot, req, mode, hang=False)

    def _do_hang(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        return self._release(robot, req, mode, hang=True)

    def _release(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode, *, hang: bool) -> _Step:
        g = req.guidance
        loc = robot.location
        target_loc = req.args.get("location", loc)
        slot = req.args.get("slot")
        if target_loc != loc:
            return _Step(False, 0.5, FailureReason("precondition_failed", f"robot is at {loc}, not {target_loc}", evidence={"at": loc}), progress=0.0)
        if not robot.holding:
            ev: dict[str, Any] = {"holding": False}
            if robot.last_drop:
                ev["gripper_opened_in_transit"] = True
                ev["dropped_at"] = robot.last_drop["at"]
            return _Step(False, 0.5, FailureReason("not_holding", "gripper is empty", evidence=ev), progress=0.0)
        has_policy = "guided_policy" in robot.features and mode in (ExecutionMode.LANGUAGE, ExecutionMode.AFFORDANCE)
        cam = "wrist" if pol.wants_wrist_camera(g) and "wrist_camera" in robot.features else "head"
        dur = (self.p.hang_s if hang else self.p.place_s) + (self.p.wrist_approach_s if cam == "wrist" else 0.0)
        blocked = self._intrusion_gate(robot, g, dur)
        if isinstance(blocked, _Step):
            return blocked
        dur += blocked
        thing = self.things[robot.holding]
        place = self.places[loc]
        point = pol.placement_point(g) if has_policy else None

        if hang:
            info = place.slot_info.get(slot or "", {})
            vis = info.get("vis_wrist" if cam == "wrist" else "vis_head", 0.95)
            p_loc = vis if point is None else 1.0 - (1.0 - vis) * self.p.box_localization_gain
            if self.rng.random() >= p_loc:
                ev = {"camera": cam, "detection_confidence": round(vis * self.rng.uniform(0.75, 1.0), 2), "target": "hook"}
                if info.get("occluder"):
                    ev["occluder"] = info["occluder"]
                return _Step(False, dur, FailureReason("target_not_localized", "hook could not be localized", evidence=ev), camera=cam, progress=0.3)
            if self.rng.random() >= self.p.hang_base:
                return _Step(False, dur, FailureReason("misplaced", "item did not catch on the hook", evidence={"camera": cam}), camera=cam, progress=0.5)
            final_slot = slot
        else:
            if slot:
                p_ok = self.p.place_point_guided if point else self.p.place_language
                final_slot = slot if self.rng.random() < p_ok else self._neighbor_slot(place, slot)
            else:
                prev = thing.state.get("prev")
                final_slot = prev[1] if prev and prev[0] == loc else None  # put it back where it was taken from
        robot.holding = None
        thing.loose = False
        if self.rng.random() < self.p.release_drop:
            thing.location = loc
            thing.slot = None
            self._assign_pos(thing)
            return _Step(False, dur, FailureReason("dropped", "object fell during release", evidence={"camera": cam}), camera=cam, progress=0.6)
        thing.location = loc
        thing.slot = final_slot
        self._assign_pos(thing)
        return _Step(True, dur, camera=cam, details={"released": True})

    def _do_handover(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        loc = robot.location
        recipient = req.args.get("recipient") or loc
        if req.args.get("location") and req.args["location"] != loc:
            return _Step(False, 0.5, FailureReason("precondition_failed", f"robot is at {loc}", evidence={"at": loc}), progress=0.0)
        if not robot.holding:
            ev: dict[str, Any] = {"holding": False}
            if robot.last_drop:
                ev["gripper_opened_in_transit"] = True
                ev["dropped_at"] = robot.last_drop["at"]
            return _Step(False, 0.5, FailureReason("not_holding", "gripper is empty", evidence=ev), progress=0.0)
        thing = self.things[robot.holding]
        if self.rng.random() >= self.p.handover:
            robot.holding = None
            thing.location = loc
            self._assign_pos(thing)
            return _Step(False, self.p.handover_s, FailureReason("dropped", "handover failed, object dropped"), progress=0.5)
        robot.holding = None
        thing.loose = False
        thing.location = f"delivered:{loc}"
        self.deliveries.append({"oid": thing.oid, "sku": thing.sku, "to": loc, "recipient": recipient, "t": robot.clock})
        return _Step(True, self.p.handover_s)

    def _do_wipe(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        g = req.guidance
        loc = robot.location
        if req.args.get("location") and req.args["location"] != loc:
            return _Step(False, 0.5, FailureReason("precondition_failed", f"robot is at {loc}", evidence={"at": loc}), progress=0.0)
        if robot.holding:
            return _Step(False, 0.5, FailureReason("hand_occupied", "release the held object before wiping"), progress=0.0)
        dur = self.p.wipe_s
        blocked = self._intrusion_gate(robot, g, dur)
        if isinstance(blocked, _Step):
            return blocked
        dur += blocked
        place = self.places[loc]
        guided = "guided_policy" in robot.features
        if self.rng.random() >= (self.p.wipe_guided if guided else self.p.wipe_language):
            return _Step(False, dur, FailureReason("incomplete", "surface still dirty"), progress=0.5)
        place.state["dirty"] = False
        edges = pol.mentions_edges(g) or pol.verify_close_range(g)
        place.state["edge_dirty"] = (not edges) and self.rng.random() < self.p.edge_dirt_left
        return _Step(True, dur)

    def _do_spread(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        g = req.guidance
        loc = robot.location
        if "dual_arm" not in robot.features:
            return _Step(False, 0.5, FailureReason("unreachable", "spreading needs two arms", evidence={"requires": "dual_arm"}, recoverable=True), progress=0.0)
        cloth = self.things.get(robot.holding or "")
        if cloth is None or cloth.category != "cloth":
            here = [t for t in self.things_at(loc) if t.category == "cloth"]
            cloth = here[0] if here else None
        if cloth is None:
            return _Step(False, 0.5, FailureReason("not_holding", "no cloth to spread"), progress=0.0)
        dur = self.p.spread_s
        blocked = self._intrusion_gate(robot, g, dur)
        if isinstance(blocked, _Step):
            return blocked
        dur += blocked
        p_full = self.p.spread_base
        if pol.has_arm_assignment(g):
            p_full += self.p.spread_arm_assignment_bonus
        if len(pol.corner_points(g)) >= 2:
            p_full += self.p.spread_corner_points_bonus
        coverage = 1.0 if self.rng.random() < p_full else round(self.rng.uniform(0.55, 0.89), 2)
        if robot.holding == cloth.oid:
            robot.holding = None
        cloth.location = loc
        cloth.slot = None
        cloth.loose = False
        cloth.state["coverage"] = coverage
        self._assign_pos(cloth)
        if coverage < 0.75:
            return _Step(False, dur, FailureReason("incomplete", "cloth only partly spread", evidence={"coverage_estimate": coverage}), progress=coverage)
        return _Step(True, dur, progress=1.0, details={"coverage_estimate": min(1.0, round(coverage + 0.08, 2))})

    def _do_search(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode) -> _Step:
        """Visit candidate locations and look for the target with the head camera."""
        g = req.guidance
        target = req.args.get("sku")
        candidates = [c for c in req.args.get("candidates", []) if c in self.places]
        if not candidates:
            return _Step(False, 0.5, FailureReason("precondition_failed", "no candidate locations given"), progress=0.0)
        verify = pol.verify_close_range(g) and "wrist_camera" in robot.features
        refs = pol.reference_skus(g)
        total = 0.0
        visited = []
        for loc in candidates:
            if robot.location != loc:
                total += self.p.nav_overhead_s + self.distance(robot.location, loc) / self.p.speed_mps
                if robot.holding:
                    thing = self.things[robot.holding]
                    if thing.loose:
                        thing.loose = False
                        thing.location = robot.location
                        self._assign_pos(thing)
                        robot.holding = None
                        robot.last_drop = {"at": robot.location, "t": robot.clock + total}
                robot.location = loc
            visited.append(loc)
            total += self.p.head_observe_s
            for thing in self.things_at(loc):
                vis = self._visibility(robot, thing, "head")
                if self.rng.random() >= vis:
                    continue
                label = self._label(thing, loc, "head", refs)
                if label != target:
                    continue
                if verify:
                    total += self.p.wrist_approach_s
                    if self._label(thing, loc, "wrist", refs) != target:
                        continue
                return _Step(
                    True,
                    total,
                    details={"found": True, "location": loc, "bbox": self._box(thing), "visited": visited, "verified": verify},
                )
        return _Step(False, total, FailureReason("target_not_found", f"'{target}' not seen at {visited}", evidence={"visited": visited}), progress=0.0)

    # ------------------------------------------------------------- detectors

    def _held(self, robot: SimRobot) -> Thing | None:
        return self.things.get(robot.holding) if robot.holding else None

    def _noisy(self, truth: bool, close: bool, fp: float) -> bool:
        if close:
            return truth if self.rng.random() >= self.p.det_close_error else not truth
        if truth:
            return self.rng.random() >= self.p.det_fn
        return self.rng.random() < fp

    def _check_at_location(self, robot: SimRobot, args: dict, close: bool):
        truth = robot.location == args.get("location")
        return truth, truth, {"at": robot.location}

    def _check_holding_target(self, robot: SimRobot, args: dict, close: bool):
        held = self._held(robot)
        if held is None:
            return False, False, {"holding": False}
        target = args.get("sku")
        truth = held.sku == target
        fp = self.p.det_fp_lookalike if any(self.lookalike(held, t) for t in self.things.values() if t.sku == target) else self.p.det_fp_other
        passed = self._noisy(truth, close, fp)
        return passed, truth, {"holding": True}

    def _check_on_slot(self, robot: SimRobot, args: dict, close: bool):
        sku, loc, slot = args.get("sku"), args.get("location"), args.get("slot")
        here = [t for t in self.things_at(loc) if t.sku == sku]
        truth = any(t.slot == slot for t in here) if slot else bool(here)
        if truth:
            fp = 0.0
        elif here:
            fp = self.p.det_fp_misplaced
        elif any(t.group and t.group == self._group_of(sku) for t in self.things_at(loc)):
            fp = self.p.det_fp_lookalike
        else:
            fp = self.p.det_fp_other
        hard = bool(self.places[loc].slot_info.get(slot or "", {}).get("occluder")) if loc in self.places else False
        if hard and not truth:
            fp = max(fp, self.p.det_fp_hard)
        return self._noisy(truth, close, fp), truth, {"location": loc, "slot": slot}

    _check_on_hook = _check_on_slot

    def _check_delivered(self, robot: SimRobot, args: dict, close: bool):
        sku, loc = args.get("sku"), args.get("location")
        recent = [d for d in self.deliveries if d["to"] == loc]
        truth = any(d["sku"] == sku for d in recent)
        fp = self.p.det_fp_other
        if not truth and any(self._group_of(d["sku"]) and self._group_of(d["sku"]) == self._group_of(sku) for d in recent):
            fp = self.p.det_fp_lookalike
        return self._noisy(truth, close, fp), truth, {"location": loc}

    def _check_surface_clean(self, robot: SimRobot, args: dict, close: bool):
        place = self.places.get(args.get("location", ""))
        if place is None:
            return False, False, {}
        truth = not place.state.get("dirty") and not place.state.get("edge_dirty")
        fp = self.p.det_fp_hard if place.state.get("edge_dirty") and not place.state.get("dirty") else self.p.det_fp_other
        return self._noisy(truth, close, fp), truth, {"location": place.loc_id}

    def _check_cloth_coverage(self, robot: SimRobot, args: dict, close: bool):
        loc = args.get("location")
        cloths = [t for t in self.things_at(loc) if t.category == "cloth"]
        cov = max((t.state.get("coverage", 0.0) for t in cloths), default=0.0)
        truth = cov >= float(args.get("min", 0.9))
        fp = self.p.det_fp_hard if cov >= 0.75 else self.p.det_fp_other
        return self._noisy(truth, close, fp), truth, {"location": loc}

    def _check_item_found(self, robot: SimRobot, args: dict, close: bool):
        sku, loc = args.get("sku"), args.get("location")
        truth = any(t.sku == sku for t in self.things_at(loc))
        fp = self.p.det_fp_lookalike if any(t.group and t.group == self._group_of(sku) for t in self.things_at(loc)) else self.p.det_fp_other
        return self._noisy(truth, close, fp), truth, {"location": loc}

    def _check_trash_cleared(self, robot: SimRobot, args: dict, close: bool):
        loc = args.get("location")
        trash = [t for t in self.things_at(loc) if t.category == "trash"]
        truth = not trash
        fp = self.p.det_fp_hard if trash and all("small" in t.tags for t in trash) else self.p.det_fp_other
        return self._noisy(truth, close, fp), truth, {"location": loc}

    def _check_in_bin(self, robot: SimRobot, args: dict, close: bool):
        loc = args.get("location", "bin")
        sku = args.get("sku")
        truth = any(t.sku == sku for t in self.things_at(loc))
        return self._noisy(truth, close, self.p.det_fp_other), truth, {"location": loc}

    # --------------------------------------------------------------- helpers

    def _group_of(self, sku: str | None) -> str | None:
        for t in self.things.values():
            if t.sku == sku:
                return t.group
        return None

    def _outage(self, robot_id: str, t: float) -> Outage | None:
        for o in self.outages:
            if o.robot_id != robot_id:
                continue
            if o.after_calls is not None and self.robots[robot_id].calls >= o.after_calls:
                o.start, o.end, o.after_calls = t, t + o.duration, None  # the crash happens now
            if o.after_calls is None and o.start <= t < o.end:
                return o
        return None

    def _intrusion_gate(self, robot: SimRobot, g: Guidance, duration: float) -> float | _Step:
        """Returns extra waiting time, or a failed step if the policy kept moving near a person."""
        start, end = robot.clock, robot.clock + duration
        for it in self.intrusions:
            if it.location == robot.location and it.start < end and start < it.end:
                if pol.pause_on_intrusion(g):
                    return max(0.0, it.end - max(start, it.start)) + 1.0
                if self.rng.random() < self.p.intrusion_fail:
                    self.safety_violations.append({"robot": robot.robot_id, "location": robot.location, "t": start})
                    return _Step(
                        False,
                        duration * 0.5,
                        FailureReason(
                            "workspace_intrusion",
                            "a person entered the workspace; motion aborted",
                            evidence={"person_in_workspace": True},
                        ),
                        progress=0.2,
                    )
        return 0.0

    def _visibility(self, robot: SimRobot, thing: Thing, cam: str) -> float:
        if cam == "wrist" and "wrist_camera" in robot.features:
            return thing.vis_wrist
        vis = thing.vis_head
        if "head_waist" in robot.features and ("back_row" in thing.tags or "low" in thing.tags):
            vis = max(vis, self.p.active_perception_vis)
        return vis

    def _label(self, thing: Thing, loc: str, cam: str, refs: list[str] | None = None) -> str:
        """Detector label for a thing; look-alikes may be confused."""
        siblings = [t for t in self.things_at(loc) if self.lookalike(t, thing)]
        if not siblings:
            catalog_twins = sorted({t.sku for t in self.things.values() if self.lookalike(t, thing)})
            if not catalog_twins:
                return thing.sku
            siblings_skus = catalog_twins
        else:
            siblings_skus = sorted({t.sku for t in siblings})
        rate = self.p.det_confusion_wrist if cam == "wrist" else self.p.det_confusion_head
        if refs and thing.sku in refs:
            rate *= 0.5
        if self.rng.random() < rate:
            return self.rng.choice(siblings_skus)
        return thing.sku

    def _policy_identify(self, robot: SimRobot, g: Guidance, args: dict, cands: list[Thing]) -> Thing | None:
        target = args.get("sku")
        if not target and args.get("object"):
            obj = self.things.get(args["object"])
            target = obj.sku if obj else None
        refs = pol.reference_skus(g)
        use_ref = bool(refs) and "object_reference" in robot.features
        sf = pol.scale_factor(self.policy.training_scale)
        wanted = refs[0] if use_ref else target
        by_sku = [t for t in cands if t.sku == wanted]
        if use_ref:
            seen = wanted in self.policy.trained_skus
            p_id = self.p.id_reference_max * (1.0 if seen else 0.1 + 0.9 * sf)
        elif target in self.policy.trained_skus:
            p_id = self.p.id_language_seen
            if any(t.group and t.sku != target and t.group == self._group_of(target) for t in cands):
                p_id *= self.p.lookalike_language_factor
        else:
            p_id = self.p.novel_text_max * sf
        if by_sku and self.rng.random() < p_id:
            return by_sku[0]
        others = [t for t in cands if t.sku != wanted]
        lookalikes = [t for t in others if t.group and t.group == self._group_of(wanted)]
        if lookalikes:
            return self.rng.choice(lookalikes)
        same_cat = [t for t in others if by_sku and t.category == by_sku[0].category]
        pool = same_cat or others
        if pool and self.rng.random() < 0.6:
            return self.rng.choice(pool)
        return None

    def _thing_at_box(self, loc: str, coords: list[float]) -> Thing | None:
        x0, y0, x1, y1 = coords
        best, best_d = None, None
        for t in self.things_at(loc):
            cx, cy = t.pos
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                d = (cx - (x0 + x1) / 2) ** 2 + (cy - (y0 + y1) / 2) ** 2
                if best_d is None or d < best_d:
                    best, best_d = t, d
        return best

    def _box(self, thing: Thing) -> list[float]:
        cx, cy = thing.pos
        h = thing.size / 2
        return [round(max(0.0, cx - h), 3), round(max(0.0, cy - h), 3), round(min(1.0, cx + h), 3), round(min(1.0, cy + h), 3)]

    def _assign_pos(self, thing: Thing) -> None:
        place = self.places.get(thing.location)
        if place is None:
            return
        if thing.slot and thing.slot in place.slots:
            thing.pos = slot_center(place.slots, thing.slot)
            return
        taken = [t.pos for t in self.things_at(place.loc_id) if t.oid != thing.oid]
        for _ in range(50):
            pos = (round(self.rng.uniform(0.1, 0.9), 3), round(self.rng.uniform(0.15, 0.85), 3))
            if all(math.dist(pos, q) > 0.12 for q in taken):
                break
        thing.pos = pos

    def _neighbor_slot(self, place: Place, slot: str) -> str | None:
        if slot not in place.slots or len(place.slots) < 2:
            return None
        i = place.slots.index(slot)
        j = i + 1 if i + 1 < len(place.slots) else i - 1
        return place.slots[j]

    def _robot_state(self, robot: SimRobot) -> dict[str, Any]:
        held = self._held(robot)
        state: dict[str, Any] = {
            "location": robot.location,
            "holding": held is not None,
            "gripper": "closed" if held is not None else "open",
            "clock_s": round(robot.clock, 1),
        }
        if held is not None:
            # Quick wrist-camera glance at the held object; look-alikes may be mislabeled.
            confusion = self.p.det_confusion_head if any(self.lookalike(held, t) for t in self.things.values()) else 0.0
            if confusion and self.rng.random() < confusion * 0.6:
                twins = sorted({t.sku for t in self.things.values() if self.lookalike(held, t)})
                state["held_label"] = self.rng.choice(twins)
            else:
                state["held_label"] = held.sku
        return state

    def _observation(self, robot: SimRobot, camera: str) -> Observation:
        cam = "wrist" if camera.startswith("wrist") and "wrist_camera" in robot.features else "head"
        loc = robot.location
        detections = []
        for thing in self.things_at(loc):
            vis = self._visibility(robot, thing, cam)
            if self.rng.random() >= vis:
                continue
            label = self._label(thing, loc, cam)
            attrs = {}
            if thing.occluder:
                attrs["partially_occluded"] = "yes"
            detections.append(
                Detection(
                    label=label,
                    ref_id=label,
                    bbox=self._box(thing),
                    confidence=round(min(0.99, vis * self.rng.uniform(0.85, 1.0)), 2),
                    camera=cam,
                    attributes=attrs,
                    track_id=thing.oid,
                )
            )
        flags = []
        t = robot.clock
        if any(it.location == loc and it.start <= t < it.end for it in self.intrusions):
            flags.append("person_in_workspace")
        place = self.places.get(loc)
        if place is not None and place.state.get("dirty"):
            flags.append("surface_dirty")
        return Observation(
            robot_id=robot.robot_id,
            t=round(t, 2),
            camera=cam,
            location=loc,
            detections=detections,
            robot_state=self._robot_state(robot),
            flags=flags,
        )

    def _feedback(self, robot: SimRobot, req: ExecutionRequest, mode: ExecutionMode, step: _Step) -> Feedback:
        outcome = step.outcome or (Outcome.SUCCESS if step.success else Outcome.FAILURE)
        details = dict(step.details)
        obs = self._observation(robot, step.camera)
        return Feedback(
            request_id=req.request_id or "",
            robot_id=robot.robot_id,
            success=step.success,
            observation=obs,
            action_result=ActionResult(
                skill=req.skill,
                mode=mode,
                outcome=outcome,
                duration_s=round(step.duration, 2),
                details=details,
            ),
            failure_reason=step.failure,
            progress=1.0 if step.success else step.progress,
            task_id=req.task_id,
            step_id=req.step_id,
            policy_success=step.success,
        )
