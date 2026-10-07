"""Шаг «расшифровка звонка → Потребность».

Два движка с одинаковым результатом:
  * llm   — Claude по JSON-схеме (основной);
  * rules — регулярные выражения (без ключа API и как запасной при сбое ИИ).
Главный принцип: итоговое резюме менеджера важнее остального разговора; расхождение между
ними не исправляется молча, а возвращается как conflict.
"""
from __future__ import annotations

import re

from . import delivery, llm
from .catalog import KINDS

# ---------- общий формат

def empty_need() -> dict:
    f = lambda: {"value": None, "quote": None}  # noqa: E731
    return {
        "summary": "", "summary_found": False, "summary_quote": None, "client_confirmed": False,
        "vehicle": f(), "max_rim_inch": f(), "voltage": f(), "budget_rub": f(), "installation": f(), "city": f(),
        "items": [], "conflicts": [], "open_questions": [], "next_steps": [], "deadline": None,
        "mentioned_not_in_summary": [],
    }


def parse_lines(text: str) -> list[dict]:
    """«Менеджер: …» / «Клиент: …» → [{i, speaker, text}]. Строки без метки приклеиваются к предыдущей."""
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        m = re.match(r"^\s*(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?(Менеджер|Клиент|М|К)\s*[:：-]\s*(.*)$", s, re.I)
        if m:
            spk = "manager" if m[1].lower().startswith("м") else "client"
            out.append({"i": len(out), "speaker": spk, "text": m[2].strip()})
        elif out:
            out[-1]["text"] += " " + s
        else:
            out.append({"i": 0, "speaker": "unknown", "text": s})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е")).strip(" .,!?«»\"")


def verify_quotes(need: dict, lines: list[dict]) -> list[str]:
    """Цитата должна дословно встречаться в расшифровке, иначе ИИ её «придумал» — обнуляем."""
    corpus = [_norm(l["text"]) for l in lines]
    bad = []

    def ok(q):
        nq = _norm(q)
        return bool(nq) and any(nq in c for c in corpus)

    for key in ("vehicle", "max_rim_inch", "voltage", "budget_rub", "installation", "city"):
        q = need[key].get("quote")
        if q and not ok(q):
            bad.append(q)
            need[key]["quote"] = None
    for it in need["items"]:
        if it.get("quote") and not ok(it["quote"]):
            bad.append(it["quote"])
            it["quote"] = None
    if need.get("summary_quote") and not ok(need["summary_quote"]):
        bad.append(need["summary_quote"])
        need["summary_quote"] = None
    return bad


# ---------- движок на правилах

WORD_NUMS = [
    (r"двести\s+двадцать", "220"), (r"двести\s+тридцать", "230"), (r"триста\s+восемьдесят", "380"),
    (r"двадцать\s+два\s+(?:с\s+половиной|и\s+пять|пять)", "22.5"), (r"девятнадцать\s+с\s+половиной", "19.5"),
    (r"семнадцать\s+с\s+половиной", "17.5"), (r"двадцать\s+шесть", "26"), (r"двадцать\s+четыре", "24"),
    (r"двадцать\s+пять", "25"), (r"двадцать\s+два", "22"),
    (r"полтора\s+(?:миллиона|млн|ляма)", "1.5 млн"), (r"(?<!\d\s)миллион(?!а|ов)", "1 млн"),
    (r"два\s+миллиона", "2 млн"), (r"три\s+миллиона", "3 млн"),
]
SUMMARY_RE = re.compile(r"\b(итак|фиксирую|зафиксирую|подытож|подвед[уе]м?\s+итог|резюмир|правильно\s+понимаю|"
                        r"давайте\s+(?:я\s+)?повторю|сверим|итого)", re.I)
