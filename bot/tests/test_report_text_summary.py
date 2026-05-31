"""Аудит 2026-05-16, Тема 4 (C1): месячный дайджест был всегда пуст.

load_recent_reports() читает reports/*.{txt,md}, но конвейер писал только
reports/{ts}_report.pdf → блок «отчёты за 30 дней» в месячном письме
всегда пустой. Фикс: рядом с PDF пишем человекочитаемую .md-сводку.
build_report_text_summary() — её чистый генератор.
"""

import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from context import build_report_text_summary


SAMPLE = {
    "title": "Биохимия крови",
    "opening": "Мария Петровна, посмотрели вашу биохимию.",
    "headline": {"headline": "Холестерин чуть выше нормы"},
    "action_plan": [
        {"priority": "warning", "action": "Сдать липидограмму", "deadline": "2 недели"},
        {"priority": "info", "action": "Меньше жирного", "deadline": ""},
    ],
    "personal_closing": "Берегите себя.",
}


def test_summary_is_nonempty_plaintext():
    s = build_report_text_summary(SAMPLE)
    assert isinstance(s, str) and s.strip()
    assert "Биохимия крови" in s
    assert "посмотрели вашу биохимию" in s


def test_summary_includes_headline_and_actions():
    s = build_report_text_summary(SAMPLE)
    assert "Холестерин чуть выше нормы" in s
    assert "Сдать липидограмму" in s
    assert "Меньше жирного" in s


def test_summary_handles_degraded_and_empty():
    assert build_report_text_summary({}).strip()  # не падает на пустом
    assert "разбор" in build_report_text_summary(
        {"opening": "разбор не собрался"}
    ).lower()
