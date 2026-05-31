"""
Работа с questions/pending.md — резервация Q-ID, добавление новых вопросов,
запись ответов под нужный Q.

Q-ID формат: `Q-YYYYMMDD-NNN`, где NNN — порядковый номер за день (001..999).

Блок вопроса в pending.md:

    ## Q-20260511-001: <topic>

    **Источник:** <relative path или batch IMG_...>
    **Дата создания:** YYYY-MM-DD HH:MM
    **Специалист:** <suggested_specialist или synthesis>
    **Статус:** open

    **Вопрос:**
    <doubt>

    **Зачем уточняем:**
    <why_matters>

    **Варианты ответа:**
    - [ ] вариант 1
    - [ ] вариант 2

    ---

После reply'а в Telegram дописывается секция «**Ответ:**» и статус меняется на `answered`.
Файлы per-doc этим модулем НЕ правятся — это решает владелец вручную через Claude Code.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

QUESTIONS_DIR = Path(__file__).parent.parent / "questions"
QUESTIONS_FILE = QUESTIONS_DIR / "pending.md"

QID_PATTERN = re.compile(r"^## (Q-(\d{8})-(\d{3})):", re.MULTILINE)


def _ensure_file() -> None:
    QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not QUESTIONS_FILE.exists():
        QUESTIONS_FILE.write_text(
            "# Очередь открытых вопросов\n\n"
            "Сюда падают вопросы к маме, владельцу или врачам, поднятые любым агентом проекта. "
            "Главврач разбирает очередь, уточняет, и помечает resolved.\n\n---\n\n",
            encoding="utf-8",
        )


def reserve_question_id(now: Optional[datetime] = None) -> str:
    """Резервирует следующий Q-ID на сегодня. Атомарности здесь нет —
    бот один, конкурентного доступа не ждём."""
    _ensure_file()
    now = now or datetime.now()
    date_tag = now.strftime("%Y%m%d")
    text = QUESTIONS_FILE.read_text(encoding="utf-8")
    used = [
        int(m.group(3))
        for m in QID_PATTERN.finditer(text)
        if m.group(2) == date_tag
    ]
    next_n = (max(used) + 1) if used else 1
    return f"Q-{date_tag}-{next_n:03d}"


def _format_options(options: Optional[list]) -> str:
    if not options:
        return "- [ ] не читается / нужен новый скан\n- [ ] оригинала больше нет — помечаем недостоверным\n"
    lines = [f"- [ ] {opt}" for opt in options]
    lines.append("- [ ] не читается / нужен новый скан")
    return "\n".join(lines) + "\n"


def format_question_block(
    qid: str,
    payload: dict,
    source: str,
    specialist: str,
    now: Optional[datetime] = None,
) -> str:
    now = now or datetime.now()
    topic = payload.get("topic", "").strip() or "без темы"
    doubt = payload.get("doubt", "").strip() or "(вопрос не сформулирован)"
    why = payload.get("why_matters", "").strip() or "(пояснение отсутствует)"
    options = payload.get("options") or []

    return (
        f"## {qid}: {topic}\n\n"
        f"**Источник:** {source}\n"
        f"**Дата создания:** {now.strftime('%Y-%m-%d %H:%M')}\n"
        f"**Специалист:** {specialist}\n"
        f"**Статус:** open\n\n"
        f"**Вопрос:**\n{doubt}\n\n"
        f"**Зачем уточняем:**\n{why}\n\n"
        f"**Варианты ответа:**\n{_format_options(options)}\n"
        f"---\n\n"
    )


def append_question(
    qid: str,
    payload: dict,
    source: str,
    specialist: str,
    now: Optional[datetime] = None,
) -> None:
    _ensure_file()
    block = format_question_block(qid, payload, source, specialist, now)
    with QUESTIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(block)
    log.info("pending.md: добавлен %s (%s)", qid, payload.get("topic", ""))


def record_answer(
    qid: str,
    answer_text: str,
    author: str,
    now: Optional[datetime] = None,
) -> bool:
    """Находит блок Q-ID, дописывает раздел Ответ и меняет статус на answered.
    Возвращает True, если блок найден и обновлён."""
    _ensure_file()
    now = now or datetime.now()
    text = QUESTIONS_FILE.read_text(encoding="utf-8")

    header = f"## {qid}:"
    idx = text.find(header)
    if idx == -1:
        log.warning("pending.md: %s не найден, ответ не записан", qid)
        return False

    next_idx = text.find("\n## ", idx + len(header))
    block_end = next_idx if next_idx != -1 else len(text)
    block = text[idx:block_end]

    block = re.sub(r"\*\*Статус:\*\* open", "**Статус:** answered", block, count=1)

    answer_section = (
        f"\n**Ответ ({author}, {now.strftime('%Y-%m-%d %H:%M')}):**\n"
        f"{answer_text.strip()}\n"
    )
    sep_idx = block.rfind("\n---\n")
    if sep_idx != -1:
        block = block[:sep_idx] + answer_section + block[sep_idx:]
    else:
        block = block.rstrip() + "\n" + answer_section + "\n---\n\n"

    new_text = text[:idx] + block + text[block_end:]
    QUESTIONS_FILE.write_text(new_text, encoding="utf-8")
    log.info("pending.md: %s помечен answered (%s)", qid, author)
    return True
