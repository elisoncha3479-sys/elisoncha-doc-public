"""Backlog #2: profile_refresher / profile_writer / build_specialist_memory
переведены с захардкоженных dict'ов на динамический реестр discover_specialists().

Регрессия: следующий approve'нутый специалист (новый agents/<slug>.md) должен
автоматически попадать в map для записи per-doc и пересборки profile.md — без
правки Python. Раньше с неврологом сработало случайно (была предусмотрительная
строка в _DIR_TO_AGENT).
"""

import textwrap
from pathlib import Path

import pytest

from agents import discover_specialists, resolve_specialist_slug


SPECIALIST_TEMPLATE = textwrap.dedent("""\
    ---
    slug: {slug}
    name_ru: {name_ru}
    role: specialist
    domain: тестовый домен
    ---

    # {name_ru}
    тело
""")


@pytest.fixture
def agents_dir_with_new_specialist(tmp_path: Path) -> Path:
    """Каталог agents/ с одним «старым» и одним «только что approve'нутым»
    специалистом, которого нет ни в одном хардкоженном словаре."""
    d = tmp_path / "agents"
    d.mkdir()
    (d / "cardiologist.md").write_text(
        SPECIALIST_TEMPLATE.format(slug="cardiologist", name_ru="кардиолог"),
        encoding="utf-8",
    )
    (d / "pulmonologist.md").write_text(
        SPECIALIST_TEMPLATE.format(slug="pulmonologist", name_ru="пульмонолог"),
        encoding="utf-8",
    )
    return d


def test_discover_picks_up_new_specialist(agents_dir_with_new_specialist):
    reg = discover_specialists(agents_dir_with_new_specialist)
    assert reg == {"кардиолог": "cardiologist", "пульмонолог": "pulmonologist"}


# ---------- profile_writer: name_ru → dir из реестра ----------

def test_profile_writer_dir_map_is_dynamic(agents_dir_with_new_specialist):
    """profile_writer не должен иметь захардкоженного _SPECIALIST_DIR_MAP;
    name_ru→slug берётся из discover_specialists()."""
    import profile_writer

    assert not hasattr(profile_writer, "_SPECIALIST_DIR_MAP"), (
        "захардкоженный _SPECIALIST_DIR_MAP должен быть удалён"
    )
    dir_map = profile_writer._specialist_dir_map(agents_dir_with_new_specialist)
    assert dir_map["пульмонолог"] == "pulmonologist"


# ---------- profile_refresher: валидность папки из реестра ----------

def test_profile_refresher_known_slugs_are_dynamic(agents_dir_with_new_specialist):
    """profile_refresher не должен иметь захардкоженного _DIR_TO_AGENT;
    набор валидных slug'ов берётся из реестра."""
    import profile_refresher

    assert not hasattr(profile_refresher, "_DIR_TO_AGENT"), (
        "захардкоженный _DIR_TO_AGENT должен быть удалён"
    )
    slugs = profile_refresher._known_specialist_slugs(agents_dir_with_new_specialist)
    assert "pulmonologist" in slugs
    assert "cardiologist" in slugs


# ---------- build_specialist_memory: SPECIALIST_MAP из реестра ----------

def test_build_specialist_memory_map_matches_registry():
    import build_specialist_memory

    assert build_specialist_memory.SPECIALIST_MAP == discover_specialists()


# ---------- historical_check: name_ru → dir из реестра ----------

def test_historical_check_dir_map_is_dynamic():
    """historical_check не должен иметь захардкоженного _SPECIALIST_DIR_MAP;
    name_ru→slug берётся из реестра. Резолв в _relevant_specialist_dirs."""
    import historical_check

    assert not hasattr(historical_check, "_SPECIALIST_DIR_MAP"), (
        "захардкоженный _SPECIALIST_DIR_MAP должен быть удалён"
    )
    reg = discover_specialists()
    a_name_ru = next(iter(reg))
    report = {"_routing": {"specialists": [a_name_ru]}}
    assert historical_check._relevant_specialist_dirs(report) == [reg[a_name_ru]]


def test_build_specialist_memory_routing_prompt_lists_all_specialists():
    """В ROUTING_PROMPT перечислены все name_ru из реестра — без ручного
    дублирования списка."""
    import build_specialist_memory

    for name_ru in discover_specialists().keys():
        assert name_ru in build_specialist_memory.ROUTING_PROMPT, (
            f"{name_ru!r} отсутствует в ROUTING_PROMPT"
        )


# ---------- resolve_specialist_slug: толерантность к slug/имени ----------
#
# Бэкграунд: модель иногда возвращает slug ('dental-gnathologist') вместо
# русского имени ('стоматолог-гнатолог') в routing.specialists для
# дефисных названий. Без толерантного резолва consult_specialist и
# profile_writer молча пропускали такого специалиста.

def test_resolve_specialist_slug_accepts_name_ru():
    spec_map = {"кардиолог": "cardiologist", "стоматолог-гнатолог": "dental-gnathologist"}
    assert resolve_specialist_slug("кардиолог", spec_map) == "cardiologist"
    assert resolve_specialist_slug("стоматолог-гнатолог", spec_map) == "dental-gnathologist"


def test_resolve_specialist_slug_accepts_slug_fallback():
    """Главный кейс: модель сорвалась и вернула slug — резолвим в тот же slug."""
    spec_map = {"кардиолог": "cardiologist", "стоматолог-гнатолог": "dental-gnathologist"}
    assert resolve_specialist_slug("dental-gnathologist", spec_map) == "dental-gnathologist"
    assert resolve_specialist_slug("cardiologist", spec_map) == "cardiologist"


def test_resolve_specialist_slug_unknown_returns_none():
    spec_map = {"кардиолог": "cardiologist"}
    assert resolve_specialist_slug("марсианский-врач", spec_map) is None
    assert resolve_specialist_slug("", spec_map) is None
    assert resolve_specialist_slug(None, spec_map) is None
