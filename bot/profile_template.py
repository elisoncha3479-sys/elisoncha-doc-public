"""bot/profile_template.py — канонический скелет profile.md специалиста.

ЕДИНЫЙ источник структуры. Раньше bot.profile_refresher
описывал шаблон прозой, а commands/refresh-profile.md задавал явный каркас —
авто-refresh плодил произвольную разбивку (профиль без `## 4. ЛЕКАРСТВА`),
что обнуляло smart-фильтр reconcile (#4), якорящийся на этой секции.

Теперь скелет читается из commands/refresh-profile.md (того же файла, что
использует ручная Claude Code команда) — дублирования нет. Если файл
недоступен (напр., commands/ не смонтирован в docker — ср. bug-009),
используется встроенный fallback, тоже удовлетворяющий контракту с
meds_section.extract_meds_section.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent
DEFAULT_COMMAND_FILE = PROJECT_DIR / "commands" / "refresh-profile.md"

# Якорь, на котором держится meds_section.extract_meds_section (раздел 4).
MEDS_SECTION_ANCHOR = "## 4. ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ"

# Точные заголовки канонического шаблона. Контракт-тест проверяет, что они
# присутствуют и в скелете, и в REFRESH_INSTRUCTION.
CANONICAL_SECTION_HEADERS = (
    "## 1. МОЙ ПРОФИЛЬ ПАЦИЕНТКИ",
    "## 2. ХРОНОЛОГИЯ",
    "## 3. ТЕКУЩЕЕ СОСТОЯНИЕ",
    "## 4. ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ",
    "## 5. НА ЧТО ОБРАТИТЬ ВНИМАНИЕ — КРАСНЫЕ ФЛАГИ",
    "## 6. РЕКОМЕНДАЦИИ",
    "## 7. ОТКРЫТЫЕ ЗАДАЧИ (хвост по моей области)",
    "## 8. ИСТОЧНИКИ",
)

# Компактный аварийный каркас. Используется, только если командный файл не
# читается. Сохраняет все 8 секций и контракт с парсером лекарств.
_FALLBACK_SKELETON = """# <СПЕЦИАЛЬНОСТЬ В ВЕРХНЕМ РЕГИСТРЕ> — Свод по пациентке

**Пациент:** см. `history/MEDICAL_HISTORY.md`
**Последнее обновление:** ДД.ММ.ГГГГ ЧЧ:MM
**Документов в базе:** N

---

## 1. МОЙ ПРОФИЛЬ ПАЦИЕНТКИ

<Один абзац: пациентка глазами этого специалиста — конкретные цифры, стадии, даты.>

---

## 2. ХРОНОЛОГИЯ

| Дата | Исследование / событие | Ключевые находки | Тренд |
|------|------------------------|------------------|-------|

---

## 3. ТЕКУЩЕЕ СОСТОЯНИЕ

<🔴 Приоритет 1… с обоснованием и статусом.>

---

## 4. ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ

| Препарат | Доза | Цель | Особые замечания |
|----------|------|------|------------------|

**Что влияет на терапию из других областей:**

- <сопутствующий диагноз> → <как меняет тактику>

---

## 5. НА ЧТО ОБРАТИТЬ ВНИМАНИЕ — КРАСНЫЕ ФЛАГИ

| Флаг | Почему важно | Действие |
|------|--------------|----------|

---

## 6. РЕКОМЕНДАЦИИ

<Приоритезированный список действий, применимый к этой пациентке.>

---

## 7. ОТКРЫТЫЕ ЗАДАЧИ (хвост по моей области)

| Дата постановки | Задача | Почему важно | Статус |
|-----------------|--------|--------------|--------|

---

## 8. ИСТОЧНИКИ

- `<имя файла>.md` — <что это за документ и что из него взято>
"""


def _extract_fenced_block(text: str) -> str | None:
    """Достаёт содержимое первого блока ```markdown … ``` из текста команды."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```") and "markdown" in ln.strip().lower():
            start = i + 1
            break
    if start is None:
        return None
    out: list[str] = []
    for ln in lines[start:]:
        if ln.strip() == "```":
            return "\n".join(out).strip()
        out.append(ln)
    # закрывающего fence нет — блок битый
    return None


def load_profile_skeleton(command_file: Path | None = None) -> str:
    """Канонический скелет profile.md. По умолчанию — из
    commands/refresh-profile.md (единый источник). Fallback при недоступности."""
    path = command_file if command_file is not None else DEFAULT_COMMAND_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "Не прочитать %s (%s) — использую встроенный fallback-шаблон "
            "profile.md (bug-011/bug-009)",
            path,
            exc,
        )
        return _FALLBACK_SKELETON

    block = _extract_fenced_block(text)
    if not block or MEDS_SECTION_ANCHOR not in block:
        log.warning(
            "В %s не найден корректный ```markdown блок с секцией «Лекарства» "
            "— использую fallback-шаблон",
            path,
        )
        return _FALLBACK_SKELETON
    return block


PROFILE_SKELETON = load_profile_skeleton()
