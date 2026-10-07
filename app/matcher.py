"""Шаги «правила подбора → кандидаты → выбор».

Правила — код и справочник, а не промпт: их подтверждает инженер по подбору, и они проверяемы.
ИИ (если включён) только выбирает из кандидатов, прошедших правила, и объясняет выбор.
"""
from __future__ import annotations

from . import llm
from .catalog import KINDS, Product, load

MAX_CANDIDATES = 12

# Подсказки инженера с сайта компании — показываются менеджеру при срабатывании условия.
ENGINEER_NOTES = {
    "big_rim": "Колёса от 24″: проверьте наружный диаметр и ширину колеса по маркировке шины, "
               "а не только посадочный диаметр — станок «до 26″» может не взять, например, 18.00R25.",
    "impact_wrench": "Дюймовый гайковёрт расходует 650–1500 л/мин — сверьте с производительностью компрессора.",
    "truck_compressor": "Для рулевых колёс тягача нужно 8,5–9 бар: компрессора «на 10 бар» может не хватить, "
                        "обычно ставят 12–16 бар.",
}


def _fits(p: Product, need: dict, relax: set[str]) -> list[str]:
    """Пустой список — товар подходит. Иначе — нарушенные требования."""
    bad = []
    v = need["voltage"]["value"]
    if "voltage" not in relax and v == 220 and p.voltages and 220 not in p.voltages:
        bad.append(f"питание {'/'.join(map(str, sorted(p.voltages)))} В, у клиента 220 В")
    rim = need["max_rim_inch"]["value"]
    if "rim" not in relax and rim and p.kind in ("tire_changer_truck", "balancer_truck"):
        if p.rim and p.rim[1] < rim:
            bad.append(f"зажим до {p.rim[1]:g}″, нужно R{rim:g}")
    veh = need["vehicle"]["value"]
    if "segment" not in relax and p.kind == "alignment_stand" and veh in ("грузовые", "легковые") and p.segment:
        if veh not in p.segment:
            bad.append(f"стенд для: {p.segment}, нужен для: {veh}")
    return bad


def candidates(kind: str, need: dict) -> tuple[list[Product], list[str], list[str]]:
    """→ (кандидаты, нарушения если пришлось ослабить правила, заметки инженера)."""
    pool = [p for p in load().values() if p.kind == kind]
    notes = []
    rim = need["max_rim_inch"]["value"]
    if kind in ("tire_changer_truck", "balancer_truck") and rim and rim >= 24:
        notes.append(ENGINEER_NOTES["big_rim"])
    if kind == "impact_wrench":
        notes.append(ENGINEER_NOTES["impact_wrench"])
    if kind == "compressor" and need["vehicle"]["value"] == "грузовые":
        notes.append(ENGINEER_NOTES["truck_compressor"])

    strict = [p for p in pool if not _fits(p, need, set())]
    key = lambda p: (p.price is None, p.price or 0)  # noqa: E731
    if strict:
        return sorted(strict, key=key)[:MAX_CANDIDATES], [], notes
    # строгих совпадений нет — ослабляем по одному требованию и честно говорим, что нарушено
    for relax in ({"voltage"}, {"rim"}, {"segment"}, {"voltage", "rim", "segment"}):
        loose = [p for p in pool if not _fits(p, need, relax)]
        if loose:
            viol = sorted({b for p in loose for b in _fits(p, need, set())})
            return sorted(loose, key=key)[:MAX_CANDIDATES], viol, notes
    return [], ["в каталоге нет позиций этого вида"], notes


def _reason_rules(p: Product, need: dict) -> str:
    bits = []
    if p.rim and need["max_rim_inch"]["value"]:
        bits.append(f"зажим {p.rim[0]:g}–{p.rim[1]:g}″ покрывает R{need['max_rim_inch']['value']:g}")
    if p.voltages and need["voltage"]["value"]:
        bits.append(f"питание {'/'.join(map(str, sorted(p.voltages)))} В")
    if p.kind == "alignment_stand" and p.segment:
        bits.append(f"для {p.segment}")
    bits.append("самый доступный из подходящих" if p.price else "цена по запросу")
    return "; ".join(bits)


def choose_rules(slots: list[dict], need: dict) -> None:
    for s in slots:
        c = s["candidates"]
        if not c:
            continue
        priced = [p for p in c if p.price] or c
        s["selected"] = priced[0].id
        # альтернатива: ближайший вариант «оптимум» — другой бренд, дороже основного
        others = [p for p in priced[1:] if p.brand != priced[0].brand] or priced[1:]
        s["alternatives"] = [p.id for p in others[:2]]
        s["reason"] = _reason_rules(priced[0], need)


CHOICE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["choices"],
    "properties": {"choices": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["slot", "selected_id", "alternative_ids", "reason"],
        "properties": {"slot": {"type": "integer"}, "selected_id": {"type": "string"},
                       "alternative_ids": {"type": "array", "items": {"type": "string"}},
                       "reason": {"type": "string"}}}}},
}
CHOICE_SYSTEM = """Ты — инженер по подбору оборудования. Для каждой позиции (slot) выбери основной вариант \
и до двух альтернатив СТРОГО из списка кандидатов этого слота (по id). Учитывай общий бюджет клиента на все \
позиции, требования из потребности и баланс цена/качество. Альтернатива — осмысленный выбор для менеджера \
(например, дешевле или надёжнее/известнее бренд). reason — одно-два предложения на русском: почему этот \
вариант подходит клиенту, со ссылкой на его слова. Цены не пересчитывай и не выдумывай."""


def choose_llm(slots: list[dict], need: dict) -> list[str]:
    """ИИ выбирает из кандидатов. Неизвестные id отбрасываются кодом; для таких слотов — правила."""
    lines = [f"Потребность: {need.get('summary', '')}",
             f"Бюджет: {need['budget_rub']['value'] or 'не назван'} ₽", ""]
    for i, s in enumerate(slots):
        if not s["candidates"]:
            continue
        lines.append(f"slot {i}: {KINDS[s['kind']]} × {s['qty']} (клиент: «{s.get('quote') or '-'}»)")
        for p in s["candidates"]:
            lines.append(f"  id={p.id} | {p.name} | {p.brand} | "
                         f"{(str(p.price) + ' ₽') if p.price else 'цена по запросу'} | "
                         f"питание {'/'.join(map(str, sorted(p.voltages))) or '?'} | "
                         f"зажим {('%g–%g' % p.rim) if p.rim else '?'}")
    res = llm.call_json(CHOICE_SYSTEM, "\n".join(lines), CHOICE_SCHEMA, effort="medium")
    warnings, done = [], set()
    for ch in res.get("choices", []):
        i = ch.get("slot")
        if not isinstance(i, int) or not 0 <= i < len(slots) or i in done:
            continue
        ids = {p.id for p in slots[i]["candidates"]}
        if ch["selected_id"] not in ids:
            warnings.append(f"ИИ предложил несуществующую позицию «{ch['selected_id']}» — применён выбор по правилам.")
            continue
        slots[i]["selected"] = ch["selected_id"]
        slots[i]["alternatives"] = [a for a in ch["alternative_ids"] if a in ids and a != ch["selected_id"]][:2]
        slots[i]["reason"] = ch["reason"]
        done.add(i)
    rest = [s for j, s in enumerate(slots) if j not in done]
    choose_rules(rest, need)
    return warnings
