"""Преемственность памяти специалиста при анализе документа (2026-06-06).

Баг: при разборе нового документа специалист получал только общую историю,
но НЕ свою личную память (specialists/<slug>/profile.md), где лежат прежние
выводы и решения из чата. Из-за этого бот разбирал документ «с нуля» и заново
переоткрывал уже решённые вопросы.

Контракт после фикса:
- build_specialist_system подставляет личную память специалиста ПЕРЕД общей
  историей и помечает её как приоритетную («опирайся в первую очередь»);
- если личной памяти ещё нет — блок не подставляется (никаких заглушек);
- load_specialist_profile отдаёт содержимое profile.md по slug или '' если нет.
"""

import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import context
from agents import build_specialist_system


# ---------- build_specialist_system: подстановка личной памяти ----------

def test_specialist_gets_own_memory_marked_as_priority():
    profile = "РЕШЕНО ранее: тактика по этому вопросу выбрана, повторно не обсуждать."
    system = build_specialist_system(
        agent_prompt="Ты профильный специалист.",
        history_summary="Общая история болезни.",
        specialist_profile=profile,
    )
    assert "повторно не обсуждать" in system
    assert "НАКОПЛЕННАЯ ПАМЯТЬ" in system
    assert "в первую очередь" in system
    # личная память идёт ПЕРЕД общей историей
    assert system.index(profile) < system.index("Общая история болезни")


def test_specialist_without_memory_has_no_empty_block():
    system = build_specialist_system(
        agent_prompt="Ты профильный специалист.",
        history_summary="Общая история болезни.",
        specialist_profile="",
    )
    assert "НАКОПЛЕННАЯ ПАМЯТЬ" not in system
    assert "Общая история болезни" in system


def test_specialist_memory_is_optional_backward_compatible():
    system = build_specialist_system("Ты профильный специалист.", "История.")
    assert "Ты профильный специалист." in system


# ---------- load_specialist_profile: чтение личной тетради по slug ----------

def test_load_profile_returns_content(tmp_path, monkeypatch):
    spec = tmp_path / "nephrologist"
    spec.mkdir()
    (spec / "profile.md").write_text("ранее принятое решение по тактике", encoding="utf-8")
    monkeypatch.setattr(context, "SPECIALISTS_DIR", tmp_path)
    assert "решение по тактике" in context.load_specialist_profile("nephrologist")


def test_load_profile_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "SPECIALISTS_DIR", tmp_path)
    assert context.load_specialist_profile("cardiologist") == ""
    (tmp_path / "cardiologist").mkdir()
    assert context.load_specialist_profile("cardiologist") == ""


def test_load_profile_empty_slug_returns_empty(monkeypatch):
    assert context.load_specialist_profile("") == ""
