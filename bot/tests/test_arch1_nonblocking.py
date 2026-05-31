"""АРХ1 (2026-05-30): синхронные LLM-вызовы на event loop обёрнуты в
asyncio.to_thread, чтобы бот не «залипал» во время тяжёлого разбора и
сразу забирал новые сообщения из Telegram.

Source-level guard от регресса: в живых async-модулях не должно остаться
ни одного синхронного .messages.create(...), висящего прямо на loop.
Корень инцидента 2026-05-30: заблокированный loop → follow-up ждал ~15 мин,
synthesis ловил watchdog.
"""
from __future__ import annotations

from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent


def _src(name: str) -> str:
    return (_BOT_DIR / name).read_text(encoding="utf-8")


def test_agents_pipeline_creates_wrapped():
    """run_multi_agent: routing/специалист/synthesis — все через to_thread,
    без прямого присваивания client.messages.create на loop."""
    src = _src("agents.py")
    assert "await asyncio.to_thread(" in src
    assert "= client.messages.create(" not in src


def test_main_creates_wrapped():
    """main.py: _analyze_ocr_text/_analyze_image/_chat_reply/_vision — все
    через to_thread."""
    src = _src("main.py")
    assert "await asyncio.to_thread(" in src
    assert "= claude.messages.create(" not in src


def test_patient_message_callsites_wrapped():
    """handle_patient_message зовёт синхронные classify/generate через
    to_thread (сами функции остаются sync — их юнит-тесты не трогаются)."""
    src = _src("patient_message.py")
    assert "asyncio.to_thread(classify_patient_message" in src
    assert "asyncio.to_thread(generate_patient_reply" in src
