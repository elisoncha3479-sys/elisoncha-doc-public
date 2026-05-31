"""
Тесты для динамического реестра специалистов в bot/agents.py.

Покрывают A1: data-driven регистрация специалистов через YAML-frontmatter
в файлах agents/*.md.
"""
import logging
import textwrap
from pathlib import Path

import pytest

from agents import (
    AGENTS_DIR,
    build_routing_prompt,
    discover_specialists,
    parse_frontmatter,
)


# ---------- Парсер frontmatter ----------

def test_parse_frontmatter_basic():
    text = textwrap.dedent("""\
        ---
        slug: cardiologist
        name_ru: кардиолог
        role: specialist
        ---

        # Кардиолог
        тело документа
    """)
    fm, body = parse_frontmatter(text)
    assert fm == {"slug": "cardiologist", "name_ru": "кардиолог", "role": "specialist"}
    assert body.lstrip().startswith("# Кардиолог")


def test_parse_frontmatter_no_block_returns_empty():
    text = "# Какой-то агент\nбез шапки\n"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_folded_scalar():
    """YAML folded scalar (>) — переносы строк превращаются в пробелы."""
    text = textwrap.dedent("""\
        ---
        slug: cardiologist
        name_ru: кардиолог
        role: specialist
        domain: >
          ишемическая болезнь сердца,
          артериальная гипертония,
          ХСН.
        ---
        body
    """)
    fm, _ = parse_frontmatter(text)
    assert fm["slug"] == "cardiologist"
    assert fm["domain"] == "ишемическая болезнь сердца, артериальная гипертония, ХСН."
    assert fm["role"] == "specialist"


def test_parse_frontmatter_literal_scalar():
    """YAML literal scalar (|) — переносы строк сохраняются."""
    text = textwrap.dedent("""\
        ---
        slug: x
        name_ru: икс
        role: specialist
        body_template: |
          Line one
          Line two
        ---
    """)
    fm, _ = parse_frontmatter(text)
    assert fm["body_template"] == "Line one\nLine two"
    assert fm["role"] == "specialist"


def test_parse_frontmatter_empty_value_with_continuation():
    """Пустое значение после двоеточия + отступленная строка → как folded."""
    text = textwrap.dedent("""\
        ---
        slug: y
        name_ru: игрек
        role: specialist
        domain:
          одна строка,
          вторая строка
        ---
    """)
    fm, _ = parse_frontmatter(text)
    assert fm["domain"] == "одна строка, вторая строка"


def test_parse_frontmatter_ignores_quotes_and_extra_spaces():
    text = textwrap.dedent("""\
        ---
        slug:   nephrologist
        name_ru: "нефролог"
        role: 'specialist'
        ---
        body
    """)
    fm, _ = parse_frontmatter(text)
    assert fm["slug"] == "nephrologist"
    assert fm["name_ru"] == "нефролог"
    assert fm["role"] == "specialist"


# ---------- discover_specialists ----------

def _write_agent(directory: Path, slug: str, name_ru: str, role: str = "specialist") -> None:
    (directory / f"{slug}.md").write_text(
        textwrap.dedent(f"""\
            ---
            slug: {slug}
            name_ru: {name_ru}
            role: {role}
            ---

            # {name_ru.capitalize()}
            заглушка
        """),
        encoding="utf-8",
    )


def test_discover_specialists_returns_mapping(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "nephrologist", "нефролог")

    result = discover_specialists(tmp_path)
    assert result == {"кардиолог": "cardiologist", "нефролог": "nephrologist"}


