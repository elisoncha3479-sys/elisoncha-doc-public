"""Инкремент A: единая функция записи-в-память.

Запись per-doc + refresh профилей + (опц.) reconcile-hook вынесена из
main.py::_finalize_analysis в bot/persist.py, чтобы её звали И Telegram-
поток, И массовый загрузчик (bulk_import) — ОДНУ И ТУ ЖЕ функцию, без
параллельной копии (parity-guard, уроки багов 009/010/011).

Ключевое отличие двух вызывающих:
  - Telegram: app задан, run_reconcile=True → может стрельнуть reconcile
    с уведомлением в чат.
  - Загрузчик: app=None, run_reconcile=False → НИКАКОГО спама в чат,
    только тихая запись в память.
"""

import asyncio
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import persist


class _FakeRefreshed:
    def __init__(self, path: Path, meds_changed: bool = False):
        self.path = path
        self.meds_changed = meds_changed


@pytest.fixture
def wired(monkeypatch):
    """Подменяет все тяжёлые зависимости persist; копит вызовы."""
    calls = {"perdoc": [], "refresh": [], "reconcile": []}

    def fake_write_perdoc(analysis, raw_ocr_text="", source_filename="—"):
        calls["perdoc"].append((analysis, raw_ocr_text, source_filename))
        return [Path("specialists/cardiologist/2026-05-18.md")]

    async def fake_refresh(dirs):
        calls["refresh"].append(list(dirs))
        return [_FakeRefreshed(Path("specialists/cardiologist/profile.md"),
                               meds_changed=True)]

    async def fake_reconcile(refreshed_dirs, app, meds_changed_dirs=None):
        calls["reconcile"].append((list(refreshed_dirs), app, meds_changed_dirs))
        return None

    monkeypatch.setattr(persist, "write_perdoc_files", fake_write_perdoc)
    monkeypatch.setattr(persist, "refresh_profiles_for_specialists", fake_refresh)
    monkeypatch.setattr(persist, "maybe_run_reconcile_after_refresh", fake_reconcile)
    monkeypatch.setattr(persist, "is_degraded", lambda a: bool(a.get("_synthesis_failed")))
    return calls


def _analysis():
    return {"_routing": {"specialists": ["Кардиолог"]}, "summary": "ok"}


def test_persist_writes_perdoc_and_refreshes(wired):
    res = asyncio.run(persist.persist_analysis_to_memory(
        _analysis(), file_paths=[Path("/inbox/sub/a.pdf")],
        combined_ocr="текст", app=None, run_reconcile=False,
    ))
    assert wired["perdoc"], "write_perdoc_files должен быть вызван"
    assert wired["perdoc"][0][2] == "a.pdf"  # source_filename из первого файла
    assert wired["refresh"] == [["cardiologist"]]
    assert res["engaged_dirs"] == ["cardiologist"]
    assert res["skipped_degraded"] is False


def test_loader_path_never_touches_chat(wired):
    """app=None / run_reconcile=False — reconcile (а с ним уведомление
    в чат) НЕ запускается. Это и есть «без спама в чат» для загрузчика."""
    asyncio.run(persist.persist_analysis_to_memory(
        _analysis(), file_paths=[Path("/inbox/a.pdf")],
        combined_ocr="t", app=None, run_reconcile=False,
    ))
    assert wired["reconcile"] == [], "загрузчик не должен звать reconcile/чат"


def test_telegram_path_runs_reconcile(wired):
    """app задан + run_reconcile=True — reconcile-hook вызывается
    (Telegram-поведение сохранено, parity не сломан)."""
    sentinel_app = object()
    asyncio.run(persist.persist_analysis_to_memory(
        _analysis(), file_paths=[Path("/inbox/a.pdf")],
        combined_ocr="t", app=sentinel_app, run_reconcile=True,
    ))
    assert len(wired["reconcile"]) == 1
    refreshed_dirs, app, meds = wired["reconcile"][0]
    assert app is sentinel_app
    assert refreshed_dirs == ["cardiologist"]


def test_degraded_analysis_skips_memory(wired):
    """Деградированный разбор не пишется в память (аудит 2026-05-16,
    Тема 2 — долгая память не отравляется)."""
    res = asyncio.run(persist.persist_analysis_to_memory(
        {"_synthesis_failed": True}, file_paths=[Path("/x/a.pdf")],
        combined_ocr="t", app=None, run_reconcile=False,
    ))
    assert res["skipped_degraded"] is True
    assert wired["perdoc"] == []
    assert wired["refresh"] == []
    assert wired["reconcile"] == []