CONFIRM_RE = re.compile(r"\b(да|верно|все\s+так|всё\s+так|согласен|подтверждаю|правильно|точно)\b", re.I)
KIND_RE = [
    # «шиномонтаж» сам по себе — чаще бизнес клиента, а не оборудование; станок — только со словом «станок»
    ("tire_changer_truck", r"шиномонтажн\w*\s+станок|станок\s+(?:для\s+)?шиномонтаж\w*|монтажн\w*\s+станок|"
                           r"станок\s+для\s+(?:шин|колес)|(?<!\w)станок(?!\w)"),
    ("balancer_truck", r"баланс"),
    ("alignment_stand", r"сход|развал|стенд\s+развал"),
    ("compressor", r"компрессор"),
    ("impact_wrench", r"гайков[её]рт"),
    ("jack", r"домкрат"),
    ("receiver", r"ресивер"),
    ("lift", r"подъ[её]мник|подкатн\w*\s+стойк"),
]
NEG_RE = r"(?:не\s+нужен|не\s+нужна|не\s+нужно|не\s+надо|уже\s+есть|есть\s+уже|\bесть\b|\bнет\b|не\s+берем)"


def _prep(t: str) -> str:
    t = t.lower().replace("ё", "е")
    for pat, rep in WORD_NUMS:
        t = re.sub(pat, rep, t)
    return t


def _voltage(t: str) -> int | None:
    t = _prep(t)
    vals = re.findall(r"(?<!\d)(?<!\d[.,])(220|230|380|400)(?!\d|[.,]\d)", t)  # «питание 380, бюджет» — запятая не мешает
    three = r"(?:тр[её]хфаз\w*|три\s+фазы|3\s*фаз\w*)"
    if re.search(rf"(?:нет|без)\s+{three}|{three}\s+(?:нет|не\s+будет|не\s+подвед)", t):
        vals.append("220")
    elif re.search(three, t):
        vals.append("380")
    if re.search(r"однофаз|одна\s+фаза|обычн\w+\s+розетк", t):
        vals.append("220")
    if not vals:
        return None
    return 220 if vals[-1] in ("220", "230") else 380


def _rim(t: str) -> float | None:
    t = _prep(t)
    if not re.search(r"колес|диск|шин|r\s?\d|дюйм|″|\"", t):
        return None
    nums = [float(x.replace(",", ".")) for x in re.findall(r"(?:r\s?)?(?<![\d.,])(\d{2}(?:[.,]5)?)(?![\d])", t)]
    nums = [n for n in nums if 13 <= n <= 60]
    return max(nums) if nums else None


def _budget(t: str) -> int | None:
    t = _prep(t)
    if not re.search(r"бюджет|денег|рассчитыва|уложит|потянем|не\s+больше|максимум|до\s+\d", t):
        return None
    m = re.findall(r"(\d+(?:[.,]\d+)?)\s*(млн|миллион|тыс|тысяч|к\b|руб|₽)", t)
    if not m:
        return None
    v, unit = m[-1]
    v = float(v.replace(",", "."))
    return int(v * 1_000_000) if unit.startswith(("млн", "миллион")) else int(v * 1000 if unit.startswith(("тыс", "к")) else v)


def _vehicle(t: str) -> str | None:
    t = t.lower()
    if re.search(r"\bгруз|тягач|\bфур|самосвал|автобус|полуприцеп|камаз|спецтехн", t):
        return "грузовые"
    if re.search(r"легков", t):
        return "легковые"
    return None


def _installation(t: str) -> str | None:
    t = t.lower()
    if re.search(r"\bям[аеуы]\b|на\s+яму", t):
        return "яма"
    if re.search(r"на\s+подъ[её]мник", t):
        return "подъёмник"
    return None


def _kinds(t: str) -> tuple[set[str], set[str]]:
    """Виды позиций в реплике: (нужны, явно не нужны)."""
    t = t.lower().replace("ё", "е")
    want, neg = set(), set()
    for kind, pat in KIND_RE:
        for m in re.finditer(pat, t):
            head = t[max(0, m.start() - 20): m.start()]
            if kind == "tire_changer_truck" and m[0] == "станок" and re.search(r"баланс\w*\s*$", head):
                continue  # «балансировочный станок» — это балансир
            tail = re.split(r"[.;!?]|,\s*(?:а|но)\s", t[m.end(): m.end() + 60])[0]  # до конца фразы
            (neg if re.search(NEG_RE, tail) or re.search(r"\bбез\s*$", head) else want).add(kind)
    return want, neg - want


