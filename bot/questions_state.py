"""
Маппинг Telegram message_id ↔ Q-ID. Хранится в data/.questions_state.json
(уже в .gitignore через data/.*.json).

Используется reply-handler'ом: пользователь делает reply на сообщение бота с
тегом Q-XXX, бот по message_id поднимает соответствующий Q-ID и пишет ответ
в pending.md.

State хранит последние ~500 записей — старые подрезаются (Q разбираются вручную
в Claude Code и попадают в pending.md, так что после resolved старая запись в
state больше не нужна).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent / "data"
STATE_FILE = STATE_DIR / ".questions_state.json"
MAX_ENTRIES = 500


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("questions_state: повреждённый файл, начинаем заново: %s", e)
        return {}


def _save(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if len(state) > MAX_ENTRIES:
        keep = sorted(state.items(), key=lambda kv: kv[1].get("ts", 0))[-MAX_ENTRIES:]
        state = dict(keep)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def register(message_id: int, qid: str, chat_id: int, ts: float) -> None:
    state = _load()
    state[str(message_id)] = {"qid": qid, "chat_id": chat_id, "ts": ts}
    _save(state)


def lookup(message_id: int) -> Optional[str]:
    state = _load()
    entry = state.get(str(message_id))
    return entry["qid"] if entry else None
