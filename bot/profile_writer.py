"""
bot/profile_writer.py — после synthesis записывает per-doc .md файлы в директории
задействованных специалистов. Это «долгосрочная память» бота: profile.md
обновляется отдельной командой `commands/refresh-profile.md`, которая
консолидирует накопившиеся per-doc .md в актуальную сводку.

Каждый per-doc файл содержит:
- Шапку: заголовок, дата, источник, специалист, тип документа
- Заключение этого специалиста (из multi-agent `_opinions`)
- Сырой OCR-текст (для будущего refresh-profile, чтобы можно было пересмотреть)
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from agents import discover_specialists, resolve_specialist_slug

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent
SPECIALISTS_DIR = PROJECT_DIR / "specialists"
EXCLUDED_DIR = SPECIALISTS_DIR / "_excluded"

PATIENT_FULL_NAME = os.environ.get("PATIENT_FULL_NAME", "")
PATIENT_DOB = os.environ.get("PATIENT_DOB", "")

# Маппинг русских названий из routing → имена директорий специалистов.
# Берётся из динамического реестра agents/*.md (backlog #2), а не из
# захардкоженного словаря: следующий approve'нутый специалист подхватывается
# без правки Python. Резолвится при каждом вызове write_perdoc_files —
# approve срабатывает без перезапуска бота.
def _specialist_dir_map(agents_dir=None) -> dict:
    return discover_specialists(agents_dir)


def _slugify_title(title: str, max_len: int = 50) -> str:
    """Приводит заголовок к safe-имени файла: убирает лишние знаки, обрезает.

    Заголовок сначала нормализуется в NFC: macOS APFS отдаёт строки в NFD
    («й» = «и» + U+0306), и без нормализации re.sub по \\w выбрасывает
    комбинирующие знаки, плодя NFD/NFC-дубли при rsync VPS→mac.
    """
    if not title:
        return "документ"
    title = unicodedata.normalize("NFC", title)
    # Заменим всё, что не буква/цифра/пробел/дефис, на пробел
    s = re.sub(r"[^\w\s\-]", " ", title, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "документ"


def _format_perdoc_content(
    title: str,
    subtitle: str,
    doc_date: str,
    source_filename: str,
    specialist_ru: str,
    document_type: str,
    opinion: str,
    raw_ocr_text: str,
) -> str:
    """Собирает полный markdown-текст per-doc файла."""
    parts = [
        f"# {title}",
        "",
    ]
    if subtitle:
        parts.append(f"_{subtitle}_")
        parts.append("")
    parts.extend([
        f"**Дата:** {doc_date}",
        f"**Источник:** {source_filename}",
        f"**Специалист:** {specialist_ru}",
        f"**Тип документа:** {document_type or '—'}",
        "",
        "---",
        "",
        "## Заключение специалиста",
        "",
        opinion.strip() if opinion else "_(заключение не предоставлено)_",
        "",
        "---",
        "",
        "## Исходный распознанный текст",
        "",
        "```",
        raw_ocr_text.strip()[:8000] if raw_ocr_text else "(OCR-текст недоступен)",
        "```",
        "",
    ])
    return "\n".join(parts)


def _write_excluded(
    analysis: dict,
    raw_ocr_text: str,
    source_filename: str,
    reason: str,
) -> Optional[Path]:
    try:
        EXCLUDED_DIR.mkdir(parents=True, exist_ok=True)
        title = analysis.get("title") or "Документ другого пациента"
        doc_date = _extract_doc_date(analysis)
        slug = _slugify_title(f"mismatch_{title}")
        fname = f"{doc_date}_{slug}.md"
        path = EXCLUDED_DIR / fname
        if path.exists():
            stem = path.stem
            suffix = path.suffix
            i = 2
            while (EXCLUDED_DIR / f"{stem}_v{i}{suffix}").exists():
                i += 1
            path = EXCLUDED_DIR / f"{stem}_v{i}{suffix}"
        content = (
            f"# {title}\n\n"
            f"**Дата:** {doc_date}\n"
            f"**Источник:** {source_filename}\n\n"
            f"> 🔴 **Identity gate (автоматический, {datetime.now().strftime('%Y-%m-%d %H:%M')}):** "
            f"{reason}\n>\n"
            f"> Документ записан в `specialists/_excluded/` и **не включён** в profile.md "
            f"ни одного специалиста. Если это ошибка — переместите файл в нужную папку "
            f"и обновите шапку.\n\n"
            f"---\n\n"
            f"## Исходный распознанный текст\n\n"
            f"```\n{(raw_ocr_text or '(OCR-текст недоступен)')[:8000]}\n```\n"
        )
        path.write_text(content, encoding="utf-8")
        log.warning("Identity gate: per-doc записан в %s", path.relative_to(PROJECT_DIR))
        return path
    except Exception as e:
        log.error("Не записала excluded per-doc: %s", e, exc_info=False)
        return None


def record_excluded_document(
    raw_ocr_text: str,
    source_filename: str,
    reason: str,
) -> Optional[Path]:
    """Записать чужой документ в specialists/_excluded/ для ручного разбора.

    Вызывается гейтом ДО анализа (bot/main.py): клинический контекст пациента
    к документу не применялся, поэтому analysis пустой — только сырой OCR
    и причина блокировки.
    """
    if not SPECIALISTS_DIR.exists():
        log.warning("specialists/ не существует — excluded не записан")
        return None
    return _write_excluded(
        analysis={},
        raw_ocr_text=raw_ocr_text,
        source_filename=source_filename,
        reason=reason,
    )


def write_perdoc_files(
    analysis: dict,
    raw_ocr_text: str = "",
    source_filename: str = "—",
) -> List[Path]:
    """Записывает per-doc .md файлы для всех задействованных специалистов.

    Возвращает список созданных путей. Если что-то пошло не так — логируем,
    но не падаем (ошибка в записи памяти не должна ломать доставку отчёта)."""
    if not SPECIALISTS_DIR.exists():
        log.warning("specialists/ не существует — per-doc .md не записываются")
        return []

    # Identity-гейт по ФИО теперь стоит ВЫШЕ по потоку (bot/main.py, до
    # анализа). Сюда документ доходит только когда ФИО пациента подтверждено
    # ИЛИ человек явно подтвердил кнопкой «это анализ мамы» — повторно
    # отводить в _excluded здесь нельзя, иначе подтверждённый документ
    # потеряется (инцидент 013).
    routing = analysis.get("_routing") or {}
    spec_names_ru = routing.get("specialists") or []
    if not spec_names_ru:
        log.info("В routing нет специалистов — per-doc записывать некому")
        return []

    opinions = analysis.get("_opinions") or {}
    document_type = routing.get("document_type") or ""
    title = analysis.get("title") or document_type or "Документ"
    subtitle = analysis.get("subtitle") or ""

    # Дата документа: пытаемся вытащить YYYY-MM-DD из patient_line или date,
    # иначе используем сегодня.
    doc_date = _extract_doc_date(analysis)
    slug = _slugify_title(title)
    fname = f"{doc_date}_{slug}.md"

    dir_map = _specialist_dir_map()
    written: List[Path] = []
    for spec_ru in spec_names_ru:
        dir_name = resolve_specialist_slug(spec_ru, dir_map)
        if not dir_name:
            log.warning("Не знаю dir для специалиста %r — пропускаю", spec_ru)
            continue
        spec_dir = SPECIALISTS_DIR / dir_name
        if not spec_dir.exists():
            log.warning("Папка %s не существует — пропускаю", spec_dir)
            continue

        opinion = opinions.get(spec_ru, "")
        content = _format_perdoc_content(
            title=title,
            subtitle=subtitle,
            doc_date=doc_date,
            source_filename=source_filename,
            specialist_ru=spec_ru,
            document_type=document_type,
            opinion=opinion,
            raw_ocr_text=raw_ocr_text,
        )

        path = spec_dir / fname
        try:
            # Если файл уже есть с таким именем — добавляем суффикс
            if path.exists():
                stem = path.stem
                suffix = path.suffix
                i = 2
                while (spec_dir / f"{stem}_v{i}{suffix}").exists():
                    i += 1
                path = spec_dir / f"{stem}_v{i}{suffix}"
            path.write_text(content, encoding="utf-8")
            written.append(path)
            log.info("per-doc записан: %s", path.relative_to(PROJECT_DIR))
        except Exception as e:
            log.error("Не записала per-doc для %s: %s", spec_ru, e, exc_info=False)

    return written


def _extract_doc_date(analysis: dict) -> str:
    """Пытаемся вытащить дату документа в формате YYYY-MM-DD.
    Источники: analysis['date'] (DD.MM.YYYY от _save_and_render),
    patient_line с цифрами, иначе — сегодня."""
    date_field = (analysis.get("date") or "").strip()
    if date_field:
        m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", date_field)
        if m:
            d, mth, y = m.groups()
            return f"{y}-{int(mth):02d}-{int(d):02d}"

    patient_line = analysis.get("patient_line") or ""
    # ищем «DD месяц YYYY» или «DD.MM.YYYY»
    m = re.search(r"(\d{1,2})[.\s/](\d{1,2})[.\s/](\d{4})", patient_line)
    if m:
        d, mth, y = m.groups()
        try:
            return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
        except ValueError:
            pass

    return datetime.now().strftime("%Y-%m-%d")