def extract_rules(lines: list[dict]) -> dict:
    need = empty_need()
    # 1. резюме: последняя реплика менеджера с маркером итога + его следующие реплики подряд
    s_idx = [l["i"] for l in lines if l["speaker"] == "manager" and SUMMARY_RE.search(l["text"])]
    summary_lines: list[dict] = []
    if s_idx:
        j = s_idx[-1]
        while j < len(lines) and lines[j]["speaker"] == "manager":
            summary_lines.append(lines[j])
            j += 1
        need["summary_found"] = True
        need["summary_quote"] = summary_lines[0]["text"]
        if j < len(lines) and lines[j]["speaker"] == "client" and CONFIRM_RE.search(lines[j]["text"]):
            need["client_confirmed"] = True
    summary_ids = {l["i"] for l in summary_lines}
    body = [l for l in lines if l["i"] not in summary_ids]

    # 2. поля: из резюме, иначе — последнее упоминание в разговоре (мнение могло поменяться)
    extractors = {"voltage": _voltage, "max_rim_inch": _rim, "budget_rub": _budget,
                  "vehicle": _vehicle, "installation": _installation, "city": delivery.find_city}
    labels = {"voltage": "Питание", "max_rim_inch": "Диаметр колёс", "budget_rub": "Бюджет",
              "vehicle": "Тип транспорта", "installation": "Установка", "city": "Город доставки"}
    for key, fn in extractors.items():
        in_sum = next(((fn(l["text"]), l["text"]) for l in summary_lines if fn(l["text"]) is not None), None)
        in_body = next(((fn(l["text"]), l["text"]) for l in reversed(body) if fn(l["text"]) is not None), None)
        if key == "max_rim_inch":  # для диаметра берём максимум по разговору, а не последнее упоминание
            vals = [(fn(l["text"]), l["text"]) for l in body if fn(l["text"]) is not None]
            in_body = max(vals, key=lambda x: x[0]) if vals else None
        src = in_sum or in_body
        if src:
            need[key] = {"value": src[0], "quote": src[1]}
        if in_sum and in_body and in_sum[0] != in_body[0] and key != "max_rim_inch":
            need["conflicts"].append({"field": labels[key], "dialog_value": str(in_body[0]),
                                      "summary_value": str(in_sum[0]),
                                      "dialog_quote": in_body[1], "summary_quote": in_sum[1]})

    # 3. позиции: при наличии резюме — из него; остальное в разговоре — только как напоминание
    def collect(src):
        want, neg = {}, set()
        for l in src:
            w, n = _kinds(l["text"])
            for k in w:
                want.setdefault(k, l["text"])
            neg |= n
        return {k: q for k, q in want.items() if k not in neg}, neg

    body_items, body_neg = collect(body)
    if summary_lines:
        sum_items, _ = collect(summary_lines)
        items = sum_items
        need["mentioned_not_in_summary"] = [KINDS[k] for k in body_items if k not in sum_items and k not in body_neg]
    else:
        items = body_items
    need["items"] = [{"kind": k, "qty": 1, "quote": q, "notes": ""} for k, q in items.items()]

    # 4. дальнейшие шаги и срок
    for l in lines:
        if l["speaker"] == "manager" and re.search(r"пришл|отправл|направл|перезвон|созвон|подготовл|сделаю|выставл", l["text"].lower()):
            need["next_steps"].append(l["text"])
            m = re.search(r"(сегодня|завтра|послезавтра|в\s+(?:понедельник|вторник|среду|четверг|пятницу)|до\s+\d{1,2}(?::\d{2})?(?:\s*час\w*)?)"
                          r"(?:\s+до\s+\d{1,2}(?::\d{2})?)?", l["text"].lower())
            if m:
                need["deadline"] = m[0]
    need["open_questions"] = open_questions(need)
    kinds_txt = ", ".join(KINDS[i["kind"]].lower() for i in need["items"]) or "позиции не определены"
    parts = [f"Клиенту нужно: {kinds_txt}."]
    for key, label in labels.items():
        v = need[key]["value"]
        if v is not None:
            parts.append(f"{label}: {fmt_value(key, v)}.")
    need["summary"] = " ".join(parts)
    return need


