"""Единый источник правды для записи готового разбора в память.

Раньше per-doc запись + refresh профилей + reconcile-hook жили inline
внутри main.py::_finalize_analysis. Чтобы массовый загрузчик
(bot/bulk_import.py) клал документы в память ТЕМ ЖЕ путём, что и
Telegram, эта часть вынесена сюда — одна функция, два вызывающих,
без параллельной копии (parity-guard, уроки багов 009/010/011).

Telegram-поток зовёт persist_analysis_to_memory(..., app=app,
run_reconcile=True): после refresh может стрельнуть reconcile с
уведомлением в чат.

Загрузчик зовёт persist_analysis_to_memory(..., app=None,
run_reconcile=False): тихая запись в память, БЕЗ доставки/уведомлений
в чат (никакого спама при заливке 50–80 документов).
"""

import logging
from pathlib import Path
from typing import Optional

from failure_modes import is_degraded
from profile_refresher import refresh_profiles_for_specialists
from profile_writer import write_perdoc_files
from reconcile import maybe_run_reconcile_after_refresh

log = logging.getLogger(__name__)


async def persist_analysis_to_memory(
    analysis: dict,
    *,
    file_paths: list,
    combined_ocr: str,
    app=None,
    run_reconcile: bool = True,
) -> dict:
    """Записывает готовый analysis в долгую память специалистов.

    Шаги (каждый изолирован try/except — падение записи памяти не
    должно ломать вызывающего):
      1. per-doc .md (write_perdoc_files)
      2. refresh profile.md задействованных специалистов
      3. (опц.) reconcile-hook — только если app задан И
         run_reconcile=True (Telegram-поведение; для загрузчика
         выключено, чтобы не слать уведомления в чат).

    Возвращает сводку: written/engaged_dirs/refreshed_dirs/
    reconcile_ran/skipped_degraded — для лога загрузчика и тестов.
    """
    result = {
        "written": [],
        "engaged_dirs": [],
        "refreshed_dirs": [],
        "reconcile_ran": False,
        "skipped_degraded": False,
    }

    # Деградированный разбор (_synthesis_failed) в долгую память не
    # пишем — иначе она отравится свежим неполным JSON (аудит
    # 2026-05-16, Тема 2).
    if is_degraded(analysis):
        log.warning(
            "Разбор деградирован: пропускаю per-doc/refresh/reconcile"
        )
        result["skipped_degraded"] = True
        return result

    engaged_dirs: list = []
    try:
        source_name = file_paths[0].name if file_paths else "—"
        written = write_perdoc_files(
            analysis,
            raw_ocr_text=combined_ocr,
            source_filename=source_name,
        )
        if written:
            log.info("Per-doc записан в %d специалистов", len(written))
            result["written"] = list(written)
            engaged_dirs = sorted({p.parent.name for p in written})
            result["engaged_dirs"] = engaged_dirs
    except Exception as e:
        log.error("Запись per-doc упала (не критично): %s", e)

    refreshed_dirs: list[str] = []
    meds_changed_dirs: set[str] = set()
    if engaged_dirs:
        try:
            refreshed = await refresh_profiles_for_specialists(engaged_dirs)
            refreshed_dirs = sorted({r.path.parent.name for r in refreshed})
            meds_changed_dirs = {
                r.path.parent.name for r in refreshed if r.meds_changed
            }
            result["refreshed_dirs"] = refreshed_dirs
            log.info(
                "Refresh profile.md: %d/%d", len(refreshed), len(engaged_dirs)
            )
        except Exception as e:
            log.error("Refresh profile.md упал (не критично): %s", e)

    # Reconcile-hook шлёт уведомление в чат через app.bot — для
    # загрузчика это выключено (app=None / run_reconcile=False).
    if run_reconcile and app is not None and refreshed_dirs:
        try:
            await maybe_run_reconcile_after_refresh(
                refreshed_dirs, app, meds_changed_dirs=meds_changed_dirs
            )
            result["reconcile_ran"] = True
        except Exception as e:
            log.error("Reconcile-hook упал (не критично): %s", e)

    return result
