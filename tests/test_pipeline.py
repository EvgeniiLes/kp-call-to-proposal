"""Смоук-тесты конвейера в режиме «по правилам» (без ключа API). Запуск: python -m pytest -q"""
import json
from pathlib import Path

import pytest

from app import pipeline

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(autouse=True)
def tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(pipeline, "DATA", tmp_path)


def draft(name):
    return pipeline.analyze((SAMPLES / f"{name}.txt").read_text(encoding="utf-8"), use_llm=False)


def items(d):
    return [{"id": s["selected"], "qty": s["qty"]} for s in d["slots"] if s["selected"]]


def test_truck_tire_shop_gets_changer_and_balancer():
    d = draft("1_грузовой_шиномонтаж")
    assert d["mode"] == "rules"
    assert {s["kind"] for s in d["slots"] if s["selected"]} == {"tire_changer_truck", "balancer_truck"}


def test_call_without_summary_is_blocking():
    d = draft("4_без_резюме")
    assert any(w["level"] == "block" for w in d["warnings"])


def test_price_on_request_needs_manual_price():
    d = draft("3_сход_развал")
    kp = pipeline.build_kp({"draft_id": d["draft_id"], "items": items(d)})
    assert not kp["ok"] and "цена по запросу" in kp["errors"][0]


def test_discount_over_limit_rejected():
    d = draft("1_грузовой_шиномонтаж")
    kp = pipeline.build_kp({"draft_id": d["draft_id"], "items": items(d), "discount_pct": 50})
    assert not kp["ok"]


def test_edited_metric_is_computed_on_server():
    d = draft("1_грузовой_шиномонтаж")
    assert pipeline.build_kp({"draft_id": d["draft_id"], "items": items(d), "edited": True})["ok"]
    changed = items(d)
    changed[0]["qty"] += 1
    # браузер говорит «правок нет», но количество изменено — сервер должен это заметить
    assert pipeline.build_kp({"draft_id": d["draft_id"], "items": changed, "edited": False})["ok"]
    assert pipeline.stats() == {"drafts": 1, "kp": 2, "kp_without_edits": 1}
