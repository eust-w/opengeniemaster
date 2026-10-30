"""Business records: the "materials & business records" of a domain.

These are what an agent may legitimately know before acting: the merchant
catalog (names, attributes, reference images), the planogram, location
metadata and open orders. They are *not* ground truth about the physical
scene; stale or incomplete records are expected.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .._serde import from_dict, to_dict


def slot_center(slots: list[str], slot: str) -> tuple[float, float]:
    """Normalized image position of a slot in its location's canonical view.

    Slots are laid out row-major on a near-square grid; the simulator and the
    guidance builder share this convention.
    """
    n = max(len(slots), 1)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    idx = slots.index(slot)
    r, c = divmod(idx, cols)
    return (round((c + 0.5) / cols, 3), round((r + 0.5) / rows, 3))


@dataclass
class CatalogItem:
    sku: str
    name: str
    category: str
    attributes: dict[str, str] = field(default_factory=dict)
    image: str | None = None  # reference image URI
    group: str | None = None  # look-alike family, if known
    home: str | None = None  # planogram location
    slot: str | None = None  # planogram slot
    mount: str = "shelf"  # "shelf" | "hook" | "none"
    tags: list[str] = field(default_factory=list)
    stock_location: str | None = None  # where replacement stock is kept
    likely_locations: list[str] = field(default_factory=list)  # for items without a fixed home

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class PlaceInfo:
    loc_id: str
    kind: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    slots: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class Order:
    order_id: str
    kind: str  # "retrieve" | "restock" | "serve" | "fetch" | "clean"
    items: list[str] = field(default_factory=list)
    deliver_to: str | None = None
    requester: str | None = None
    created_at: float = 0.0
    status: str = "open"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class BusinessRecords:
    catalog: dict[str, CatalogItem] = field(default_factory=dict)
    places: dict[str, PlaceInfo] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def item(self, sku: str) -> CatalogItem:
        try:
            return self.catalog[sku]
        except KeyError:
            raise KeyError(f"unknown catalog item '{sku}'") from None

    def place(self, loc_id: str) -> PlaceInfo | None:
        return self.places.get(loc_id)

    def lookup(self, query: str, limit: int = 10) -> list[CatalogItem]:
        """Fuzzy lookup by sku, name, category or attribute words."""
        q = query.strip().lower()
        if not q:
            return []
        if q in self.catalog:
            return [self.catalog[q]]
        words = [w for w in re.split(r"[^a-z0-9一-鿿]+", q) if w]
        scored = []
        for item in self.catalog.values():
            hay = " ".join(
                [item.sku, item.name, item.category, *item.attributes.values(), *item.tags]
            ).lower()
            tokens = set(re.split(r"[^a-z0-9一-鿿]+", hay))
            # whole-word matches rank above substring matches ("cola" vs "chocolate")
            score = sum(2 if w in tokens else 1 for w in words if w in hay)
            if score:
                scored.append((-score, item.sku, item))
        scored.sort()
        return [it for _, _, it in scored[:limit]]

    def planogram(self, location: str) -> list[tuple[str, str]]:
        return sorted(
            (it.slot or "", it.sku)
            for it in self.catalog.values()
            if it.home == location
        )

    def lookalikes(self, sku: str) -> list[str]:
        group = self.catalog[sku].group if sku in self.catalog else None
        if not group:
            return []
        return sorted(s for s, it in self.catalog.items() if it.group == group and s != sku)

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders if o.status == "open"]

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BusinessRecords":
        return from_dict(cls, data)
