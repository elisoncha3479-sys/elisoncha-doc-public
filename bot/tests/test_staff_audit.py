"""
Тесты для bot/staff_audit.py — чистая часть (без LLM-вызовов).

LLM-функция propose_new_specialist() покрывается интеграционным тестом
позже в A3 (там, где встройка в digest и approve-flow в Telegram).
"""
import textwrap
from pathlib import Path

import pytest

from staff_audit import (
    EXCLUDED_DIR_NAMES,
    SPECIALISTS_DIR,
    approve_draft,
    audit_and_propose,
    collect_stats,
    draft_path,
    final_path,
    find_shadow_specialists,
    format_proposal_message,
    read_draft,
    reject_draft,
    save_draft,
)


# ---------- helpers ----------

def _make_specialist_dir(parent: Path, name: str, doc_count: int) -> Path:
    d = parent / name
    d.mkdir()
    (d / "profile.md").write_text("# profile\n", encoding="utf-8")
    for i in range(doc_count):
        (d / f"2026-01-{i+1:02d}_doc.md").write_text("# doc\n", encoding="utf-8")
    return d


def _make_agent_md(parent: Path, slug: str, name_ru: str, role: str = "specialist") -> None:
    (parent / f"{slug}.md").write_text(
        textwrap.dedent(f"""\
            ---
            slug: {slug}
            name_ru: {name_ru}
            role: {role}
            ---
            # body
        """),
        encoding="utf-8",
    )


@pytest.fixture
def tmp_dirs(tmp_path):
    specialists = tmp_path / "specialists"
    agents = tmp_path / "agents"
    specialists.mkdir()
    agents.mkdir()
    return specialists, agents


# ---------- collect_stats ----------

def test_collect_stats_returns_registered_specialist(tmp_dirs):
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "cardiologist", 5)
    _make_agent_md(agents, "cardiologist", "кардиолог")

    stats = collect_stats(specialists, agents)
    assert stats["cardiologist"]["total"] == 5
    assert stats["cardiologist"]["has_agent_md"] is True
    assert stats["cardiologist"]["last_doc_date"] is not None


def test_collect_stats_marks_shadow_when_no_agent_md(tmp_dirs):
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "neurologist", 16)
    _make_agent_md(agents, "cardiologist", "кардиолог")  # другой агент

    stats = collect_stats(specialists, agents)
    assert stats["neurologist"]["has_agent_md"] is False
    assert stats["neurologist"]["total"] == 16


def test_collect_stats_excludes_underscore_dirs(tmp_dirs):
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "_excluded", 4)
    _make_specialist_dir(specialists, "cardiologist", 1)
    _make_agent_md(agents, "cardiologist", "кардиолог")

    stats = collect_stats(specialists, agents)
    assert "_excluded" not in stats
    assert "cardiologist" in stats


def test_collect_stats_excludes_uncategorized(tmp_dirs):
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "uncategorized", 4)
    _make_specialist_dir(specialists, "cardiologist", 1)
    _make_agent_md(agents, "cardiologist", "кардиолог")

    stats = collect_stats(specialists, agents)
    assert "uncategorized" not in stats


def test_collect_stats_ignores_profile_md(tmp_dirs):
    """profile.md не считается per-doc — он сводный документ специалиста."""
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "cardiologist", 0)  # только profile.md
    _make_agent_md(agents, "cardiologist", "кардиолог")

    stats = collect_stats(specialists, agents)
    assert stats["cardiologist"]["total"] == 0


def test_collect_stats_returns_empty_when_dir_missing(tmp_path):
    """specialists/ отсутствует — не падаем, возвращаем {}."""
    agents = tmp_path / "agents"
    agents.mkdir()
    stats = collect_stats(tmp_path / "nope", agents)
    assert stats == {}


# ---------- find_shadow_specialists ----------

def test_find_shadow_returns_only_unregistered(tmp_dirs):
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "neurologist", 16)
    _make_specialist_dir(specialists, "cardiologist", 30)
    _make_agent_md(agents, "cardiologist", "кардиолог")

    stats = collect_stats(specialists, agents)
    shadow = find_shadow_specialists(stats)
    assert shadow == ["neurologist"]


def test_find_shadow_respects_min_docs(tmp_dirs):
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "newcomer", 2)

    stats = collect_stats(specialists, agents)
    assert find_shadow_specialists(stats, min_docs=3) == []
    assert find_shadow_specialists(stats, min_docs=2) == ["newcomer"]


def test_find_shadow_sorts_by_total_desc(tmp_dirs):
    specialists, agents = tmp_dirs
    _make_specialist_dir(specialists, "small", 4)
    _make_specialist_dir(specialists, "big", 30)
    _make_specialist_dir(specialists, "medium", 10)

    stats = collect_stats(specialists, agents)
    shadow = find_shadow_specialists(stats)
    assert shadow == ["big", "medium", "small"]


# ---------- excluded names constant ----------

def test_excluded_dir_names_contains_uncategorized():
    assert "uncategorized" in EXCLUDED_DIR_NAMES


# ---------- реальная директория проекта ----------

