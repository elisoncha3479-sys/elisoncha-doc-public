"""
Отложенные батчи разбора, ожидающие ответа на встречный вопрос.

Поток: пользователь шлёт фото → бот разбирает → synthesis вернул
clarifying_questions[] → бот сохраняет весь промежуточный state батча в
data/.pending_batches.json под ключом qid, отправляет вопрос в группу, ждёт.
Когда приходит reply → бот достаёт state, добавляет ответ как extra_context,
прогоняет synthesis повторно (второй раз игнорируя clarifying_questions),
делает PDF и финализирует per-doc + refresh + reconcile.

State на диске, не в памяти — это критично для переживания рестартов
контейнера (watchdog kill, ребут VPS).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent / "data"
STATE_FILE = STATE_DIR / ".pending_batches.json"


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("pending_batches: повреждённый файл, начинаем заново: %s", e)
        return {}


def _save(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save(qid: str, batch: dict) -> None:
    """Сохраняет промежуточный batch state под ключом Q-ID.
    batch должен содержать всё, что нужно для повторного synthesis:
    chat_id, first_msg_id, ts, source_name, combined_ocr, combined_caption,
    soft_context, file_paths (список абсолютных путей к скачанным файлам)."""
    state = _load()
    state[qid] = batch
    _save(state)
    log.info("pending_batches: сохранён batch для %s", qid)


def pop(qid: str) -> Optional[dict]:
    """Достаёт и удаляет batch state по Q-ID. Если нет — возвращает None."""
    state = _load()
    batch = state.pop(qid, None)
    if batch is not None:
        _save(state)
        log.info("pending_batches: извлечён batch для %s", qid)
    return batch


def peek(qid: str) -> Optional[dict]:
    """Возвращает batch без удаления (для отладки/инспекции)."""
    return _load().get(qid)


def list_all() -> dict:
    return _load()