def fmt_value(key: str, v) -> str:
    if key == "voltage":
        return f"{v} В"
    if key == "max_rim_inch":
        return f"до R{v:g}″"
    if key == "budget_rub":
        return f"до {v:,} ₽".replace(",", " ")
    return str(v)


def open_questions(need: dict) -> list[str]:
    q = []
    kinds = {i["kind"] for i in need["items"]}
    if not kinds:
        q.append("Какое оборудование нужно в первую очередь: шиномонтаж, балансировка, сход-развал, компрессор?")
    if kinds & {"tire_changer_truck", "balancer_truck"} and need["max_rim_inch"]["value"] is None:
        q.append("Какой самый большой диаметр колёс обслуживаете (например, R22.5)? Если спецтехника — пришлите маркировку шины.")
    if kinds & {"tire_changer_truck", "balancer_truck", "compressor", "alignment_stand"} and need["voltage"]["value"] is None:
        q.append("Какое питание в боксе: обычная сеть 220 В или три фазы 380 В?")
    if "alignment_stand" in kinds:
        if need["vehicle"]["value"] is None:
            q.append("Стенд нужен для легковых, грузовых или для тех и других?")
        if need["installation"]["value"] is None:
            q.append("Куда ставим стенд: на яму или на подъёмник?")
    if need["budget_rub"]["value"] is None and kinds:
        q.append("Есть ли ориентир по бюджету? Подберём вариант эконом и оптимум.")
    if need["city"]["value"] is None and kinds:
        q.append("Куда доставить оборудование: город и адрес? Посчитаем доставку от склада в Москве.")
    return q


# ---------- движок на ИИ

_F = lambda t: {"type": "object", "additionalProperties": False, "required": ["value", "quote"],  # noqa: E731
                "properties": {"value": {"type": [t, "null"]}, "quote": {"type": ["string", "null"]}}}
# enum с типом-массивом ["string", "null"] API не принимает — допустимые значения или null через anyOf
_E = lambda t, values: {"type": "object", "additionalProperties": False, "required": ["value", "quote"],  # noqa: E731
                        "properties": {"value": {"anyOf": [{"type": t, "enum": values}, {"type": "null"}]},
                                       "quote": {"type": ["string", "null"]}}}
NEED_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "summary_found", "summary_quote", "client_confirmed", "vehicle", "max_rim_inch",
                 "voltage", "budget_rub", "installation", "city", "items", "conflicts", "next_steps", "deadline",
                 "mentioned_not_in_summary"],
    "properties": {
        "summary": {"type": "string"},
        "summary_found": {"type": "boolean"},
        "summary_quote": {"type": ["string", "null"]},
        "client_confirmed": {"type": "boolean"},
        "vehicle": _E("string", ["легковые", "грузовые", "смешанный"]),
        "max_rim_inch": _F("number"),
        "voltage": _E("integer", [220, 380]),
        "budget_rub": _F("integer"),
        "installation": _E("string", ["яма", "подъёмник"]),
        "city": _F("string"),
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "qty", "quote", "notes"],
            "properties": {"kind": {"type": "string", "enum": list(KINDS)}, "qty": {"type": "integer"},
                           "quote": {"type": ["string", "null"]}, "notes": {"type": "string"}}}},
        "conflicts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["field", "dialog_value", "summary_value", "dialog_quote", "summary_quote"],
            "properties": {k: {"type": "string"} for k in
                           ["field", "dialog_value", "summary_value", "dialog_quote", "summary_quote"]}}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "deadline": {"type": ["string", "null"]},
        # только виды оборудования — иначе ИИ складывает сюда город, размеры и прочее
        "mentioned_not_in_summary": {"type": "array", "items": {"type": "string", "enum": list(KINDS)}},
    },
}

