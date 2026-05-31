"""Аудит E3 + баг 2026-05-16: жёсткое обрезание текста.

Раньше в run_multi_agent документ резался до 2000–3000 знаков, история —
до 3000. 8-страничный PDF (≈18 000 знаков) фактически анализировался по
первой странице, и модель ложно просила «пришлите скан получше».

Теперь лимиты большие и настраиваемые; _clip отдаёт полный текст, если
он короче лимита.
"""

import importlib
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import agents
from agents import DOC_TEXT_LIMIT, HISTORY_LIMIT, _clip


def test_limits_are_generous_for_multipage_docs():
    # 8-страничная выписка ≈ 18 000 знаков должна проходить целиком.
    assert DOC_TEXT_LIMIT >= 30000
    assert HISTORY_LIMIT >= 12000


def test_clip_returns_full_text_when_under_limit():
    text = "А" * 18000
    assert _clip(text, DOC_TEXT_LIMIT) == text  # 8 страниц — не режется


def test_clip_trims_only_when_over_limit():
    assert _clip("abc", 5) == "abc"
    assert _clip("abcdef", 5) == "abcde"
    assert len(_clip("Б" * 100000, DOC_TEXT_LIMIT)) == DOC_TEXT_LIMIT


def test_limits_are_env_overridable(monkeypatch):
    monkeypatch.setenv("DOC_TEXT_LIMIT", "12345")
    monkeypatch.setenv("HISTORY_LIMIT", "6789")
    importlib.reload(agents)
    try:
        assert agents.DOC_TEXT_LIMIT == 12345
        assert agents.HISTORY_LIMIT == 6789
    finally:
        monkeypatch.delenv("DOC_TEXT_LIMIT", raising=False)
        monkeypatch.delenv("HISTORY_LIMIT", raising=False)
        importlib.reload(agents)