def test_discover_excludes_role_meta(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог", role="specialist")
    _write_agent(tmp_path, "chief", "главврач", role="meta")
    _write_agent(tmp_path, "ocr-validator", "ocr-валидатор", role="meta")

    result = discover_specialists(tmp_path)
    assert "кардиолог" in result
    assert "главврач" not in result
    assert "ocr-валидатор" not in result


def test_discover_skips_files_without_frontmatter(tmp_path, caplog):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    (tmp_path / "legacy.md").write_text("# Старый агент без шапки\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = discover_specialists(tmp_path)

    assert result == {"кардиолог": "cardiologist"}
    assert any("legacy.md" in r.message for r in caplog.records)


def test_discover_deterministic_order_by_slug(tmp_path):
    _write_agent(tmp_path, "orthopedist", "ортопед")
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "nephrologist", "нефролог")

    result = discover_specialists(tmp_path)
    assert list(result.values()) == ["cardiologist", "nephrologist", "orthopedist"]


def test_discover_ignores_unknown_role(tmp_path, caplog):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "weird", "странный", role="superhero")

    with caplog.at_level(logging.WARNING):
        result = discover_specialists(tmp_path)

    assert result == {"кардиолог": "cardiologist"}


def test_discover_requires_slug_and_name_ru(tmp_path, caplog):
    (tmp_path / "broken.md").write_text(
        textwrap.dedent("""\
            ---
            role: specialist
            ---
            # Нет slug и name_ru
        """),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        result = discover_specialists(tmp_path)
    assert result == {}


# ---------- Активный состав (data/active_specialists.json) ----------
#
# agents/ = библиотека общих архетипов. Под конкретного человека онбординг
# пишет data/active_specialists.json — подмножество активных slug.
# Файла нет → активны ВСЕ (обратная совместимость: исходная установка и
# репо-дефолт не меняются).

import json


def test_active_roster_absent_returns_all(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "hematologist", "гематолог")
    result = discover_specialists(tmp_path, active_roster_path=tmp_path / "nope.json")
    assert set(result.values()) == {"cardiologist", "hematologist"}


def test_active_roster_filters_to_subset(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "hematologist", "гематолог")
    _write_agent(tmp_path, "nephrologist", "нефролог")
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps(["cardiologist", "hematologist"]), encoding="utf-8")

    result = discover_specialists(tmp_path, active_roster_path=roster)
    assert set(result.values()) == {"cardiologist", "hematologist"}
    assert "nephrologist" not in result.values()