SYSTEM = f"""Ты помогаешь менеджеру по продажам оборудования для автосервисов (шиномонтаж, балансировка, \
сход-развал, компрессоры, пневмоинструмент) подготовить коммерческое предложение по расшифровке телефонного звонка.

Извлеки потребность клиента. Правила:
1. Если в конце звонка менеджер проговаривает итог («итак», «фиксирую», «правильно понимаю» и т.п.) — это \
главный источник. summary_found=true, summary_quote — дословная первая фраза резюме. client_confirmed=true, \
если клиент после резюме согласился.
2. Если клиент по ходу разговора менял мнение — бери итоговое решение, а не первое упоминание.
3. Если значение в резюме противоречит тому, что клиент сказал в разговоре (и клиент это не менял), не выбирай \
сам: заполни поле значением из резюме и добавь запись в conflicts с обеими цитатами (field — название \
по-русски: «Питание», «Бюджет» и т.п.). Короткое «угу»/«ага» на резюме — слабое подтверждение: client_confirmed=false.
4. Не угадывай. Чего клиент не сказал — value=null.
5. quote — ДОСЛОВНЫЙ фрагмент реплики из расшифровки (копируй символ в символ), из которого взято значение.
6. items — только оборудование, которое клиент решил брать (по резюме, если оно есть). Допустимые kind: \
{", ".join(f"{k} ({v})" for k, v in KINDS.items())}. Оборудование, от которого клиент отказался или которое \
уже есть, не включай. Оборудование, упомянутое в разговоре как возможное, но не вошедшее в резюме, — \
перечисли его kind в mentioned_not_in_summary.
7. max_rim_inch — максимальный посадочный диаметр колеса в дюймах (R22.5 → 22.5). voltage — 220 или 380 \
(«три фазы» = 380). budget_rub — верхняя граница бюджета в рублях. city — город доставки в именительном \
падеже («под Новосибирском» → «Новосибирск»).
8. summary — 1–3 предложения: к чему пришли. next_steps — договорённости о дальнейших действиях, deadline — \
обещанный срок («завтра до 12:00»), если назван.
"""


def extract_llm(lines: list[dict]) -> dict:
    transcript = "\n".join(f"{'Менеджер' if l['speaker'] == 'manager' else 'Клиент'}: {l['text']}" for l in lines)
    need = llm.call_json(SYSTEM, f"Расшифровка звонка:\n\n{transcript}", NEED_SCHEMA)
    for it in need["items"]:
        it["qty"] = max(1, int(it.get("qty") or 1))
    labels = {"voltage": "Питание", "max_rim_inch": "Диаметр колёс", "budget_rub": "Бюджет",
              "vehicle": "Тип транспорта", "installation": "Установка", "city": "Город доставки"}
    for c in need["conflicts"]:  # ИИ иногда пишет имя поля схемы, а не человеческое
        c["field"] = labels.get(c["field"], c["field"])
    in_items = {i["kind"] for i in need["items"]}
    need["mentioned_not_in_summary"] = [KINDS[k] for k in dict.fromkeys(need["mentioned_not_in_summary"])
                                        if k not in in_items]
    need["open_questions"] = open_questions(need)
    return need


def extract(text: str, use_llm: bool) -> tuple[list[dict], dict, list[str]]:
    """→ (реплики, потребность, служебные заметки)."""
    lines = parse_lines(text)
    notes = []
    if use_llm:
        try:
            need = extract_llm(lines)
            bad = verify_quotes(need, lines)
            if bad:
                notes.append(f"ИИ привёл {len(bad)} цитат(ы), которых нет в расшифровке — они скрыты, проверьте значения.")
            return lines, need, notes
        except llm.LLMError as e:
            notes.append(f"ИИ недоступен ({e}) — разбор выполнен по правилам.")
    return lines, extract_rules(lines), notes
