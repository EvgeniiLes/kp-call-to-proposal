"""Конвейер: расшифровка → потребность → кандидаты → выбор → проверки → черновик → КП."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from . import delivery, extract, llm, matcher
from .catalog import KINDS, load

DATA = Path(__file__).resolve().parent.parent / "data"
JOURNAL = DATA / "journal.jsonl"
DISCOUNT_LIMIT = 10      # допущение: менеджер даёт скидку до 10% без согласования
KP_VALID_DAYS = 10       # допущение: срок действия КП


def _log(event: str, payload: dict) -> None:
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **payload},
                           ensure_ascii=False) + "\n")


def analyze(text: str, use_llm: bool | None = None) -> dict:
    use_llm = llm.available() if use_llm is None else use_llm
    lines, need, notes = extract.extract(text, use_llm)
    mode = "llm" if use_llm and not any("по правилам" in n for n in notes) else "rules"
    if need["city"]["value"]:  # ИИ может вернуть «Новосибирская обл.» — приводим к справочнику
        need["city"]["value"] = delivery.find_city(need["city"]["value"]) or need["city"]["value"]

    slots = []
    for it in need["items"]:
        cands, viol, eng = matcher.candidates(it["kind"], need)
        slots.append({"kind": it["kind"], "qty": it["qty"], "quote": it.get("quote"), "notes": eng,
                      "candidates": cands, "violations": viol, "selected": None, "alternatives": [], "reason": ""})
    if mode == "llm" and any(s["candidates"] for s in slots):
        try:
            notes += matcher.choose_llm(slots, need)
        except llm.LLMError as e:
            notes.append(f"ИИ не смог выбрать позиции ({e}) — применён выбор по правилам.")
            matcher.choose_rules(slots, need)
    else:
        matcher.choose_rules(slots, need)

    warnings = build_warnings(need, slots, notes)
    draft = {
        "draft_id": uuid.uuid4().hex[:10],
        "mode": mode,
        "model": llm.MODEL if mode == "llm" else None,
        "lines": lines,
        "need": need,
        "slots": [{
            "kind": s["kind"], "kind_name": KINDS[s["kind"]], "qty": s["qty"], "quote": s["quote"],
            "reason": s["reason"], "selected": s["selected"], "alternatives": s["alternatives"],
            "violations": s["violations"], "notes": s["notes"],
            "options": [p.to_dict() for p in s["candidates"]],
        } for s in slots],
        "warnings": warnings,
        "delivery": delivery.calc([{"id": s["selected"], "qty": s["qty"]} for s in slots if s["selected"]],
                                  city=need["city"]["value"]),
    }
    _log("draft", {"draft_id": draft["draft_id"], "mode": mode,
                   "proposed": [{"kind": s["kind"], "id": s["selected"], "qty": s["qty"]} for s in slots]})
    return draft


def build_warnings(need: dict, slots: list[dict], notes: list[str]) -> list[dict]:
    """level: block — нужно подтверждение менеджера; warn — важно; info — к сведению."""
    w = []
    add = lambda level, text, quote=None: w.append(  # noqa: E731
        {"id": f"w{len(w)}", "level": level, "text": text, "quote": quote})
    if not need["summary_found"]:
        add("block", "Менеджер не подвёл итог в конце звонка — черновик собран по всему разговору. Проверьте внимательнее.")
    elif not need["client_confirmed"]:
        add("warn", "Клиент явно не подтвердил резюме менеджера.", need.get("summary_quote"))
    for c in need["conflicts"]:
        add("block", f"Расхождение «{c['field']}»: в разговоре — {c['dialog_value']}, в резюме — {c['summary_value']}. "
                     f"Уточните у клиента, какое верно, и проверьте позиции черновика.", c["dialog_quote"])
    for name in need.get("mentioned_not_in_summary", []):
        add("info", f"В разговоре упоминалось «{name}», но в итог не вошло — уточните, нужно ли.")
    for s in slots:
        name = KINDS[s["kind"]]
        if not s["candidates"]:
            add("block", f"{name}: подходящих позиций нет ({'; '.join(s['violations'])}).")
        elif s["violations"]:
            add("block", f"{name}: под все требования ничего нет, показаны ближайшие варианты. "
                         f"Не совпадает: {'; '.join(s['violations'])}.", s["quote"])
        sel = next((p for p in s["candidates"] if p.id == s["selected"]), None)
        if sel and sel.price is None:
            add("warn", f"{name}: у «{sel.name}» цена по запросу — уточните у поставщика и впишите в строку.")
        for n in s["notes"]:
            add("info", f"{name}: {n}")
    budget = need["budget_rub"]["value"]
    if budget:
        by_id = {p.id: p for s in slots for p in s["candidates"]}
        total = sum((by_id[s["selected"]].price or 0) * s["qty"] for s in slots if s["selected"])
        if total > budget:
            add("warn", f"Черновик дороже бюджета клиента: {total:,} ₽ при бюджете {budget:,} ₽ "
                        f"(+{total - budget:,} ₽). Посмотрите альтернативы.".replace(",", " "),
                need["budget_rub"]["quote"])
    if not slots:
        add("block", "Не удалось определить, какое оборудование нужно клиенту. Добавьте позиции вручную или уточните у клиента.")
    for n in notes:
        add("warn", n)
    return w


def build_kp(req: dict) -> dict:
    """Проверка и фиксация КП. Цены — только из каталога; вручную — лишь там, где в каталоге «по запросу»."""
    products = load()
    errors, rows = [], []
    disc = float(req.get("discount_pct") or 0)
    if not 0 <= disc <= DISCOUNT_LIMIT:
        errors.append(f"Скидка {disc:g}% вне лимита 0–{DISCOUNT_LIMIT}% — нужна согласованная скидка руководителя.")
    seen = set()
    for it in req.get("items", []):
        p = products.get(it.get("id"))
        if not p:
            errors.append(f"Позиция {it.get('id')} не найдена в каталоге.")
            continue
        if p.id in seen:
            errors.append(f"Позиция «{p.name}» добавлена дважды — объедините количество.")
            continue
        seen.add(p.id)
        qty = int(it.get("qty") or 0)
        if qty < 1:
            errors.append(f"«{p.name}»: количество должно быть не меньше 1.")
            continue
        price, source = p.price, "каталог"
        if price is None:
            manual = it.get("price_manual")
            if not manual or float(manual) <= 0:
                errors.append(f"«{p.name}»: цена по запросу — укажите цену, подтверждённую поставщиком.")
                continue
            price, source = int(float(manual)), "вручную"
        rows.append({"id": p.id, "name": p.name, "brand": p.brand, "qty": qty, "price": price,
                     "price_source": source, "sum": price * qty, "specs": p.specs, "kind_name": KINDS[p.kind]})
    if not rows and not errors:
        errors.append("В КП нет ни одной позиции.")
    # доставка пересчитывается здесь же: сумме из браузера не доверяем
    dlv_req, dlv = req.get("delivery") or {}, None
    if dlv_req.get("include"):
        dlv = delivery.calc([{"id": r["id"], "qty": r["qty"]} for r in rows],
                            city=dlv_req.get("city") or None, km=dlv_req.get("km"))
        if not dlv["ok"]:
            errors.append(f"Доставка: {dlv['error']}")
    if errors:
        return {"ok": False, "errors": errors}

    subtotal = sum(r["sum"] for r in rows)
    discount = round(subtotal * disc / 100)  # скидка — только на оборудование, не на доставку
    dlv_cost = dlv["cost"] if dlv else 0
    kp = {
        "ok": True, "kp_id": "КП-" + datetime.now().strftime("%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
        "draft_id": req.get("draft_id"), "deal_id": req.get("deal_id") or "", "client": req.get("client") or "",
        "manager": req.get("manager") or "", "date": date.today().isoformat(),
        "valid_until": (date.today() + timedelta(days=KP_VALID_DAYS)).isoformat(),
        "rows": rows, "subtotal": subtotal, "discount_pct": disc, "discount": discount,
        "delivery": dlv, "total": subtotal - discount + dlv_cost,
        "budget": req.get("budget"), "summary": req.get("summary") or "",
    }
    (DATA / "kp").mkdir(exist_ok=True)
    (DATA / "kp" / f"{kp['kp_id']}.json").write_text(json.dumps(kp, ensure_ascii=False, indent=1), encoding="utf-8")
    _log("kp", {"draft_id": kp["draft_id"], "kp_id": kp["kp_id"], "edited": _was_edited(kp["draft_id"], rows),
                "edited_client_flag": bool(req.get("edited")),
                "final": [{"id": r["id"], "qty": r["qty"]} for r in rows], "total": kp["total"]})
    return kp


def _was_edited(draft_id: str | None, rows: list[dict]) -> bool:
    """Метрика «КП без правок» считается на сервере: итог сравнивается с тем, что предложил черновик.
    Флагу из браузера не доверяем. Если черновик не найден в журнале — считаем, что правки были."""
    if not draft_id or not JOURNAL.exists():
        return True
    proposed = None
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        e = json.loads(line) if line.strip() else {}
        if e.get("event") == "draft" and e.get("draft_id") == draft_id:
            proposed = e["proposed"]
    if proposed is None:
        return True
    want = sorted((x["id"], x["qty"]) for x in proposed if x.get("id"))
    got = sorted((r["id"], r["qty"]) for r in rows)
    return want != got or any(r["price_source"] != "каталог" for r in rows)


def load_kp(kp_id: str) -> dict | None:
    f = DATA / "kp" / f"{Path(kp_id).name}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def b24_send(kp: dict) -> dict:
    """Заглушка Битрикс24: показывает, что ушло бы в REST. Методы — допущение, уточнить до разработки."""
    calls = [
        {"method": "crm.deal.productrows.set", "params": {
            "id": kp["deal_id"] or "<ID сделки>",
            "rows": [{"PRODUCT_NAME": r["name"], "PRICE": r["price"], "QUANTITY": r["qty"],
                      "DISCOUNT_TYPE_ID": 2, "DISCOUNT_RATE": kp["discount_pct"]} for r in kp["rows"]]
                    + ([{"PRODUCT_NAME": f"Доставка до {kp['delivery']['city'] or str(kp['delivery']['km']) + ' км'}",
                         "PRICE": kp["delivery"]["cost"], "QUANTITY": 1}] if kp.get("delivery") else [])}},
        {"method": "crm.timeline.comment.add", "params": {
            "fields": {"ENTITY_ID": kp["deal_id"] or "<ID сделки>", "ENTITY_TYPE": "deal",
                       "COMMENT": f"{kp['kp_id']} на {kp['total']:,} ₽. Итоги звонка: {kp['summary']}".replace(",", " "),
                       "FILES": [[f"{kp['kp_id']}.pdf", "<base64 PDF>"]]}}},
    ]
    with (DATA / "b24_outbox.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "kp_id": kp["kp_id"], "calls": calls},
                           ensure_ascii=False) + "\n")
    _log("b24_sent", {"kp_id": kp["kp_id"]})
    return {"ok": True, "calls": calls}


def stats() -> dict:
    """Метрики из журнала: сколько КП собрано без правок черновика агента."""
    if not JOURNAL.exists():
        return {"drafts": 0, "kp": 0, "kp_without_edits": 0}
    ev = [json.loads(l) for l in JOURNAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    kps = [e for e in ev if e["event"] == "kp"]
    return {"drafts": sum(e["event"] == "draft" for e in ev), "kp": len(kps),
            "kp_without_edits": sum(not e["edited"] for e in kps)}
