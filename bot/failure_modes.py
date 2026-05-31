"""Единое место для «честного поведения на сбоях» (аудит 2026-05-16, Тема 2).

Когда разбор не удался (сбой парсинга синтеза, ни одной успешной попытки
анализа), результат помечается SYNTHESIS_FAILED_KEY. Вызывающий код обязан:
  - НЕ писать per-doc / refresh / reconcile (не отравлять долгую память);
  - всё равно честно ответить пациенту (fallback в схеме рендерера).

Схема fallback держится здесь, рядом с предикатом, чтобы рендерер и
вызывающий код не разъезжались (паттерн source-of-truth-parity-guard).
"""
from __future__ import annotations

import patient_identity

SYNTHESIS_FAILED_KEY = "_synthesis_failed"


def is_degraded(analysis: dict) -> bool:
    """True, если разбор деградирован и его НЕЛЬЗЯ писать в долгую память."""
    return bool(analysis.get(SYNTHESIS_FAILED_KEY))


def make_fallback_analysis() -> dict:
    """Честный fallback в схеме report_renderer, когда разбор не собрался.

    Каждый вызов — свежая копия (без общего изменяемого состояния).
    """
    who = patient_identity.address_name()
    opening_lead = f"{who}, документ" if who else "Документ"
    return {
        "title": "Документ получен",
        "patient_line": "",
        "opening": (
            f"{opening_lead} получен и сохранён, но в этот раз "
            "собрать полный разбор не получилось. Команда «Elisoncha Doc» "
            "посмотрит логи и пришлёт разбор отдельно."
        ),
        "headline": None,
        "highlights": [],
        "lab_tables": None,
        "action_plan": [
            {
                "priority": "warning",
                "action": "Дождаться повторного разбора этого документа от команды.",
                "deadline": "В течение дня",
            }
        ],
        "personal_closing": (
            "Если что-то требует срочного внимания — пожалуйста, "
            "позвоните лечащему врачу. Мы вернёмся к вам в ближайшее время."
        ),
        SYNTHESIS_FAILED_KEY: True,
    }
