"""Единый источник 7/8-секционного шаблона profile.md.

profile_refresher раньше описывал структуру прозой → авто-refresh плодил
произвольную разбивку (кардиолог без ## 4 ЛЕКАРСТВА), расходясь с явным
каркасом commands/refresh-profile.md и обнуляя smart-фильтр reconcile (#4).

Контракт генератор↔парсер: что REFRESH_INSTRUCTION просит сгенерировать,
то meds_section.extract_meds_section должен уметь распарсить.
"""

from pathlib import Path

import pytest

from profile_template import (
    CANONICAL_SECTION_HEADERS,
    MEDS_SECTION_ANCHOR,
    load_profile_skeleton,
)
from meds_section import extract_meds_section
import profile_refresher


def test_skeleton_has_all_canonical_sections():
    sk = load_profile_skeleton()
    for header in CANONICAL_SECTION_HEADERS:
        assert header in sk, f"в шаблоне нет секции: {header!r}"


def test_skeleton_contains_meds_anchor():
    """## 4. ЛЕКАРСТВА — якорь, на котором держится extract_meds_section."""
    sk = load_profile_skeleton()
    assert MEDS_SECTION_ANCHOR in sk


def test_generator_parser_contract():
    """Парсер раздела «Лекарства» обязан находить секцию в том самом шаблоне,
    который генератор просит у LLM. Это ядро бага-011."""
    sk = load_profile_skeleton()
    meds = extract_meds_section(sk)
    assert meds, "extract_meds_section не нашёл ## 4 в каноническом шаблоне"
    assert "ЛЕКАРСТВ" in meds.upper()


def test_skeleton_loaded_from_command_file():
    """По умолчанию шаблон читается из commands/refresh-profile.md —
    истинно единый источник, без дублирования (bug-011 «How to avoid»)."""
    sk = load_profile_skeleton()
    # Признаки именно командного шаблона, не куцего fallback'а
    assert "Свод по пациентке" in sk
    assert "ХРОНОЛОГИЯ" in sk and "КРАСНЫЕ ФЛАГИ" in sk


def test_fallback_when_command_missing_still_satisfies_contract(tmp_path):
    """Если commands/refresh-profile.md недоступен (напр., не смонтирован в
    docker — ср. bug-009) — fallback всё равно держит контракт с парсером."""
    sk = load_profile_skeleton(command_file=tmp_path / "нет.md")
    for header in CANONICAL_SECTION_HEADERS:
        assert header in sk
    assert MEDS_SECTION_ANCHOR in sk
    assert extract_meds_section(sk).strip()


def test_refresh_instruction_embeds_skeleton():
    """REFRESH_INSTRUCTION больше не описывает структуру прозой — в нём
    лежит явный канонический каркас."""
    instr = profile_refresher.REFRESH_INSTRUCTION
    assert MEDS_SECTION_ANCHOR in instr
    present = sum(1 for h in CANONICAL_SECTION_HEADERS if h in instr)
    assert present == len(CANONICAL_SECTION_HEADERS), (
        f"в REFRESH_INSTRUCTION только {present}/{len(CANONICAL_SECTION_HEADERS)} секций"
    )
