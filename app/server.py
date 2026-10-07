"""Веб-сервер прототипа. Запуск: python run.py → http://127.0.0.1:8000"""
from __future__ import annotations

import html
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import delivery, llm, pipeline
from .catalog import KINDS, search

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
app = FastAPI(title="Подбор КП")


class AnalyzeReq(BaseModel):
    text: str
    use_llm: bool | None = None


class KPItem(BaseModel):
    id: str
    qty: int = 1
    price_manual: float | None = None


class KPReq(BaseModel):
    draft_id: str | None = None
    deal_id: str = ""
    client: str = ""
    manager: str = ""
    discount_pct: float = 0
    budget: int | None = None
    summary: str = ""
    edited: bool = False
    items: list[KPItem]
    delivery: dict | None = None  # {include, city, km}


class DeliveryReq(BaseModel):
    items: list[KPItem]
    city: str | None = None
    km: float | None = None


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/meta")
def meta():
    return {"llm": llm.available(), "model": llm.MODEL, "kinds": KINDS,
            "discount_limit": pipeline.DISCOUNT_LIMIT,
            "samples": sorted(p.stem for p in SAMPLES.glob("*.txt"))}


@app.get("/api/samples/{name}")
def sample(name: str):
    f = SAMPLES / f"{Path(name).name}.txt"
    if not f.exists():
        raise HTTPException(404, "Нет такого примера")
    return {"text": f.read_text(encoding="utf-8")}


@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    if not req.text.strip():
        raise HTTPException(400, "Пустая расшифровка")
    if len(req.text) > 60_000:
        raise HTTPException(400, "Расшифровка слишком длинная (больше 60 000 символов)")
    return pipeline.analyze(req.text, req.use_llm)


@app.get("/api/catalog")
def catalog(q: str = "", kind: str = ""):
    return [p.to_dict() for p in search(q, kind)]


@app.get("/api/cities")
def cities():
    return sorted(delivery.CITY_KM)


@app.post("/api/delivery")
def calc_delivery(req: DeliveryReq):
    return delivery.calc([i.model_dump() for i in req.items], city=req.city or None, km=req.km)


@app.post("/api/kp")
def make_kp(req: KPReq):
    return pipeline.build_kp(req.model_dump())


@app.post("/api/b24/{kp_id}")
def b24(kp_id: str):
    kp = pipeline.load_kp(kp_id)
    if not kp:
        raise HTTPException(404, "КП не найдено")
    return pipeline.b24_send(kp)


@app.get("/api/stats")
def stats():
    return pipeline.stats()


def rub(v) -> str:
    return f"{int(v):,}".replace(",", " ") + " ₽"


@app.get("/kp/{kp_id}", response_class=HTMLResponse)
def kp_page(kp_id: str):
    kp = pipeline.load_kp(kp_id)
    if not kp:
        raise HTTPException(404, "КП не найдено")
    e = html.escape
    rows = "".join(
        f"<tr><td>{i}</td><td><b>{e(r['name'])}</b><div class='sp'>{e(r['kind_name'])}"
        f"{' · ' + e(r['brand']) if r['brand'] else ''}"
        f"{''.join(f' · {e(k)}: {e(str(v))}' for k, v in list(r['specs'].items())[:4] if k not in ('Производитель',))}"
        f"</div></td><td class='n'>{r['qty']}</td><td class='n'>{rub(r['price'])}</td><td class='n'>{rub(r['sum'])}</td></tr>"
        for i, r in enumerate(kp["rows"], 1))
    disc = (f"<tr><td colspan=4 class='n'>Скидка {kp['discount_pct']:g}%</td><td class='n'>−{rub(kp['discount'])}</td></tr>"
            if kp["discount"] else "")
    d = kp.get("delivery")
    if d:
        where = e(d["city"]) if d["city"] else f"{d['km']} км от Москвы"
        free = f"<div class='sp'>{'<br>'.join(e(x) for x in d['free'])}</div>" if d["free"] else ""
        disc += (f"<tr><td colspan=4 class='n'>Доставка со склада в Москве до: {where}, срок {e(d['days'])}{free}</td>"
                 f"<td class='n'>{rub(d['cost']) if d['cost'] else 'бесплатно'}</td></tr>")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{e(kp['kp_id'])}</title>
<style>
 body{{font-family:Arial,sans-serif;color:#1d2433;max-width:820px;margin:32px auto;padding:0 24px;font-size:14px}}
 h1{{font-size:22px;margin:0 0 4px}} .muted{{color:#667085}} .sp{{color:#667085;font-size:12px;margin-top:2px}}
 table{{width:100%;border-collapse:collapse;margin:20px 0}} th,td{{padding:8px 6px;border-bottom:1px solid #e4e7ec;text-align:left;vertical-align:top}}
 th{{background:#f2f5fa;font-size:12px}} .n{{text-align:right;white-space:nowrap}} .tot td{{font-weight:bold;font-size:16px;border:0}}
 .head{{display:flex;justify-content:space-between;gap:24px;border-bottom:3px solid #1f3864;padding-bottom:12px}}
 .note{{background:#fff8e6;border:1px solid #f5d38a;padding:8px 12px;border-radius:6px;font-size:12px}}
 button{{padding:8px 16px;font-size:14px;cursor:pointer}} @media print{{.noprint{{display:none}} body{{margin:0}}}}
</style></head><body>
<div class="noprint" style="margin-bottom:16px"><button onclick="print()">Сохранить в PDF / печать</button></div>
<div class="head"><div><h1>Коммерческое предложение</h1><div class="muted">{e(kp['kp_id'])} от {kp['date']}</div></div>
<div style="text-align:right"><b>АО «Автосервис-Снаб»</b><br><span class="muted">+7 (000) 000-00-00 · sales@example.com</span></div></div>
<p>{('Для: <b>' + e(kp['client']) + '</b><br>') if kp['client'] else ''}По итогам нашего разговора предлагаем оборудование:</p>
<table><tr><th>№</th><th>Наименование</th><th class="n">Кол-во</th><th class="n">Цена</th><th class="n">Сумма</th></tr>{rows}
<tr><td colspan=4 class="n">Итого без скидки</td><td class="n">{rub(kp['subtotal'])}</td></tr>{disc}
<tr class="tot"><td colspan=4 class="n">К оплате</td><td class="n">{rub(kp['total'])}</td></tr></table>
<p>Гарантия 12 месяцев (стенды сход-развал — 2 года). {'Стоимость доставки указана ориентировочно и уточняется при оформлении заказа.' if d else 'Доставка по России, стоимость рассчитаем до вашего города.'}
Возможен лизинг с авансом от 10%. Предложение действительно до {kp['valid_until']}.</p>
<p>{('Менеджер: ' + e(kp['manager'])) if kp['manager'] else ''}</p>
<p class="note">Прототип: цены взяты с витрины сайта и могут отличаться от актуального прайса.</p>
</body></html>"""
