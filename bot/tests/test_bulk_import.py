"""Инкремент A: массовый загрузчик базы (bot/bulk_import.py).

Заливает ~50–80 личных документов (PDF/фото/сканы вперемешку, лежат
в папке с вложенными подпапками) в память специалистов ТЕМ ЖЕ
конвейером, что Telegram, но БЕЗ доставки/уведомлений в чат.

Требования (план 11.13, дизайн Инкремента A):
  - рекурсивный обход (вложенные подпапки — ок);
  - идемпотентность/возобновление: уже обработанное пропускается
    (манифест);
  - путь нейтральный (PATIENT_INBOX_DIR), не зашитый «МАМА»;
  - запись в память — через общую persist (parity), app=None,
    run_reconcile=False (никакого спама в чат);
  - деструктив (удаление сырья) только под флагом и ТОЛЬКО после
    успешного разбора; по умолчанию ничего не удаляется.
"""

import asyncio
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import bulk_import


class _Extraction:
    def __init__(self, text):
        self.text = text
        self.confidence = 0.9
        self.method = "fake"
        self.error = None


@pytest.fixture
def wired(monkeypatch):
    calls = {"persist": [], "multi": [], "hist": []}

    def fake_extract(file_path):
        return _Extraction(f"OCR<{Path(file_path).name}>")

    async def fake_multi(client, text, image_bytes=None, caption="",
                         extra_context=""):
        calls["multi"].append(text)
        return {"_routing": {"specialists": ["Кардиолог"]}, "summary": "ok"}

    async def fake_hist(report):
        calls["hist"].append(report)
        return report

    async def fake_persist(analysis, *, file_paths, combined_ocr,
                           app=None, run_reconcile=True):
        calls["persist"].append({
            "file_paths": file_paths,
            "app": app,
            "run_reconcile": run_reconcile,
        })
        return {"engaged_dirs": ["cardiologist"], "skipped_degraded": False}

    monkeypatch.setattr(bulk_import, "extract_text", fake_extract)
    monkeypatch.setattr(bulk_import, "run_multi_agent", fake_multi)
    monkeypatch.setattr(bulk_import, "historical_check", fake_hist)
    monkeypatch.setattr(bulk_import, "persist_analysis_to_memory", fake_persist)
    return calls


def _make_tree(root: Path):
    (root / "sub1").mkdir(parents=True)
    (root / "sub1" / "deep").mkdir()
    (root / "a.pdf").write_bytes(b"%PDF-")
    (root / "sub1" / "b.jpg").write_bytes(b"\xff\xd8")
    (root / "sub1" / "deep" / "c.png").write_bytes(b"\x89PNG")
    (root / "notes.txt").write_text("игнор")  # не поддержано
    (root / ".DS_Store").write_bytes(b"x")     # мусор


def test_discover_recurses_and_filters_extensions(tmp_path):
    _make_tree(tmp_path)
    found = bulk_import.discover_files(tmp_path, done=set())
    names = sorted(p.name for p in found)
    assert names == ["a.pdf", "b.jpg", "c.png"]


def test_discover_skips_already_done(tmp_path):
    _make_tree(tmp_path)
    done = {bulk_import.file_key(tmp_path / "a.pdf", tmp_path)}
    found = bulk_import.discover_files(tmp_path, done=done)
    assert sorted(p.name for p in found) == ["b.jpg", "c.png"]


def test_inbox_dir_is_neutral_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PATIENT_INBOX_DIR", str(tmp_path / "myinbox"))
    got = bulk_import.inbox_dir()
    assert got == (tmp_path / "myinbox")
    # путь не зашит на «МАМА»
    assert "МАМА" not in str(got)


def test_process_one_uses_shared_persist_without_chat(tmp_path, wired):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF-")
    res = asyncio.run(
        bulk_import.process_one(f, tmp_path, client=object())
    )
    assert res["ok"] is True
    assert wired["multi"] and wired["hist"]
    assert len(wired["persist"]) == 1
    p = wired["persist"][0]
    assert p["app"] is None              # никакого app → никакого чата
    assert p["run_reconcile"] is False   # reconcile/уведомления выключены
    assert p["file_paths"] == [f]


def test_no_delete_by_default(tmp_path, wired):
    _make_tree(tmp_path)
    manifest = tmp_path / ".bulk_import_done.json"
    asyncio.run(bulk_import.run_bulk_import(
        tmp_path, client=object(), manifest_path=manifest,
    ))
    # все исходники на месте — деструктив выключен по умолчанию
    assert (tmp_path / "a.pdf").exists()
    assert (tmp_path / "sub1" / "b.jpg").exists()
    assert (tmp_path / "sub1" / "deep" / "c.png").exists()


def test_delete_after_only_on_success(tmp_path, monkeypatch):
    (tmp_path / "good.pdf").write_bytes(b"%PDF-")
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-")
    manifest = tmp_path / ".done.json"

    def fake_extract(file_path):
        return _Extraction(f"OCR<{Path(file_path).name}>")

    async def fake_multi(client, text, image_bytes=None, caption="",
                         extra_context=""):
        if "bad.pdf" in text:
            raise RuntimeError("разбор упал")
        return {"_routing": {"specialists": ["Кардиолог"]}}

    async def fake_hist(report):
        return report

    async def fake_persist(analysis, *, file_paths, combined_ocr,
                           app=None, run_reconcile=True):
        return {"engaged_dirs": ["cardiologist"], "skipped_degraded": False}

    monkeypatch.setattr(bulk_import, "extract_text", fake_extract)
    monkeypatch.setattr(bulk_import, "run_multi_agent", fake_multi)
    monkeypatch.setattr(bulk_import, "historical_check", fake_hist)
    monkeypatch.setattr(bulk_import, "persist_analysis_to_memory", fake_persist)

    summary = asyncio.run(bulk_import.run_bulk_import(
        tmp_path, client=object(), manifest_path=manifest,
        delete_after=True,
    ))
    # успешный — удалён; упавший — СОХРАНЁН (сырьё не теряем при ошибке)
    assert not (tmp_path / "good.pdf").exists()
    assert (tmp_path / "bad.pdf").exists()
    assert summary["ok"] == 1
    assert summary["failed"] == 1


def test_resume_skips_manifest_entries(tmp_path, wired):
    _make_tree(tmp_path)
    manifest = tmp_path / ".done.json"
    # первый прогон
    asyncio.run(bulk_import.run_bulk_import(
        tmp_path, client=object(), manifest_path=manifest,
    ))
    first = len(wired["persist"])
    assert first == 3
    # второй прогон — всё уже в манифесте, ничего не обрабатываем заново
    asyncio.run(bulk_import.run_bulk_import(
        tmp_path, client=object(), manifest_path=manifest,
    ))
    assert len(wired["persist"]) == first  # без повторной обработки
