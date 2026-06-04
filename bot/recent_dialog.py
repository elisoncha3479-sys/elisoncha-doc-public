"""bot/recent_dialog.py — окно последних реплик чата для подмешивания
в LLM-вызовы как полноценная диалоговая цепочка.

Контекст: history/*_chat.txt содержит каждую реплику отдельным файлом
с timestamp в имени. До этого модуля бот шёл в модель только с текущим
сообщением — терялась нить разговора, ощущение «захожу с нуля каждый раз».

Окно: последние RECENT_DIALOG_WINDOW_HOURS часов (дефолт 8).
Лимит: не больше RECENT_DIALOG_MAX_TURNS реплик (дефолт 10).
Файлы старше окна — игнорируются (новая сессия, недавнее не тащим;
постоянная память врачей в specialists/ при этом не зависит от этого
модуля и остаётся всегда).

Kill-switch: RECENT_DIALOG_ENABLED=false → функция всегда возвращает
[], вызывающий код шлёт только текущую реплику как раньше. Полезно
для быстрого отката без передеплоя кода.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)

WINDOW_HOURS = float(os.environ.get("RECENT_DIALOG_WINDOW_HOURS", "8"))
MAX_TURNS = int(os.environ.get("RECENT_DIALOG_MAX_TURNS", "10"))
ENABLED = os.environ.get("RECENT_DIALOG_ENABLED", "true").strip().lower() != "false"

_TS_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})_chat\.txt$")
_USER_PREFIX = re.compile(
    r"^(?:Сообщение пациента(?:\s*\([^)]*\))?:|Вопрос:)\s*",
    re.MULTILINE,
)
_REPLY_SEP = re.compile(r"\n\nОтвет:\s*", re.MULTILINE)


def _parse_ts(name: str) -> Optional[datetime]:
    m = _TS_NAME.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}_{m.group(2)}",
                                 "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None


def _parse_turn(text: str) -> Optional[tuple[str, str]]:
    """Возвращает (user_text, assistant_text) или None для кривых файлов."""
    if not text:
        return None
    body, n = _USER_PREFIX.subn("", text, count=1)
    if n == 0:
        return None
    parts = _REPLY_SEP.split(body, maxsplit=1)
    if len(parts) != 2:
        return None
    user_text = parts[0].strip()
    assistant_text = parts[1].strip()
    if not user_text or not assistant_text:
        return None
    return user_text, assistant_text


def load_recent_turns(
    history_dir: Path,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Возвращает массив для Anthropic messages=[…]: чередующиеся
    user/assistant реплики из последних RECENT_DIALOG_MAX_TURNS турнов
    в окне RECENT_DIALOG_WINDOW_HOURS часов.

    Текущая реплика НЕ включается — она добавляется вызывающим кодом
    отдельно. Если включить выключатель ENABLED=false или history_dir
    не существует — []."""
    if not ENABLED:
        return []
    if not history_dir.is_dir():
        return []

    cutoff = (now or datetime.now()) - timedelta(hours=WINDOW_HOURS)

    candidates: list[tuple[datetime, Path]] = []
    for path in history_dir.glob("*_chat.txt"):
        ts = _parse_ts(path.name)
        if ts is not None and ts >= cutoff:
            candidates.append((ts, path))

    candidates.sort(key=lambda t: t[0])
    candidates = candidates[-MAX_TURNS:]

    messages: list[dict] = []
    for _ts, path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("recent_dialog: не прочитан %s: %s", path.name, exc)
            continue
        turn = _parse_turn(text)
        if turn is None:
            continue
        user_text, assistant_text = turn
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})

    return messages


def messages_with_recent(
    current_user_text: str,
    history_dir: Path,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Удобная обёртка: добавляет current_user_text последним user-сообщением
    после недавней цепочки. Возвращает готовый массив для Anthropic
    messages=[…]."""
    prior = load_recent_turns(history_dir, now=now)
    return [*prior, {"role": "user", "content": current_user_text}]


def recent_dialog_summary(
    history_dir: Path,
    now: Optional[datetime] = None,
    max_turns: int = 4,
    max_chars: int = 1000,
) -> str:
    """Короткая текстовая выжимка недавнего разговора — для подмешивания в
    разбор документа (Баг A, 2026-06-04).

    Это НЕ полная диалоговая цепочка (как messages_with_recent), а компактная
    сводка, capped по размеру. Размер критичен: ровно из-за него недавний
    диалог из документного пайплайна раньше убрали (рефактор D2, 2026-05-30) —
    большой запрос ловил watchdog. Выжимка возвращает контекст, не раздувая
    запрос.

    Возвращает '' если недавнего диалога нет или ENABLED=false."""
    turns = load_recent_turns(history_dir, now=now)
    if not turns:
        return ""
    # load_recent_turns отдаёт чередующиеся user/assistant — берём хвост из
    # последних max_turns пар (по 2 реплики на пару).
    tail = turns[-(max_turns * 2):]
    lines: list[str] = []
    for m in tail:
        who = "Пациентка" if m["role"] == "user" else "Команда"
        text = " ".join(m["content"].split())
        if len(text) > 200:
            text = text[:200].rstrip() + "…"
        lines.append(f"— {who}: {text}")
    summary = "\n".join(lines)
    if len(summary) > max_chars:
        # режем с начала (старое менее ценно), не оставляя обрубок строки
        summary = summary[-max_chars:]
        nl = summary.find("\n")
        if nl != -1:
            summary = summary[nl + 1:]
    return summary
