"""Аудит 2026-05-16, Тема 3: подключить «самое важное» в ЖИВОЙ промт.

Раньше связки между областями, «5 контуров», тёплый тон, юмор и
«что это значит для Вас» жили только в agents/chief.md, который
генератор отчёта НЕ читает. Теперь ключевые директивы вшиты в
build_synthesis_system() и в системный промт специалиста.
"""

import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from agents import build_synthesis_system, build_specialist_system
from context import SYNTHESIS_DIRECTIVES, SPECIALIST_DIRECTIVE


def _synthesis_text() -> str:
    blocks = build_synthesis_system()
    return "\n".join(b["text"] for b in blocks)


def test_synthesis_carries_cross_area_linkage():
    t = _synthesis_text()
    assert "СВЯЗЬ МЕЖДУ ОБЛАСТЯМИ" in t
    assert "5 контуров" in t


def test_synthesis_carries_tone_humor_lifestyle():
    t = _synthesis_text()
    assert "Что это значит для Вас" in t
    assert "тёплая интонация" in t
    assert "лёгкий юмор" in t
    # финальный фильтр перед отправкой пациентке
    assert "ФИНАЛЬНЫЙ ФИЛЬТР" in t


def test_synthesis_directives_constant_is_used_verbatim():
    # Директивы не переписаны в agents.py, а берутся из единого источника.
    assert SYNTHESIS_DIRECTIVES.strip()
    assert SYNTHESIS_DIRECTIVES in _synthesis_text()


def test_specialist_system_has_cross_area_directive():
    sys_text = build_specialist_system(
        agent_prompt="ТЫ КАРДИОЛОГ",
        history_summary="история",
    )
    assert "ТЫ КАРДИОЛОГ" in sys_text
    assert "СВЯЗЬ С ДРУГИМИ ОБЛАСТЯМИ" in sys_text
    assert SPECIALIST_DIRECTIVE in sys_text


def test_specialist_directive_mentions_naming_the_link():
    assert "назови эту связь явно" in SPECIALIST_DIRECTIVE
