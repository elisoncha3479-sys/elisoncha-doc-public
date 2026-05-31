"""Триаж охранника двумя кнопками (инцидент 013).

Когда ФИО пациента в документе не подтвердилось, бот не угадывает, а
спрашивает владельца и действует строго по выбору:

  acc  — «Анализ прислан случайно — не брать в работу»
         → discard: не анализируем, НИЧЕГО никуда не сохраняем;
  mine — «Подтверждаю: это мой анализ — взять в работу»
         → process: запускаем полный обычный разбор
           (человек поручился глазами — страховка от ошибки охранника
           на кривом OCR).

Только чистая логика (без Telegram), чтобы тестировать и переиспользовать.
"""
from __future__ import annotations

import patient_identity

TRIAGE_PREFIX = "idt"

OPT_ACCIDENTAL = "acc"
OPT_CONFIRM = "mine"

# Порядок фиксирован — он же порядок кнопок в чате.
# Коротко — Telegram обрезает длинные подписи кнопок. Полное пояснение
# (что это значит и последствие) — в тексте сообщения над кнопками.
TRIAGE_BUTTONS: list[tuple[str, str]] = [
    (OPT_ACCIDENTAL, "🚫 Прислала случайно"),
    (OPT_CONFIRM, "✅ Это мой анализ"),
]

_VALID_CODES = {code for code, _ in TRIAGE_BUTTONS}


def build_callback_data(code: str, token: str) -> str:
    """`idt:<code>:<token>` для Telegram callback_data (лимит 64 байта)."""
    return f"{TRIAGE_PREFIX}:{code}:{token}"


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Разбирает callback_data в (code, token). None для чужих/неполных."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    prefix, code, token = parts
    if prefix != TRIAGE_PREFIX or code not in _VALID_CODES or not token:
        return None
    return code, token


def triage_response(code: str) -> dict | None:
    """Что бот говорит и делает по кнопке.

    action="discard"  — не анализировать, ничего не сохранять;
    action="process"  — запустить полный обычный разбор как документ пациента.
    """
    if code == OPT_ACCIDENTAL:
        return {
            "ack": "Хорошо, не беру этот документ в работу 👌",
            "action": "discard",
        }
    if code == OPT_CONFIRM:
        who = patient_identity.address_name()
        ack = (
            f"Спасибо, {who} — разбираю ваш анализ. Минуту."
            if who
            else "Спасибо — разбираю ваш анализ. Минуту."
        )
        return {
            "ack": ack,
            "action": "process",
        }
    return None