def test_active_roster_unknown_slug_ignored(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    roster = tmp_path / "active.json"
    roster.write_text(json.dumps(["cardiologist", "ghost"]), encoding="utf-8")

    result = discover_specialists(tmp_path, active_roster_path=roster)
    assert set(result.values()) == {"cardiologist"}


def test_active_roster_empty_or_broken_falls_back_to_all(tmp_path, caplog):
    """Пустой/битый список — НЕ оставляет человека без врачей: активны все."""
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "hematologist", "гематолог")

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert set(discover_specialists(tmp_path, active_roster_path=empty).values()) == {
        "cardiologist", "hematologist"}

    broken = tmp_path / "broken.json"
    broken.write_text("{не json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        res = discover_specialists(tmp_path, active_roster_path=broken)
    assert set(res.values()) == {"cardiologist", "hematologist"}


# ---------- Реальная директория agents/ ----------

def test_real_agents_dir_has_expected_specialists():
    """Реальная директория agents/ выдаёт всех зарегистрированных специалистов.

    После пилота A5 в команду добавлен невролог — теперь специалистов 8.
    Если в будущем добавятся новые через approve-flow — добавь в expected.
    """
    result = discover_specialists(AGENTS_DIR)
    assert set(result.values()) == {
        "cardiologist",
        "nephrologist",
        "gastroenterologist",
        "endocrinologist",
        "orthopedist",
        "hematologist",
        "lab-analyst",
        "neurologist",
        # Библиотека общих архетипов (онбординг включает подмножество
        # под конкретного человека через data/active_specialists.json):
        "dental-gnathologist",
        "eating-disorder-therapist",
        "dietitian",
    }


def test_new_archetypes_parse_and_register():
    """Три новых архетипа имеют валидный frontmatter и видны реестру."""
    result = discover_specialists(AGENTS_DIR)
    assert result.get("стоматолог-гнатолог") == "dental-gnathologist"
    assert result.get("рпп-терапевт") == "eating-disorder-therapist"
    assert result.get("диетолог") == "dietitian"


def test_archetypes_are_generic_no_patient_data():
    """Архетип = роль, не досье: ни личных имён, ни конкретных диагнозов
    человека в файлах врачей (конкретика живёт в персоне/профиле)."""
    for slug in ("hematologist", "dental-gnathologist",
                 "eating-disorder-therapist", "dietitian"):
        text = (AGENTS_DIR / f"{slug}.md").read_text(encoding="utf-8")
        low = text.lower()
        for forbidden in ("чащин", "людмил", "семён", "1949", "1955",
                           "ростов-на-дону"):
            assert forbidden not in low, f"{slug}.md: личные данные ({forbidden})"
        # обобщённый гематолог — без онко-привкуса
    hema = (AGENTS_DIR / "hematologist.md").read_text(encoding="utf-8").lower()
    assert "онкогематолог" not in hema and "онкоанамнез" not in hema


def test_eating_disorder_and_dietitian_are_cross_linked():
    """Связка РПП↔диетолог прописана в контрактах ОБОИХ архетипов."""
    therapist = (AGENTS_DIR / "eating-disorder-therapist.md").read_text(encoding="utf-8")
    dietitian = (AGENTS_DIR / "dietitian.md").read_text(encoding="utf-8")
    assert "диетолог" in therapist and "coordinate:dietitian" in therapist
    assert ("рпп-терапевт" in dietitian.lower()
            and "coordinate:eating-disorder-therapist" in dietitian)


def test_real_agents_dir_excludes_chief_and_validator():
    result = discover_specialists(AGENTS_DIR)
    assert "главврач" not in result
    assert "ocr-валидатор" not in result
    assert "chief" not in result.values()
    assert "ocr-validator" not in result.values()


def test_neurologist_appears_when_file_added(tmp_path):
    """E2E пилота A5: положили agents/neurologist.md → он в map без правки кода."""
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "neurologist", "невролог")
    result = discover_specialists(tmp_path)
    assert result["невролог"] == "neurologist"


# ---------- build_routing_prompt ----------

def test_routing_prompt_lists_all_specialist_names():
    prompt = build_routing_prompt()
    for name in ("кардиолог", "нефролог", "гастроэнтеролог",
                 "эндокринолог", "ортопед", "гематолог", "лаборант"):
        assert name in prompt, f"в routing-промпте нет {name}"


def test_routing_prompt_keeps_routing_rules():
    """Ключевые маркеры нового двухшагового промпта (2026-05-30):
    тип документа → специалисты по типу + принцип «недобор лучше перебора».
    Защита от регрессии: главврач не должен снова подключать четверых
    врачей на простую выписку."""
    prompt = build_routing_prompt()
    # ШАГ 1: типы документа
    assert "lab_result" in prompt
    assert "consultation" in prompt
    assert "instrumental" in prompt
    assert "discharge" in prompt
    # ШАГ 2: правила по типу
    assert "ТОЛЬКО автор выписки" in prompt
    # D3 (2026-05-30): лазейка «+1 смежный» на выписке убрана — на
    # consultation подключается строго автор, без смежных врачей.
    assert "Никаких смежных врачей" in prompt
    assert "максимум один смежный" not in prompt
    assert "лаборант ВСЕГДА" in prompt
    # Принцип «недобор лучше перебора»
    assert "недобор лучше перебора" in prompt
    # JSON-схема всё ещё
    assert "JSON" in prompt


def test_routing_prompt_returns_json_schema():
    prompt = build_routing_prompt()
    assert "document_type" in prompt
    assert "specialists" in prompt
    assert "reasoning" in prompt
