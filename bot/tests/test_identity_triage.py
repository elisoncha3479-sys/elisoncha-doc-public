"""Две кнопки охранника (инцидент 013).

Когда ФИО пациента в документе не подтвердилось, бот не угадывает, а
спрашивает двумя кнопками:
  acc  — «Анализ прислан случайно — не брать в работу»  → discard,
         НИЧЕГО никуда не сохраняем;
  mine — «Подтверждаю: это мой анализ»                   → process,
         запускаем полный обычный разбор (человек поручился).
"""

import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from identity_triage import (
    OPT_ACCIDENTAL,
    OPT_CONFIRM,
    TRIAGE_BUTTONS,
    build_callback_data,
    parse_callback_data,
    triage_response,
)


def test_exactly_two_buttons_accidental_then_confirm():
    codes = [c for c, _ in TRIAGE_BUTTONS]
    assert codes == [OPT_ACCIDENTAL, OPT_CONFIRM]
    for _, label in TRIAGE_BUTTONS:
        assert label and isinstance(label, str)


def test_callback_roundtrip_within_telegram_limit():
    for code in (OPT_ACCIDENTAL, OPT_CONFIRM):
        data = build_callback_data(code, "deadbeef")
        assert len(data.encode()) <= 64
        assert parse_callback_data(data) == (code, "deadbeef")


def test_parse_rejects_foreign_or_incomplete():
    assert parse_callback_data("") is None
    assert parse_callback_data("foo:bar") is None
    assert parse_callback_data("idt:acc") is None
    assert parse_callback_data("idt:zzz:tok") is None


def test_accidental_discards_and_saves_nothing():
    r = triage_response(OPT_ACCIDENTAL)
    assert r["action"] == "discard"
    assert r["ack"].strip()
    # никакого сохранения — даже флага _excluded больше нет
    assert "save_excluded" not in r


def test_confirm_processes_the_document():
    r = triage_response(OPT_CONFIRM)
    assert r["action"] == "process"
    assert "разбираю" in r["ack"].lower()


def test_unknown_code_returns_none():
    assert triage_response("zzz") is None
