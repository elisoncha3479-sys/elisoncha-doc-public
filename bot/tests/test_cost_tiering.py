"""Cost-tiering (2026-05-30): механика на Haiku, медрассуждение на Sonnet.

Защита от регресса: повод — счёт Anthropic API > $100/мес, почти всё
крутилось на Sonnet по умолчанию.
Эти тесты ловят, если дорогая модель снова
расползётся по механическим шагам.
"""
from __future__ import annotations

import importlib

import pytest

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"


@pytest.mark.parametrize("module,attr", [
    ("ocr_validator", "VALIDATOR_MODEL"),
    ("historical_check", "CHECK_MODEL"),
    ("profile_refresher", "REFRESHER_MODEL"),
    ("reconcile", "RECONCILE_MODEL"),
    ("digest", "DIGEST_MODEL"),
    ("history_regenerator", "REGEN_MODEL"),
])
def test_mechanical_steps_default_to_haiku(module, attr):
    m = importlib.import_module(module)
    assert getattr(m, attr) == HAIKU, f"{module}.{attr} должен дефолтиться на Haiku"


def test_routing_haiku_but_reasoning_stays_sonnet(monkeypatch):
    """routing (выбор врачей) — дешёвый Haiku; мнения специалистов и
    финальная сборка — остаются на Sonnet (там реально думают).

    Чистый reload без env-оверрайдов: другие тесты могут перезагружать
    agents с подменой модели, мы проверяем именно дефолты кода."""
    monkeypatch.delenv("ROUTING_MODEL", raising=False)
    monkeypatch.delenv("AGENTS_MODEL", raising=False)
    agents = importlib.reload(importlib.import_module("agents"))
    assert agents.ROUTING_MODEL == HAIKU
    assert agents.AGENTS_MODEL == SONNET


def test_patient_facing_reply_stays_sonnet():
    """Ответы маме в чат — тепло и точность важны, остаются на Sonnet."""
    pm = importlib.import_module("patient_message")
    assert pm.PATIENT_MSG_MODEL == SONNET


def test_cost_estimate_haiku_cheaper_than_sonnet():
    cost_log = importlib.import_module("cost_log")
    sonnet = cost_log.estimate_usd(SONNET, 10_000, 2_000)
    haiku = cost_log.estimate_usd(HAIKU, 10_000, 2_000)
    assert haiku < sonnet
    # неизвестная модель — консервативно как Sonnet, не дешевле
    assert cost_log.estimate_usd("unknown-model", 10_000, 2_000) == sonnet