def test_real_specialists_dir_neurologist_is_registered():
    """Каркас specialists/neurologist/ есть и привязан к agents/neurologist.md
    — папка не теневая (для каждого специалиста из реестра есть папка)."""
    stats = collect_stats()
    assert "neurologist" in stats
    assert stats["neurologist"]["has_agent_md"] is True
    shadow = find_shadow_specialists(stats)
    assert "neurologist" not in shadow


# ---------- Draft I/O ----------

def test_save_and_read_draft(tmp_path):
    path = save_draft("neurologist", "---\nslug: neurologist\n---\n# Невролог\n", tmp_path)
    assert path.name == "neurologist.md.draft"
    assert read_draft("neurologist", tmp_path) == "---\nslug: neurologist\n---\n# Невролог\n"


def test_read_draft_returns_none_if_missing(tmp_path):
    assert read_draft("nope", tmp_path) is None


def test_approve_draft_renames_atomically(tmp_path):
    save_draft("neurologist", "# body", tmp_path)
    dst = approve_draft("neurologist", tmp_path)
    assert dst == final_path("neurologist", tmp_path)
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "# body"
    assert not draft_path("neurologist", tmp_path).exists()


def test_approve_draft_refuses_when_md_exists(tmp_path):
    save_draft("neurologist", "draft body", tmp_path)
    final_path("neurologist", tmp_path).write_text("existing body")
    with pytest.raises(FileExistsError):
        approve_draft("neurologist", tmp_path)
    # Никаких silent merge — оба файла остались
    assert draft_path("neurologist", tmp_path).exists()
    assert final_path("neurologist", tmp_path).read_text() == "existing body"


def test_approve_draft_raises_when_no_draft(tmp_path):
    with pytest.raises(FileNotFoundError):
        approve_draft("nope", tmp_path)


def test_reject_draft_removes_file(tmp_path):
    save_draft("neurologist", "draft", tmp_path)
    assert reject_draft("neurologist", tmp_path) is True
    assert not draft_path("neurologist", tmp_path).exists()


def test_reject_draft_returns_false_when_missing(tmp_path):
    assert reject_draft("nope", tmp_path) is False


# ---------- audit_and_propose с моком LLM ----------

def _make_minimal_proj(tmp_path):
    specialists = tmp_path / "specialists"
    agents = tmp_path / "agents"
    specialists.mkdir()
    agents.mkdir()
    return specialists, agents


def test_audit_and_propose_no_shadow(tmp_path):
    specialists, agents = _make_minimal_proj(tmp_path)
    _make_specialist_dir(specialists, "cardiologist", 5)
    _make_agent_md(agents, "cardiologist", "кардиолог")

    result = audit_and_propose(
        specialists_dir=specialists,
        agents_dir=agents,
        propose_fn=lambda *a, **kw: pytest.fail("LLM не должна вызываться"),
    )
    assert result is None


def test_audit_and_propose_creates_draft_when_needed(tmp_path):
    specialists, agents = _make_minimal_proj(tmp_path)
    _make_specialist_dir(specialists, "neurologist", 16)

    def fake_propose(slug, stats, client=None):
        return {
            "needed": True,
            "slug": "neurologist",
            "name_ru": "невролог",
            "reasoning": "16 неврологических документов накопилось",
            "doc_count": 16,
            "last_doc_date": "2026-04-04",
            "draft_agent_md": "---\nslug: neurologist\nname_ru: невролог\nrole: specialist\n---\n# Невролог\n",
        }

    result = audit_and_propose(
        specialists_dir=specialists, agents_dir=agents, propose_fn=fake_propose,
    )
    assert result is not None
    assert result["slug"] == "neurologist"
    assert result["draft_path"].exists()
    assert result["draft_path"].read_text(encoding="utf-8").startswith("---")


def test_audit_and_propose_returns_none_when_not_needed(tmp_path):
    specialists, agents = _make_minimal_proj(tmp_path)
    _make_specialist_dir(specialists, "weird", 5)

    def fake_propose(slug, stats, client=None):
        return {"needed": False, "reasoning": "явно покрывается лаборантом"}

    result = audit_and_propose(
        specialists_dir=specialists, agents_dir=agents, propose_fn=fake_propose,
    )
    assert result is None
    assert not draft_path("weird", agents).exists()


def test_audit_and_propose_skips_when_draft_exists(tmp_path):
    specialists, agents = _make_minimal_proj(tmp_path)
    _make_specialist_dir(specialists, "neurologist", 16)
    save_draft("neurologist", "# существующий draft", agents)

    result = audit_and_propose(
        specialists_dir=specialists,
        agents_dir=agents,
        propose_fn=lambda *a, **kw: pytest.fail("Не должны звать LLM, draft уже есть"),
    )
    assert result is None


def test_format_proposal_message_includes_commands():
    msg = format_proposal_message({
        "slug": "neurologist",
        "name_ru": "невролог",
        "reasoning": "тест",
        "doc_count": 16,
        "last_doc_date": "2026-04-04",
    })
    assert "/show_draft neurologist" in msg
    assert "/approve_specialist neurologist" in msg
    assert "/reject_specialist neurologist" in msg
    assert "невролог" in msg
