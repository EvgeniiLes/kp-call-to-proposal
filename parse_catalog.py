"""Парсер каталога с лендингов example.com.

Товары встроены в HTML страниц как JS-массивы (window.NB_CATALOG, window.NB_PRODUCTS и др.).
Скрипт находит все массивы объектов, похожих на товары, и сохраняет их в catalog_raw.json
и нормализованный catalog.json / catalog.csv.
"""
import csv
import json
import re
import urllib.request
from pathlib import Path

SITES = {
    "shinomontazh": "https://shinomontazh.example.com",
    "truck": "https://truck.example.com",
    "technovector": "https://technovector.example.com",
    "color-camera": "https://color-camera.example.com",
}
OUT = Path(__file__).parent / "data"
ASSIGN_RE = re.compile(r"(?:window\.|const |let |var )([A-Za-z_][\w\.]*)\s*=\s*\[\s*\{")
NAME_KEYS = ("title", "n", "name", "model")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_arrays(html: str) -> dict[str, list]:
    dec = json.JSONDecoder()
    found = {}
    for m in ASSIGN_RE.finditer(html):
        start = html.index("[", m.start())
        try:
            arr, _ = dec.raw_decode(html, start)
        except json.JSONDecodeError:
            continue  # JS-литерал, а не JSON (квизы, конфиги) — пропускаем
        if arr and all(isinstance(x, dict) for x in arr):
            found.setdefault(m.group(1), []).extend(arr)
    return found


def js_literal_array(html: str, var: str) -> list:
    """Массив в виде JS-литерала ({id:'x', ...}) → Python. Нужен для window.CC_MODELS."""
    m = re.search(rf"{re.escape(var)}\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        return []
    s = m.group(1)
    s = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda x: json.dumps(x.group(1)), s)  # '..' → ".."
    s = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', s)  # key: → "key":
    s = re.sub(r",\s*([\]}])", r"\1", s)  # висячие запятые
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        print(f"  не удалось разобрать {var}: {e}")
        return []


def color_camera_extra(base: str, html: str) -> dict[str, list]:
    """Камеры (CC_MODELS) и оснащение малярного участка (/assets/equipment.js, без цен)."""
    out = {}
    models = js_literal_array(html, "window.CC_MODELS")
    if models:
        out["CC_MODELS"] = [{
            "id": x.get("id"), "title": f"Окрасочно-сушильная камера {x.get('name')}", "v": x.get("brand"),
            "specs": [[k, x[k] if isinstance(x[k], str) else ", ".join(x[k])]
                      for k in ("use", "zone", "dim", "burner", "fuel", "fact") if x.get(k)],
        } for x in models]
    eq = fetch(base + "/assets/equipment.js")
    data, _ = json.JSONDecoder().raw_decode(eq, eq.index("{"))
    groups = {g["id"]: g["t"] for g in data.get("groups", [])}
    out["CC_EQUIPMENT"] = [{
        "id": x["i"], "title": x["n"], "v": "NORDBERG",
        "specs": [["Группа", groups.get(x.get("g"), x.get("g"))], ["Модель", x.get("m", "")]]
                 + [[k, v if isinstance(v, str) else ", ".join(v)] for k, v in (x.get("p") or {}).items()],
    } for x in data.get("items", [])]
    return out


def parse_price(v) -> int | None:
    if isinstance(v, (int, float)):
        return int(v) or None
    if isinstance(v, str):
        digits = re.sub(r"\D", "", v)
        return int(digits) if digits else None
    return None


def normalize(site: str, var: str, item: dict) -> dict | None:
    name = next((item[k] for k in NAME_KEYS if isinstance(item.get(k), str) and item.get(k)), None)
    if not name:
        return None
    special = item.get("special") if isinstance(item.get("special"), str) else ""
    # карточки категорий: «38 моделей» на shinomontazh, cat-* с ценой «от …» на truck
    is_category = bool(item.get("stock")) or str(item.get("id", "")).startswith("cat-") or special.startswith("от ")
    price = parse_price(item.get("p")) or (parse_price(special) if "₽" in special and not is_category else None)
    specs = item.get("specs") or []
    if item.get("cl"):
        specs = [*specs, ["Диаметр обода, дюйм", item["cl"]]]
    if item.get("segment"):
        specs = [*specs, ["Сегмент", item["segment"]]]
    return {
        "id": f"{site}:{item.get('id', name)}",
        "site": site,
        "source_var": var,
        "kind": "category" if is_category else "product",
        "name": name,
        "brand": item.get("v") or dict((s[0], s[1]) for s in specs if len(s) == 2).get("Производитель", ""),
        "price_rub": price,  # None = «цена по запросу»
        "voltage": item.get("volt", ""),
        "specs": {s[0]: s[1] for s in specs if isinstance(s, list) and len(s) == 2},
    }


def main():
    OUT.mkdir(exist_ok=True)
    raw, items, seen = {}, [], set()
    for site, url in SITES.items():
        try:
            html = fetch(url)
            arrays = extract_arrays(html)
            if site == "color-camera":
                arrays.update(color_camera_extra(url, html))
        except Exception as e:  # noqa: BLE001
            print(f"[{site}] ошибка загрузки: {e}")
            continue
        raw[site] = arrays
        for var, arr in arrays.items():
            n = 0
            for it in arr:
                row = normalize(site, var, it)
                if row and row["id"] not in seen:
                    seen.add(row["id"])
                    items.append(row)
                    n += 1
            print(f"[{site}] {var}: {len(arr)} объектов, товаров добавлено {n}")

    (OUT / "catalog_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "catalog.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(OUT / "catalog.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "site", "kind", "name", "brand", "price_rub", "voltage", "specs"])
        for r in items:
            w.writerow([r["id"], r["site"], r["kind"], r["name"], r["brand"], r["price_rub"] or "", r["voltage"],
                        "; ".join(f"{k}: {v}" for k, v in r["specs"].items())])
    priced = sum(1 for r in items if r["price_rub"])
    print(f"\nИтого товаров: {len(items)}, с ценой: {priced}. Сохранено в {OUT}")


if __name__ == "__main__":
    main()

