"""Аудит 2026-05-16, Тема 2: честное поведение на сбоях.

Раньше:
  - сбой парсинга синтеза → заглушка с пустыми highlights всё равно шла
    в per-doc + refresh + reconcile, отравляя долгую память;
  - FALLBACK_ANALYSIS был в старой схеме → рендерился почти пустой PDF;
  - ничто не помечало, что разбор деградирован.

Теперь деградированный analysis помечается is_degraded(), финал
пропускает запись в память (но честно отвечает пациенту), а fallback
сделан в схеме рендерера и осмысленно рендерится.
"""

import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from failure_modes import (
    SYNTHESIS_FAILED_KEY,
    is_degraded,
    make_fallback_analysis,
)


def test_is_degraded_predicate():
    assert is_degraded({SYNTHESIS_FAILED_KEY: True}) is True
    assert is_degraded({SYNTHESIS_FAILED_KEY: False}) is False
    assert is_degraded({}) is False
    assert is_degraded({"title": "ok"}) is False
    assert is_degraded({SYNTHESIS_FAILED_KEY: True, "_routing": {}}) is True


def test_fallback_uses_renderer_schema_and_is_marked():
    fb = make_fallback_analysis()
    # помечен как деградированный → память пропускается осознанно
    assert fb.get(SYNTHESIS_FAILED_KEY) is True
    assert is_degraded(fb) is True
    # ключи новой схемы рендерера (а не мёртвые summary/good/closing)
    for key in ("title", "opening", "personal_closing", "action_plan"):
        assert key in fb, f"fallback без ключа {key}"
    assert "summary" not in fb and "telegram_message" not in fb


def test_fallback_is_a_fresh_copy_each_call():
    a = make_fallback_analysis()
    a["highlights"].append("мутация")
    b = make_fallback_analysis()
    assert b["highlights"] == []  # независимая копия, без общего состояния


def test_fallback_text_is_honest_about_failure():
    fb = make_fallback_analysis()
    blob = (fb["opening"] + fb["personal_closing"]).lower()
    # честно говорит, что разбор не собрался, и куда обращаться
    assert "не получилось" in blob or "не собрать" in blob.replace("ё", "е")
    assert "врач" in blob
    # action_plan непустой и со схемой рендерера
    assert fb["action_plan"] and "action" in fb["action_plan"][0]
