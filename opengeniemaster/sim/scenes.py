"""Scenario builders for the semantic simulator.

Each builder returns a :class:`Scenario`: a populated :class:`SimWorld`, the
business records an agent may read, a robot fleet, the task(s) to run and
ground-truth goals used only for grading.

The six benchmark scenarios mirror the task families reported for
GenieMaster (hosting guests, tablecloth spreading, office-supply search,
item retrieval, restocking, cleaning). Layouts, products and difficulty
factors are invented for simulation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from ..domain.records import BusinessRecords, CatalogItem, Order, PlaceInfo
from ..embodiment.profiles import PROFILES
from .policy import PolicyCard, SimParams
from .world import Intrusion, Outage, Place, SimRobot, SimWorld, Thing

DEFAULT_FLEET = (("wh-01", "wheeled_humanoid"), ("wh-02", "wheeled_humanoid"), ("cart-01", "dual_arm_cart"))


@dataclass
class TaskSpec:
    goal: str
    domain: str
    workflow: str
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 0


@dataclass
class Goal:
    kind: str  # at_slot | at_location | delivered | in_bin | not_in_bin | clean | coverage
    sku: str | None = None
    location: str | None = None
    slot: str | None = None
    value: float | None = None


@dataclass
class Grade:
    success: bool
    completion: float
    goals: list[dict[str, Any]]
    safety_violations: int = 0


@dataclass
class Scenario:
    name: str
    domain: str
    world: SimWorld
    records: BusinessRecords
    fleet: list[tuple[str, str]]
    tasks: list[TaskSpec]
    goals: list[Goal]
    meta: dict[str, Any] = field(default_factory=dict)

    def grade(self) -> Grade:
        results = []
        for g in self.goals:
            ok = _goal_met(self.world, g)
            results.append({"kind": g.kind, "sku": g.sku, "location": g.location, "slot": g.slot, "met": ok})
        met = sum(1 for r in results if r["met"])
        completion = met / len(results) if results else 1.0
        safety = len(self.world.safety_violations)
        return Grade(success=bool(results) and met == len(results), completion=completion, goals=results, safety_violations=safety)


def _goal_met(world: SimWorld, g: Goal) -> bool:
    if g.kind == "at_slot":
        return any(t.sku == g.sku and t.slot == g.slot for t in world.things_at(g.location or ""))
    if g.kind == "at_location":
        return any(t.sku == g.sku for t in world.things_at(g.location or ""))
    if g.kind == "delivered":
        return any(d["sku"] == g.sku and d["to"] == g.location for d in world.deliveries)
    if g.kind == "in_bin":
        return any(t.sku == g.sku for t in world.things_at(g.location or "bin"))
    if g.kind == "not_in_bin":
        return not any(t.sku == g.sku for t in world.things_at(g.location or "bin"))
    if g.kind == "clean":
        place = world.places[g.location or ""]
        return not place.state.get("dirty") and not place.state.get("edge_dirty")
    if g.kind == "coverage":
        cloths = [t for t in world.things_at(g.location or "") if t.category == "cloth"]
        return max((t.state.get("coverage", 0.0) for t in cloths), default=0.0) >= (g.value or 0.9)
    raise ValueError(f"unknown goal kind {g.kind}")


# ---------------------------------------------------------------------------
# Catalog content
# ---------------------------------------------------------------------------

# sku, name, category, group, attributes, mount, tags
_STORE_PRODUCTS: list[tuple[str, str, str, str | None, dict[str, str], str, list[str]]] = [
    ("water-spring-500", "spring water 500ml", "drink", "water-500", {"color": "clear", "cap": "blue"}, "shelf", []),
    ("water-sparkling-500", "sparkling water 500ml", "drink", "water-500", {"color": "clear", "cap": "green"}, "shelf", []),
    ("water-electrolyte-500", "electrolyte water 500ml", "drink", "water-500", {"color": "clear", "cap": "white"}, "shelf", []),
    ("tea-green-500", "green tea 500ml", "drink", "tea-500", {"label": "green"}, "shelf", []),
    ("tea-oolong-500", "oolong tea 500ml", "drink", "tea-500", {"label": "brown"}, "shelf", []),
    ("tea-jasmine-500", "jasmine tea 500ml", "drink", "tea-500", {"label": "light green"}, "shelf", []),
    ("cola-330", "cola 330ml can", "drink", "can-330", {"color": "red"}, "shelf", []),
    ("cola-zero-330", "zero cola 330ml can", "drink", "can-330", {"color": "black"}, "shelf", []),
    ("soda-lemon-330", "lemon soda 330ml can", "drink", "can-330", {"color": "yellow"}, "shelf", []),
    ("soda-peach-330", "limited peach soda 330ml can", "drink", "can-330", {"color": "pink"}, "shelf", ["new"]),
    ("chips-salted-70", "salted chips 70g", "snack", "chips-70", {"color": "yellow bag"}, "shelf", []),
    ("chips-bbq-70", "bbq chips 70g", "snack", "chips-70", {"color": "red bag"}, "shelf", []),
    ("chips-seaweed-70", "seaweed chips 70g", "snack", "chips-70", {"color": "green bag"}, "shelf", []),
    ("noodle-beef", "beef noodle cup", "food", "noodle-cup", {"color": "red lid"}, "shelf", []),
    ("noodle-seafood", "seafood noodle cup", "food", "noodle-cup", {"color": "blue lid"}, "shelf", []),
    ("latte-matcha-280", "matcha latte 280ml", "drink", None, {"color": "green carton"}, "shelf", ["new"]),
    ("choco-bar", "chocolate bar", "snack", None, {"color": "brown wrapper"}, "shelf", ["small"]),
    ("tissues-pack", "pocket tissues", "daily", None, {"color": "white"}, "shelf", ["small"]),
    ("gum-mint", "mint gum pack", "snack", "gum-pack", {"color": "blue"}, "hook", ["small"]),
    ("gum-fruit", "fruit gum pack", "snack", "gum-pack", {"color": "pink"}, "hook", ["small"]),
    ("cable-usbc", "USB-C cable", "electronics", "cable", {"color": "white box"}, "hook", []),
    ("cable-lightning", "Lightning cable", "electronics", "cable", {"color": "white box"}, "hook", []),
    ("umbrella-travel", "travel umbrella", "daily", None, {"color": "navy"}, "hook", ["new"]),
    ("phone-stand", "folding phone stand", "electronics", None, {"color": "grey"}, "hook", ["small"]),
]

_TRASH = [
    ("trash:paper-cup", "used paper coffee cup", "cup"),
    ("trash:wrapper", "snack wrapper", None),
    ("trash:can", "empty can", "can-330"),
]


def _store_layout() -> list[Place]:
    hooks = [f"hook-{i}" for i in range(1, 9)]
    shelf_slots = [f"r{r}c{c}" for r in range(1, 4) for c in range(1, 5)]
    return [
        Place("dock", "dock", (1.0, 0.0)),
        Place("counter", "counter", (1.0, 4.0), people=True),
        Place("shelf_A", "shelf", (4.0, 2.0), slots=list(shelf_slots)),
        Place("shelf_B", "shelf", (4.0, 5.0), slots=list(shelf_slots)),
        Place("hook_wall", "hook_wall", (7.0, 2.0), slots=hooks),
        Place("storage", "storage", (10.0, 1.0)),
        Place("table_1", "table", (2.0, 7.0), people=True),
        Place("table_2", "table", (4.5, 8.0), people=True),
        Place("bin", "bin", (0.0, 7.0)),
    ]


def _store_records(places: list[Place], policy: PolicyCard) -> BusinessRecords:
    rec = BusinessRecords()
    descriptions = {
        "dock": "robot charging dock",
        "counter": "checkout counter; customers wait here",
        "shelf_A": "drinks shelf, 3 rows x 4 columns",
        "shelf_B": "snacks and food shelf, 3 rows x 4 columns",
        "hook_wall": "peg wall with 8 hooks for hanging items; price tags hang below hooks",
        "storage": "back-room stock area, unsorted boxes",
        "table_1": "eat-in table near the window",
        "table_2": "eat-in table at the back",
        "bin": "trash bin",
    }
    for p in places:
        tags = []
        if p.kind == "hook_wall":
            tags.append("hooks")
        if p.people:
            tags.append("customer_area")
        rec.places[p.loc_id] = PlaceInfo(p.loc_id, p.kind, descriptions.get(p.loc_id, ""), tags, list(p.slots))
    drinks = [s for s, *_rest in _STORE_PRODUCTS if _rest[1] == "drink"]
    shelf_a = iter(rec.places["shelf_A"].slots)
    shelf_b = iter(rec.places["shelf_B"].slots)
    hooks = iter(rec.places["hook_wall"].slots)
    for sku, name, cat, group, attrs, mount, tags in _STORE_PRODUCTS:
        if mount == "hook":
            home, slot = "hook_wall", next(hooks)
        elif sku in drinks:
            home, slot = "shelf_A", next(shelf_a)
        else:
            home, slot = "shelf_B", next(shelf_b)
        item_tags = list(tags) + (["hook"] if mount == "hook" else [])
        rec.catalog[sku] = CatalogItem(
            sku=sku,
            name=name,
            category=cat,
            attributes=dict(attrs),
            image=f"sim://catalog/{sku}.png",
            group=group,
            home=home,
            slot=slot,
            mount=mount,
            tags=item_tags,
            stock_location="storage",
        )
    for sku, name, group in _TRASH:
        rec.catalog[sku] = CatalogItem(sku=sku, name=name, category="trash", group=group, image=f"sim://catalog/{sku}.png", mount="none")
    rec.catalog["coffee-cup-new"] = CatalogItem(
        sku="coffee-cup-new",
        name="sealed iced coffee cup (customer purchase)",
        category="drink",
        group="cup",
        attributes={"color": "brown", "lid": "sealed"},
        image="sim://catalog/coffee-cup-new.png",
        mount="none",
    )
    return rec


def _policy_for(records: BusinessRecords, *, training_scale: int, seen: set[str] | None = None, flaws: dict[str, float] | None = None) -> PolicyCard:
    if seen is None:
        seen = {s for s, it in records.catalog.items() if "new" not in it.tags}
    return PolicyCard(name="sim-vla", trained_skus=set(seen), training_scale=training_scale, flaws=dict(flaws or {}))


def _robots(fleet: tuple[tuple[str, str], ...] | list[tuple[str, str]], start: str, features: dict[str, set[str]] | None = None) -> list[SimRobot]:
    out = []
    for rid, model in fleet:
        feats = set(features.get(rid, PROFILES[model].features)) if features else set(PROFILES[model].features)
        out.append(SimRobot(robot_id=rid, model=model, features=feats, location=start))
    return out


def _thing(world_things: list[Thing], counter: list[int], records: BusinessRecords, sku: str, location: str, slot: str | None = None, **kw: Any) -> Thing:
    counter[0] += 1
    item = records.catalog.get(sku)
    t = Thing(
        oid=f"obj-{counter[0]:03d}",
        sku=sku,
        location=location,
        slot=slot,
        group=item.group if item else None,
        category=item.category if item else "item",
        tags=set(kw.pop("tags", set())),
        **kw,
    )
    if item and "small" in item.tags:
        t.tags.add("small")
    world_things.append(t)
    return t


def _random_intrusions(rng: random.Random, locations: list[str], rate: float, horizon: float) -> list[Intrusion]:
    out = []
    for loc in locations:
        if rng.random() < rate:
            start = rng.uniform(0.0, horizon)
            out.append(Intrusion(loc, start, start + rng.uniform(15.0, 45.0)))
    return out


def _random_outages(rng: random.Random, robot_ids: list[str], rate: float, horizon: float) -> list[Outage]:
    out = []
    for rid in robot_ids:
        if rng.random() < rate:
            start = rng.uniform(20.0, horizon)
            out.append(Outage(rid, start, start + rng.uniform(30.0, 90.0)))
    return out


def _store_world(seed: int, *, fleet, training_scale: int, flaws: dict[str, float] | None, params: SimParams | None,
                 robot_features: dict[str, set[str]] | None = None, seen: set[str] | None = None):
    rng = random.Random(seed)
    places = _store_layout()
    records = _store_records(places, PolicyCard())
    policy = _policy_for(records, training_scale=training_scale, seen=seen, flaws=flaws)
    by_id = {p.loc_id: p for p in places}
    # Some hooks are small and partly hidden by price tags (head camera resolution is not enough).
    for hook in by_id["hook_wall"].slots:
        if rng.random() < 0.6:
            by_id["hook_wall"].slot_info[hook] = {"vis_head": 0.35, "vis_wrist": 0.95, "occluder": "price_tag"}
        else:
            by_id["hook_wall"].slot_info[hook] = {"vis_head": 0.9, "vis_wrist": 0.97}
    things: list[Thing] = []
    counter = [0]
    for sku, item in records.catalog.items():
        if item.home:
            _thing(things, counter, records, sku, item.home, item.slot)
    return rng, places, records, policy, things, counter, by_id


def _finish(name: str, domain: str, seed: int, places, things, records, policy, fleet, start, tasks, goals, *,
            params=None, intrusions=None, outages=None, robot_features=None, meta=None) -> Scenario:
    world = SimWorld(
        seed=seed * 7919 + 17,
        places=places,
        things=things,
        robots=_robots(fleet, start, robot_features),
        policy=policy,
        params=params,
        intrusions=intrusions,
        outages=outages,
    )
    return Scenario(name=name, domain=domain, world=world, records=records, fleet=list(fleet), tasks=tasks, goals=goals, meta=dict(meta or {}))


# ---------------------------------------------------------------------------
# Store scenarios
# ---------------------------------------------------------------------------


def store_restocking(seed: int, *, n_items: int = 3, fleet=DEFAULT_FLEET, training_scale: int = 1000,
                     intrusion_rate: float = 0.25, outage_rate: float = 0.08, flaws: dict[str, float] | None = None,
                     params: SimParams | None = None) -> Scenario:
    rng, places, records, policy, things, counter, by_id = _store_world(
        seed, fleet=fleet, training_scale=training_scale, flaws=flaws if flaws is not None else {"release_on_retract": 0.2}, params=params
    )
    hook_items = [s for s, it in records.catalog.items() if it.mount == "hook"]
    shelf_items = [s for s, it in records.catalog.items() if it.mount == "shelf"]
    chosen = rng.sample(hook_items, 1) + rng.sample(shelf_items, max(0, n_items - 1))
    # Remove the chosen items from the shelves and keep stock (plus look-alike clutter) in storage.
    things[:] = [t for t in things if t.sku not in chosen]
    for sku in chosen:
        _thing(things, counter, records, sku, "storage")
        for twin in records.lookalikes(sku)[:2]:
            _thing(things, counter, records, twin, "storage")
    goals = [Goal("at_slot", sku, records.catalog[sku].home, records.catalog[sku].slot) for sku in chosen]
    task = TaskSpec(goal=f"Restock missing items: {', '.join(chosen)}", domain="store", workflow="restock",
                    params={"items": [{"sku": s} for s in chosen]})
    intr = _random_intrusions(rng, ["shelf_A", "shelf_B", "hook_wall"], intrusion_rate, 200.0)
    outs = _random_outages(rng, [r for r, _ in fleet], outage_rate, 150.0)
    return _finish("restocking", "store", seed, places, things, records, policy, fleet, "dock", [task], goals,
                   params=params, intrusions=intr, outages=outs, meta={"items": chosen})


def store_item_retrieval(seed: int, *, n_items: int = 2, fleet=DEFAULT_FLEET, training_scale: int = 1000,
                         novel_ratio: float = 0.3, back_row_rate: float = 0.0, intrusion_rate: float = 0.15,
                         outage_rate: float = 0.05, flaws: dict[str, float] | None = None, params: SimParams | None = None,
                         robot_features: dict[str, set[str]] | None = None, fixed_location: bool = False,
                         seen: set[str] | None = None, novel_only: bool = False, seen_only: bool = False,
                         deliver_by: str = "handover", require_group: bool = True) -> Scenario:
    rng, places, records, policy, things, counter, by_id = _store_world(
        seed, fleet=fleet, training_scale=training_scale, flaws=flaws if flaws is not None else {"release_on_retract": 0.15},
        params=params, seen=seen,
    )
    candidates = [s for s, it in records.catalog.items() if it.mount == "shelf" and (it.group or not require_group)]
    novel = [s for s in candidates if s not in policy.trained_skus]
    known = [s for s in candidates if s in policy.trained_skus]
    items: list[str] = []
    for _ in range(n_items):
        use_novel = (novel_only or (not seen_only and rng.random() < novel_ratio)) and novel
        pool = [s for s in (novel if use_novel else known) if s not in items]
        if not pool:
            pool = [s for s in candidates if s not in items]
        items.append(rng.choice(pool))
    for t in things:
        if t.sku in items and back_row_rate and rng.random() < back_row_rate:
            t.tags.add("back_row")
            t.vis_head = 0.4
    goals = [Goal("delivered" if deliver_by == "handover" else "at_location", sku, "counter") for sku in items]
    task = TaskSpec(goal=f"Customer order: bring {', '.join(items)} to the counter", domain="store", workflow="retrieve",
                    params={"items": items, "deliver_to": "counter", "deliver_by": deliver_by})
    intr = _random_intrusions(rng, ["shelf_A", "shelf_B"], intrusion_rate, 150.0)
    outs = _random_outages(rng, [r for r, _ in fleet], outage_rate, 120.0)
    start = "shelf_A" if fixed_location else "dock"
    return _finish("item_retrieval", "store", seed, places, things, records, policy, fleet, start, [task], goals,
                   params=params, intrusions=intr, outages=outs, robot_features=robot_features, meta={"items": items})


def store_cleaning(seed: int, *, fleet=DEFAULT_FLEET, training_scale: int = 1000, intrusion_rate: float = 0.35,
                   outage_rate: float = 0.05, flaws: dict[str, float] | None = None, params: SimParams | None = None) -> Scenario:
    rng, places, records, policy, things, counter, by_id = _store_world(
        seed, fleet=fleet, training_scale=training_scale, flaws=flaws if flaws is not None else {"release_on_retract": 0.1}, params=params
    )
    trash_kinds = [s for s in records.catalog if s.startswith("trash:")]
    tables = ["table_1", "table_2"]
    trash_list = []
    for loc in tables:
        by_id[loc].state["dirty"] = True
        for sku in rng.sample(trash_kinds, 2):
            t = _thing(things, counter, records, sku, loc, tags={"small"} if sku == "trash:wrapper" else set())
            trash_list.append({"sku": sku, "location": loc})
    # A customer's sealed purchase sits on table_1 and must not be thrown away.
    _thing(things, counter, records, "coffee-cup-new", "table_1")
    goals = [Goal("in_bin", t["sku"], "bin") for t in trash_list]
    goals += [Goal("clean", None, loc) for loc in tables]
    goals.append(Goal("not_in_bin", "coffee-cup-new", "bin"))
    task = TaskSpec(goal="Clean the eat-in tables: bin the trash and wipe the surfaces", domain="store", workflow="clean",
                    params={"locations": tables, "trash": trash_list})
    intr = _random_intrusions(rng, tables, intrusion_rate, 200.0)
    outs = _random_outages(rng, [r for r, _ in fleet], outage_rate, 150.0)
    return _finish("cleaning", "store", seed, places, things, records, policy, fleet, "dock", [task], goals,
                   params=params, intrusions=intr, outages=outs, meta={"trash": trash_list})


# ---------------------------------------------------------------------------
# Office scenario
# ---------------------------------------------------------------------------

_OFFICE_ITEMS = [
    ("notebook-a5", "black A5 notebook", "stationery", "black-notebook", {"color": "black", "size": "A5"}),
    ("planner-black", "black weekly planner", "stationery", "black-notebook", {"color": "black", "size": "A5"}),
    ("earphone-case", "earphone case", "electronics", "round-silver", {"color": "silver-white", "shape": "round"}),
    ("mint-tin", "silver mint tin", "snack", "round-silver", {"color": "silver", "shape": "round"}),
    ("charger-puck", "white wireless charger", "electronics", "round-silver", {"color": "white", "shape": "round"}),
    ("tissues-box", "box of tissues", "daily", None, {"color": "white"}),
    ("stapler", "red stapler", "stationery", None, {"color": "red"}),
    ("marker-set", "whiteboard marker set", "stationery", None, {"color": "mixed"}),
]


def office_supply_search(seed: int, *, n_items: int = 3, fleet=DEFAULT_FLEET, training_scale: int = 1000,
                         intrusion_rate: float = 0.35, outage_rate: float = 0.08, flaws: dict[str, float] | None = None,
                         params: SimParams | None = None) -> Scenario:
    rng = random.Random(seed)
    places = [
        Place("dock", "dock", (0.0, 3.0)),
        Place("user_desk", "desk", (0.0, 0.0), people=True),
        Place("desk_2", "desk", (3.0, 0.0), people=True),
        Place("desk_3", "desk", (6.0, 0.0)),
        Place("bar_counter", "counter", (4.0, 4.0)),
        Place("cabinet", "cabinet", (7.0, 3.0)),
        Place("meeting_table", "table", (2.0, 5.0)),
    ]
    rec = BusinessRecords()
    desc = {
        "user_desk": "the requester's desk",
        "desk_2": "shared desk, often cluttered at the edge",
        "desk_3": "hot desk",
        "bar_counter": "pantry bar counter",
        "cabinet": "supply cabinet",
        "meeting_table": "meeting room table",
        "dock": "robot dock",
    }
    for p in places:
        rec.places[p.loc_id] = PlaceInfo(p.loc_id, p.kind, desc.get(p.loc_id, ""), ["office"] + (["people"] if p.people else []))
    likely = {
        "notebook-a5": ["desk_2", "meeting_table", "cabinet"],
        "earphone-case": ["desk_2", "user_desk", "bar_counter"],
        "tissues-box": ["cabinet", "bar_counter"],
        "stapler": ["cabinet", "desk_3"],
        "marker-set": ["meeting_table", "cabinet"],
    }
    for sku, name, cat, group, attrs in _OFFICE_ITEMS:
        rec.catalog[sku] = CatalogItem(sku=sku, name=name, category=cat, attributes=dict(attrs), group=group,
                                       image=f"sim://catalog/{sku}.png", mount="none", likely_locations=likely.get(sku, []))
    policy = _policy_for(rec, training_scale=training_scale, flaws=flaws if flaws is not None else {"release_on_retract": 0.3})
    things: list[Thing] = []
    counter = [0]
    wanted_pool = ["notebook-a5", "earphone-case", "tissues-box", "stapler", "marker-set"]
    wanted = ["earphone-case"] + rng.sample([w for w in wanted_pool if w != "earphone-case"], max(0, n_items - 1))
    # The earphone case is really at the bar counter; desk_2's edge has a look-alike tin.
    true_loc = {
        "earphone-case": "bar_counter",
        "notebook-a5": rng.choice(["meeting_table", "desk_2"]),
        "tissues-box": rng.choice(["cabinet", "bar_counter"]),
        "stapler": rng.choice(["cabinet", "desk_3"]),
        "marker-set": rng.choice(["meeting_table", "cabinet"]),
    }
    for sku in wanted_pool:
        tags = {"edge"} if rng.random() < 0.3 else set()
        _thing(things, counter, rec, sku, true_loc[sku], tags=tags)
    _thing(things, counter, rec, "mint-tin", "desk_2", tags={"edge"}, vis_head=0.8)
    _thing(things, counter, rec, "charger-puck", "user_desk", tags={"edge"})
    _thing(things, counter, rec, "planner-black", "meeting_table" if true_loc["notebook-a5"] != "meeting_table" else "desk_2")
    goals = [Goal("at_location", sku, "user_desk") for sku in wanted]
    task = TaskSpec(goal=f"Bring {', '.join(wanted)} to my desk", domain="office", workflow="fetch_items",
                    params={"items": wanted, "deliver_to": "user_desk"})
    intr = _random_intrusions(rng, ["user_desk", "desk_2", "bar_counter"], intrusion_rate, 250.0)
    outs = _random_outages(rng, [r for r, _ in fleet], outage_rate, 200.0)
    return _finish("office_supply_search", "office", seed, places, things, rec, policy, fleet, "dock", [task], goals,
                   params=params, intrusions=intr, outages=outs, meta={"items": wanted})


# ---------------------------------------------------------------------------
# Home scenarios
# ---------------------------------------------------------------------------

_HOME_ITEMS = [
    ("cup-blue", "blue cup of water", "drink", "cup-family", {"color": "blue"}),
    ("cup-teal", "teal cup of juice", "drink", "cup-family", {"color": "teal"}),
    ("mug-navy", "navy mug of tea", "drink", "cup-family", {"color": "navy"}),
    ("cookies", "plate of cookies", "snack", None, {"color": "brown"}),
    ("fruit-bowl", "small fruit bowl", "snack", None, {"color": "mixed"}),
    ("can-sparkling", "sparkling water can", "drink", "home-can", {"color": "silver"}),
    ("can-ginger", "ginger ale can", "drink", "home-can", {"color": "green"}),
    ("tablecloth", "white tablecloth (folded)", "cloth", None, {"color": "white"}),
]


def _home_base(seed: int, training_scale: int, flaws):
    places = [
        Place("dock", "dock", (0.0, 2.0)),
        Place("pantry", "pantry", (0.0, 0.0)),
        Place("fridge", "fridge", (1.5, 0.0)),
        Place("kitchen_counter", "counter", (3.0, 0.0)),
        Place("sofa", "seating", (6.0, 3.0), people=True),
        Place("dining_table", "table", (3.0, 5.0), people=True),
        Place("cabinet", "cabinet", (0.0, 4.5)),
    ]
    rec = BusinessRecords()
    for p in places:
        rec.places[p.loc_id] = PlaceInfo(p.loc_id, p.kind, "", ["home"] + (["guests"] if p.people else []))
    homes = {"cup-blue": "kitchen_counter", "cup-teal": "kitchen_counter", "mug-navy": "kitchen_counter",
             "cookies": "pantry", "fruit-bowl": "pantry", "can-sparkling": "fridge", "can-ginger": "fridge",
             "tablecloth": "cabinet"}
    for sku, name, cat, group, attrs in _HOME_ITEMS:
        rec.catalog[sku] = CatalogItem(sku=sku, name=name, category=cat, attributes=dict(attrs), group=group,
                                       image=f"sim://catalog/{sku}.png", mount="none", home=homes[sku], likely_locations=[homes[sku]])
    policy = _policy_for(rec, training_scale=training_scale, flaws=flaws)
    things: list[Thing] = []
    counter = [0]
    for sku, item in rec.catalog.items():
        _thing(things, counter, rec, sku, item.home or "pantry")
    return places, rec, policy, things


def home_hosting_guests(seed: int, *, n_guests: int = 3, fleet=DEFAULT_FLEET, training_scale: int = 1000,
                        intrusion_rate: float = 0.6, outage_rate: float = 0.05, flaws: dict[str, float] | None = None,
                        params: SimParams | None = None) -> Scenario:
    rng = random.Random(seed)
    places, rec, policy, things = _home_base(seed, training_scale, flaws if flaws is not None else {"release_on_retract": 0.15})
    servable = ["cup-blue", "cup-teal", "mug-navy", "cookies", "fruit-bowl", "can-sparkling", "can-ginger"]
    orders = []
    used: set[str] = set()
    for i in range(n_guests):
        sku = rng.choice([s for s in servable if s not in used])
        used.add(sku)
        orders.append({"guest": f"guest-{i + 1}", "sku": sku, "location": rng.choice(["sofa", "dining_table"])})
    goals = [Goal("delivered", o["sku"], o["location"]) for o in orders]
    task = TaskSpec(goal="Serve each guest what they asked for", domain="home", workflow="serve_guests", params={"orders": orders})
    intr = _random_intrusions(rng, ["sofa", "dining_table", "kitchen_counter"], intrusion_rate, 250.0)
    outs = _random_outages(rng, [r for r, _ in fleet], outage_rate, 200.0)
    return _finish("hosting_guests", "home", seed, places, things, rec, policy, fleet, "dock", [task], goals,
                   params=params, intrusions=intr, outages=outs, meta={"orders": orders})


def home_tablecloth(seed: int, *, fleet=DEFAULT_FLEET, training_scale: int = 1000, intrusion_rate: float = 0.25,
                    outage_rate: float = 0.05, flaws: dict[str, float] | None = None, params: SimParams | None = None) -> Scenario:
    rng = random.Random(seed)
    places, rec, policy, things = _home_base(seed, training_scale, flaws if flaws is not None else {})
    goals = [Goal("coverage", "tablecloth", "dining_table", value=0.9)]
    task = TaskSpec(goal="Spread the tablecloth on the dining table", domain="home", workflow="spread_tablecloth",
                    params={"cloth": "tablecloth", "table": "dining_table"})
    intr = _random_intrusions(rng, ["dining_table"], intrusion_rate, 150.0)
    outs = _random_outages(rng, [r for r, _ in fleet], outage_rate, 100.0)
    return _finish("tablecloth_spreading", "home", seed, places, things, rec, policy, fleet, "dock", [task], goals,
                   params=params, intrusions=intr, outages=outs)


# ---------------------------------------------------------------------------
# Persistent deployment (dynamic orders; no fixed task list)
# ---------------------------------------------------------------------------


def store_deployment(seed: int, *, fleet=DEFAULT_FLEET, hours: float = 24.0, stock_per_item: int = 3, intrusions_per_hour: float = 2.0,
                     outages: int = 3, training_scale: int = 1000, flaws: dict[str, float] | None = None,
                     params: SimParams | None = None) -> Scenario:
    """A full store for a long deployment run; tasks are generated over time by the caller."""
    rng, places, records, policy, things, counter, by_id = _store_world(
        seed, fleet=fleet, training_scale=training_scale, flaws=flaws if flaws is not None else {"release_on_retract": 0.2}, params=params
    )
    for sku, item in records.catalog.items():
        if item.home:
            for _ in range(stock_per_item):
                _thing(things, counter, records, sku, "storage")
    horizon = hours * 3600.0
    intr = []
    n_intr = int(hours * intrusions_per_hour)
    for _ in range(n_intr):
        loc = rng.choice(["shelf_A", "shelf_B", "hook_wall", "table_1", "table_2"])
        start = rng.uniform(0.0, horizon)
        intr.append(Intrusion(loc, start, start + rng.uniform(20.0, 90.0)))
    outs = []
    for _ in range(outages):
        # Action services crash while working (on the robot's N-th execution), not while idle.
        rid = rng.choice([r for r, _ in fleet])
        outs.append(Outage(rid, 0.0, 0.0, after_calls=rng.randint(20, 250), duration=rng.uniform(60.0, 300.0)))
    return _finish("store_deployment", "store", seed, places, things, records, policy, fleet, "dock", [], [],
                   params=params, intrusions=intr, outages=outs, meta={"hours": hours})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BENCH_TASKS: dict[str, Callable[..., Scenario]] = {
    "hosting_guests": home_hosting_guests,
    "tablecloth_spreading": home_tablecloth,
    "office_supply_search": office_supply_search,
    "item_retrieval": store_item_retrieval,
    "restocking": store_restocking,
    "cleaning": store_cleaning,
}

SCENARIOS: dict[str, Callable[..., Scenario]] = dict(BENCH_TASKS, store_deployment=store_deployment)


def build(name: str, seed: int = 0, **kwargs: Any) -> Scenario:
    try:
        builder = SCENARIOS[name]
    except KeyError:
        raise KeyError(f"unknown scenario '{name}'; choose from {sorted(SCENARIOS)}") from None
    return builder(seed, **kwargs)
