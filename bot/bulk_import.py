"""Массовый загрузчик базы документов в память специалистов.

Заливает большую папку личных документов (PDF/фото/сканы, вложенные
подпапки — ок) тем же конвейером, что и Telegram-поток, но БЕЗ
доставки PDF и БЕЗ уведомлений в чат:

    extract_text → run_multi_agent → historical_check
                 → persist_analysis_to_memory(app=None,
                                               run_reconcile=False)

Запись в память идёт через ту же общую persist, что и Telegram —
один источник правды, без параллельной копии (parity-guard, уроки
багов 009/010/011).

Свойства:
  - рекурсивный обход папки (вложенные подпапки);
  - идемпотентность и возобновление с места обрыва через
    JSON-манифест обработанных файлов;
  - путь нейтральный (env PATIENT_INBOX_DIR) — никакой зашитой
    «МАМА»;
  - деструктив (удаление сырья) ТОЛЬКО под флагом --delete-after и
    ТОЛЬКО после успешного разбора файла; по умолчанию ничего не
    удаляется, оригиналы остаются.

Запуск:
    python bulk_import.py [--inbox PATH] [--manifest PATH]
                          [--delete-after] [--limit N]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Bulk-режим: дёшево по умолчанию. Переменные подхватываются
# модулями bot/agents.py, bot/historical_check.py и
# bot/profile_refresher.py в момент их импорта НИЖЕ. Если кто-то
# уже задал переменную через docker -e или shell — setdefault её
# не трогает. Интерактивный путь (bot/main.py) этот файл не
# импортирует, его дефолты остаются на Sonnet.
os.environ.setdefault("AGENTS_MODEL", "claude-haiku-4-5-20251001")
os.environ.setdefault("HISTORICAL_CHECK_MODEL", "claude-haiku-4-5-20251001")
os.environ.setdefault("PROFILE_REFRESHER_MODEL", "claude-haiku-4-5-20251001")

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

from agents import run_multi_agent
from historical_check import historical_check
from ocr import extract_text
from persist import persist_analysis_to_memory

load_dotenv()

log = logging.getLogger("bulk_import")

SUPPORTED = {".jpg", ".jpeg", ".png", ".heic", ".pdf", ".docx"}

# Нейтральный дефолт — НЕ зашитый «inbox/МАМА» (план 11.13 п.4).
DEFAULT_INBOX = Path(__file__).resolve().parent.parent / "patient_inbox"


def inbox_dir() -> Path:
    """Папка с документами. Берётся из env PATIENT_INBOX_DIR,
    дефолт — нейтральный patient_inbox/ (не «МАМА»)."""
    raw = os.environ.get("PATIENT_INBOX_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_INBOX


def file_key(path: Path, root: Path) -> str:
    """Стабильный ключ файла для манифеста — путь относительно root."""
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def load_manifest(manifest_path: Path) -> set[str]:
    """Множество уже обработанных ключей. Битый/отсутствующий
    манифест трактуем как пустой (возобновление не должно падать)."""
    if not manifest_path.exists():
        return set()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return set(data.get("done", []))
    except Exception as e:
        log.warning("Манифест нечитаем (%s) — считаю пустым", e)
        return set()


def append_manifest(manifest_path: Path, key: str) -> None:
    """Дозаписать ключ в манифест сразу после успеха файла —
    чтобы обрыв на любом шаге не терял прогресс."""
    done = load_manifest(manifest_path)
    done.add(key)
    manifest_path.write_text(
        json.dumps({"done": sorted(done)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def discover_files(root: Path, done: set[str]) -> list[Path]:
    """Рекурсивно собрать поддерживаемые файлы, пропуская уже
    обработанные (по манифесту)."""
    if not root.exists():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        if file_key(path, root) in done:
            continue
        files.append(path)
    return files


async def process_one(path: Path, root: Path, *, client) -> dict:
    """Прогнать один файл через конвейер и записать в память.

    Возвращает {"path","ok","engaged_dirs"|"error"}. Исключения
    конвейера НЕ глотает молча — пробрасывает наверх, чтобы
    run_bulk_import пометил файл failed и НЕ удалял сырьё."""
    extraction = extract_text(str(path))
    text = (extraction.text or "").strip()
    if not text:
        log.warning("Пустой OCR: %s — пропуск", path.name)
        return {"path": path, "ok": False, "error": "empty_ocr"}

    analysis = await run_multi_agent(client, text, caption="")
    analysis = await historical_check(analysis)

    pres = await persist_analysis_to_memory(
        analysis,
        file_paths=[path],
        combined_ocr=text,
        app=None,            # нет app → никакой доставки/чата
        run_reconcile=False,  # reconcile/уведомления выключены
    )
    return {
        "path": path,
        "ok": True,
        "engaged_dirs": pres.get("engaged_dirs", []),
        "skipped_degraded": pres.get("skipped_degraded", False),
    }


async def run_bulk_import(
    root: Path | None = None,
    *,
    client,
    manifest_path: Path | None = None,
    delete_after: bool = False,
    limit: int | None = None,
) -> dict:
    """Обойти папку и залить документы в память пачкой.

    delete_after=True удаляет исходник ТОЛЬКО после успешного
    разбора (на ошибке файл остаётся — сырьё не теряем).
    """
    root = (root or inbox_dir()).resolve()
    manifest_path = manifest_path or (root / ".bulk_import_done.json")

    done = load_manifest(manifest_path)
    files = discover_files(root, done)
    if limit is not None:
        files = files[:limit]

    summary = {"total": len(files), "ok": 0, "failed": 0, "deleted": 0}
    log.info("Загрузчик: %d файлов к обработке в %s", len(files), root)

    for idx, path in enumerate(files, 1):
        key = file_key(path, root)
        log.info("[%d/%d] %s", idx, len(files), key)
        try:
            res = await process_one(path, root, client=client)
        except Exception as e:
            summary["failed"] += 1
            log.error("[%d/%d] %s — ОШИБКА: %s", idx, len(files), key, e,
                      exc_info=True)
            continue

        if not res["ok"]:
            summary["failed"] += 1
            log.error("[%d/%d] %s — не обработан: %s", idx, len(files),
                      key, res.get("error"))
            continue

        summary["ok"] += 1
        append_manifest(manifest_path, key)
        log.info("[%d/%d] %s — ✓ %s", idx, len(files), key,
                 res.get("engaged_dirs"))

        if delete_after:
            try:
                path.unlink()
                summary["deleted"] += 1
                log.info("Удалён исходник (после успеха): %s", key)
            except Exception as e:
                log.error("Не смог удалить %s: %s", key, e)

    log.info(
        "Готово: всего=%d ок=%d ошибок=%d удалено=%d",
        summary["total"], summary["ok"], summary["failed"],
        summary["deleted"],
    )
    return summary


def _build_client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY не задан в окружении")
    return anthropic.Anthropic(api_key=key)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Массовый загрузчик базы")
    parser.add_argument("--inbox", type=Path, default=None,
                        help="папка с документами (иначе PATIENT_INBOX_DIR)")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="путь к манифесту обработанных")
    parser.add_argument("--delete-after", action="store_true",
                        help="удалять исходник ПОСЛЕ успешного разбора")
    parser.add_argument("--limit", type=int, default=None,
                        help="обработать не более N файлов (пачками)")
    args = parser.parse_args(argv)

    root = args.inbox or inbox_dir()
    summary = asyncio.run(run_bulk_import(
        root,
        client=_build_client(),
        manifest_path=args.manifest,
        delete_after=args.delete_after,
        limit=args.limit,
    ))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
