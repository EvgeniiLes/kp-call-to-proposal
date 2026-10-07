"""Каталог товаров для подбора: загрузка data/catalog_*.json и нормализация атрибутов.

В бою вместо этого модуля — синхронизированная копия PIM. Здесь источник — витрина сайта
(parse_catalog.py), поэтому часть атрибутов достаётся из свободного текста.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# Виды позиций, которые умеет подбирать агент. Ключ используется и в схеме ИИ, и в правилах.
KINDS = {
    "tire_changer_truck": "Шиномонтажный станок (грузовой)",
    "balancer_truck": "Балансировочный станок (грузовой)",
    "alignment_stand": "Стенд сход-развал",
    "compressor": "Компрессор",
    "impact_wrench": "Пневмогайковёрт",
    "jack": "Домкрат",
    "receiver": "Ресивер",
    "lift": "Подъёмник / подкатные стойки",
}
TRUCK_C = {1: "tire_changer_truck", 2: "balancer_truck", 3: "lift"}
NAME_KINDS = [("домкрат", "jack"), ("гайковёрт", "impact_wrench"), ("компрессор", "compressor"),
              ("ресивер", "receiver"), ("баланс", "balancer_truck")]


@dataclass
class Product:
    id: str
    name: str
    kind: str
    brand: str
    price: int | None          # None — «цена по запросу»
    voltages: set[int]         # пустое множество — питание неизвестно
    rim: tuple[float, float] | None  # диапазон зажима диска, дюймы
    segment: str = ""          # для стендов: «легковые», «грузовые» или «легковые, грузовые»
    specs: dict = field(default_factory=dict)
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "kind_name": KINDS[self.kind],
            "brand": self.brand, "price": self.price, "voltages": sorted(self.voltages),
            "rim": list(self.rim) if self.rim else None, "segment": self.segment,
            "specs": self.specs, "url": self.url,
        }


def _voltages(*texts: str) -> set[int]:
    out = set()
    for t in texts:
        for v in re.findall(r"(?<!\d)(220|230|380|400|415)(?!\d)", t or ""):
            out.add(220 if v in ("220", "230") else 380)
    return out


def _rim(cl: str) -> tuple[float, float] | None:
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)", cl or "")
    return (float(m[1].replace(",", ".")), float(m[2].replace(",", "."))) if m else None


@lru_cache
def load() -> dict[str, Product]:
    raw = json.loads((DATA / "catalog_raw.json").read_text(encoding="utf-8"))
    cat = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
    raw_by_id: dict = {}
    for site, arrays in raw.items():
        for arr in arrays.values():
            for x in arr:
                raw_by_id.setdefault((site, str(x.get("id"))), x)

    products: dict[str, Product] = {}
    for item in cat:
        if item["kind"] != "product" or item["site"] not in ("truck", "technovector"):
            continue  # малярный участок без цен и правил — вне первой версии
        site, rid = item["id"].split(":", 1)
        r = raw_by_id.get((site, rid), {})
        if site == "technovector":
            kind = "alignment_stand"
        elif r.get("c") in TRUCK_C:
            kind = TRUCK_C[r["c"]]
        else:
            low = item["name"].lower()
            kind = next((k for kw, k in NAME_KINDS if kw in low), None)
        if not kind:
            continue
        specs = {k: v for k, v in item["specs"].items() if v}
        seg = specs.get("Сегмент", "")
        products[item["id"]] = Product(
            id=item["id"], name=item["name"], kind=kind, brand=item["brand"] or "",
            price=item["price_rub"],
            voltages=_voltages(item.get("voltage", ""), specs.get("Питание, В", ""), item["name"]),
            rim=_rim(specs.get("Диаметр обода, дюйм", "")),
            segment=", ".join(s for s, kw in (("легковые", "легк"), ("грузовые", "груз")) if kw in seg.lower()),
            specs=specs, url="",
        )
    return products


def search(q: str = "", kind: str = "", limit: int = 30) -> list[Product]:
    words = [w for w in q.lower().split() if w]
    res = [p for p in load().values()
           if (not kind or p.kind == kind) and all(w in p.name.lower() for w in words)]
    return sorted(res, key=lambda p: (p.price is None, p.price or 0))[:limit]
