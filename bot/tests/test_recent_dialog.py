"""Тесты на bot/recent_dialog.py — окно последних реплик чата."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest


def _reload(monkeypatch, **env):
    """Перечитать модуль с заданными env (значения подхватываются на
    импорте через os.environ.get)."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    import recent_dialog
    return importlib.reload(recent_dialog)


def _write_chat(dir: Path, ts: str, user: str, assistant: str,
                prefix: str = "Вопрос") -> Path:
    p = dir / f"{ts}_chat.txt"
    p.write_text(f"{prefix}: {user}\n\nОтвет: {assistant}", encoding="utf-8")
    return p


# ---------- парсинг формата реплик ----------

def test_parses_operator_format(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    _write_chat(tmp_path, "2026-05-30_120000", "Что у мамы по почкам?",
                "По последним анализам всё стабильно.")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.load_recent_turns(tmp_path, now=now)
    assert out == [
        {"role": "user", "content": "Что у мамы по почкам?"},
        {"role": "assistant", "content": "По последним анализам всё стабильно."},
    ]


def test_parses_patient_format_with_intent(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    p = tmp_path / "2026-05-30_120000_chat.txt"
    p.write_text(
        "Сообщение мамы (clinical): Давление 140/90, болит затылок\n\n"
        "Ответ: Зафиксировали у кардиолога.",
        encoding="utf-8",
    )
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.load_recent_turns(tmp_path, now=now)
    assert out[0]["content"] == "Давление 140/90, болит затылок"
    assert out[1]["content"] == "Зафиксировали у кардиолога."


# ---------- окно 8 часов ----------

def test_files_outside_window_are_skipped(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true",
                 RECENT_DIALOG_WINDOW_HOURS="8")
    # 9 часов назад — должно отрезать
    _write_chat(tmp_path, "2026-05-30_040000", "старый вопрос", "старый ответ")
    # 1 час назад — должно остаться
    _write_chat(tmp_path, "2026-05-30_120000", "свежий вопрос", "свежий ответ")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.load_recent_turns(tmp_path, now=now)
    assert len(out) == 2
    assert out[0]["content"] == "свежий вопрос"


def test_files_at_window_edge_included(tmp_path, monkeypatch):
    """Граница окна — 8 часов ровно — включается (>=, не >)."""
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true",
                 RECENT_DIALOG_WINDOW_HOURS="8")
    _write_chat(tmp_path, "2026-05-30_050000", "на самой границе", "ответ")
    now = datetime(2026, 5, 30, 13, 0, 0)  # ровно 8 часов
    out = rd.load_recent_turns(tmp_path, now=now)
    assert len(out) == 2


# ---------- лимит 10 реплик ----------

def test_limit_to_max_turns_keeps_latest(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true",
                 RECENT_DIALOG_WINDOW_HOURS="8",
                 RECENT_DIALOG_MAX_TURNS="3")
    # 5 турнов подряд (все в окне)
    for i, ts in enumerate([
        "2026-05-30_120000", "2026-05-30_120100", "2026-05-30_120200",
        "2026-05-30_120300", "2026-05-30_120400",
    ]):
        _write_chat(tmp_path, ts, f"вопрос {i}", f"ответ {i}")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.load_recent_turns(tmp_path, now=now)
    # 3 турна = 6 messages, последние 3 (i=2,3,4)
    assert len(out) == 6
    assert out[0]["content"] == "вопрос 2"
    assert out[-1]["content"] == "ответ 4"


# ---------- хронологическая сортировка ----------

def test_chronological_order(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    # пишем в обратном порядке намеренно
    _write_chat(tmp_path, "2026-05-30_120300", "поздний", "ответ-поздний")
    _write_chat(tmp_path, "2026-05-30_120100", "ранний", "ответ-ранний")
    _write_chat(tmp_path, "2026-05-30_120200", "средний", "ответ-средний")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.load_recent_turns(tmp_path, now=now)
    contents = [m["content"] for m in out]
    assert contents == [
        "ранний", "ответ-ранний",
        "средний", "ответ-средний",
        "поздний", "ответ-поздний",
    ]


# ---------- толерантность к мусору ----------

def test_malformed_files_skipped_without_raise(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    # кривой файл — нет «Вопрос:» / нет «Ответ:»
    (tmp_path / "2026-05-30_120000_chat.txt").write_text(
        "просто текст без формата", encoding="utf-8")
    # нормальный файл — должен пройти
    _write_chat(tmp_path, "2026-05-30_120100", "норм", "ок")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.load_recent_turns(tmp_path, now=now)
    assert out == [
        {"role": "user", "content": "норм"},
        {"role": "assistant", "content": "ок"},
    ]


def test_non_chat_files_ignored(tmp_path, monkeypatch):
    """В history/ живут и другие файлы (*_analysis.json, *_photo_analysis.txt) —
    их не должно засосать."""
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    (tmp_path / "2026-05-30_120000_analysis.json").write_text("{}",
                                                              encoding="utf-8")
    (tmp_path / "2026-05-30_120100_photo_analysis.txt").write_text("blah",
                                                                   encoding="utf-8")
    _write_chat(tmp_path, "2026-05-30_120200", "норм", "ок")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.load_recent_turns(tmp_path, now=now)
    assert len(out) == 2  # только один chat-файл


# ---------- kill-switch ----------

def test_disabled_returns_empty(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="false")
    _write_chat(tmp_path, "2026-05-30_120000", "вопрос", "ответ")
    now = datetime(2026, 5, 30, 13, 0, 0)
    assert rd.load_recent_turns(tmp_path, now=now) == []


# ---------- messages_with_recent: текущая реплика добавляется в конец ----------

def test_messages_with_recent_appends_current(tmp_path, monkeypatch):
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    _write_chat(tmp_path, "2026-05-30_120000", "первое", "ответ1")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.messages_with_recent("новый вопрос", tmp_path, now=now)
    assert out[-1] == {"role": "user", "content": "новый вопрос"}
    assert out[0]["content"] == "первое"


def test_messages_with_recent_no_history(tmp_path, monkeypatch):
    """Когда истории нет — возвращается только текущая реплика."""
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    now = datetime(2026, 5, 30, 13, 0, 0)
    out = rd.messages_with_recent("первый вопрос", tmp_path, now=now)
    assert out == [{"role": "user", "content": "первый вопрос"}]


# ---------- D2: память разговора только в chat-точках, не в пайплайне ----------

_BOT_DIR = Path(__file__).resolve().parent.parent


def test_d2_pipeline_does_not_mix_recent_dialog():
    """D2 (2026-05-30): медпайплайн (routing/специалист/synthesis в
    agents.py) НЕ подмешивает recent_dialog — истории чата ему не нужно,
    а размер запроса критичен для watchdog. Защита от регресса."""
    src = (_BOT_DIR / "agents.py").read_text(encoding="utf-8")
    assert "messages_with_recent" not in src


def test_d2_chat_points_keep_recent_dialog():
    """А вот chat-точки (patient_message.py) рабочую память сохраняют."""
    src = (_BOT_DIR / "patient_message.py").read_text(encoding="utf-8")
    assert "messages_with_recent" in src


# ---------- Документный ход читается памятью разговора (фикс 2026-05-30) ----------

def test_document_turn_is_visible_to_recent_dialog(tmp_path, monkeypatch):
    """Parity-guard писатель↔парсер: формат, которым main.py пишет
    документный ход в *_chat.txt после отправки PDF, должен корректно
    разбираться recent_dialog. Иначе follow-up снова перестанет видеть
    разбор (живой тест Алисы 2026-05-30).

    Формат-эталон должен совпадать с веткой 1b в bot/main.py
    (_finalize_analysis)."""
    rd = _reload(monkeypatch, RECENT_DIALOG_ENABLED="true")
    summary = "# Анализ крови\n\n**Главное:** гемоглобин в норме"
    (tmp_path / "2026-05-30_120000_chat.txt").write_text(
        "Сообщение мамы (документ): прислала медицинский документ на разбор\n\n"
        f"Ответ: {summary}",
        encoding="utf-8",
    )
    now = datetime(2026, 5, 30, 13, 0, 0)
    turns = rd.load_recent_turns(tmp_path, now=now)
    assert turns == [
        {"role": "user", "content": "прислала медицинский документ на разбор"},
        {"role": "assistant", "content": summary},
    ]


def test_main_writes_document_turn_to_chat_history():
    """main.py действительно пишет документный ход в *_chat.txt после PDF —
    в формате, который читает recent_dialog (см. тест выше)."""
    src = (_BOT_DIR / "main.py").read_text(encoding="utf-8")
    assert "Сообщение мамы (документ):" in src
    assert "build_report_text_summary(analysis)" in src
