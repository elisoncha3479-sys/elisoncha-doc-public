"""
bot/history_regenerator.py — регенерация history/MEDICAL_HISTORY.md.

Главврач читает все profile.md специалистов + текущую MEDICAL_HISTORY.md
и переписывает её как актуальный мастер-вид. По расписанию (раз в сутки)
или по явному вызову.

Стабильная часть system (роль главврача + правила + инструкция) кэшируется.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import anthropic

from context import IDENTITY_RULES, load_specialist_profiles

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent
HISTORY_FILE = PROJECT_DIR / "history" / "MEDICAL_HISTORY.md"
CHIEF_AGENT_FILE = PROJECT_DIR / "agents" / "chief.md"

# Cost-tiering (2026-05-30): регенерация истории → Haiku по умолчанию.
# Откат: env HISTORY_REGEN_MODEL=claude-sonnet-4-6.
REGEN_MODEL = os.environ.get("HISTORY_REGEN_MODEL", "claude-haiku-4-5-20251001")

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


REGEN_INSTRUCTION = """ЗАДАЧА: перегенерировать history/MEDICAL_HISTORY.md как актуальный мастер-вид по пациентке.

Это твой собственный конспект как главврача — единая страница, отражающая всё клинически значимое: текущие диагнозы, актуальная терапия, последние находки, открытые вопросы, тренды, контрольные точки.

Сохраняй структуру, которая уже есть в текущей версии. Это **чистая перезапись** — один источник правды без хвостов.

Принципы:
— Используй profile.md специалистов как фактуру; в них уже сделана клиническая аналитика по их зонам.
— Не выдумывай данных вне профилей. Если в профилях чего-то нет — в MEDICAL_HISTORY этого тоже нет.
— Если между специалистами есть противоречие — пометь явно как открытый вопрос.
— В конце добавь строку: «_Обновлено автоматически: YYYY-MM-DD HH:MM МСК_».

ВЫХОД: чистый markdown, начинай сразу с `#` заголовка. Без markdown-обёртки, без комментариев."""


async def regenerate_medical_history() -> Optional[Path]:
    """Перезаписывает history/MEDICAL_HISTORY.md на основе всех profile.md."""
    profiles = load_specialist_profiles()
    if not profiles or profiles.startswith("("):
        log.info("Profile.md недоступны — пропускаю регенерацию MEDICAL_HISTORY")
        return None

    chief_role = ""
    if CHIEF_AGENT_FILE.exists():
        try:
            chief_role = CHIEF_AGENT_FILE.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Не прочла chief.md: %s", e)

    current = ""
    if HISTORY_FILE.exists():
        try:
            current = HISTORY_FILE.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Не прочла текущий MEDICAL_HISTORY: %s", e)

    system_text = (
        f"{IDENTITY_RULES}\n\n"
        f"---\n\n"
        f"## РОЛЬ ГЛАВВРАЧА\n\n{chief_role}\n\n"
        f"---\n\n"
        f"## ЗАДАЧА\n\n{REGEN_INSTRUCTION}"
    )
    system_blocks = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    user_msg = (
        f"## ТЕКУЩАЯ MEDICAL_HISTORY.md\n\n{current if current else '(пустой/нет)'}\n\n"
        f"---\n\n"
        f"## АКТУАЛЬНЫЕ profile.md СПЕЦИАЛИСТОВ\n\n{profiles}\n\n"
        f"---\n\nПерепиши MEDICAL_HISTORY.md полностью."
    )

    log.info("Регенерация MEDICAL_HISTORY...")

    def _call_sync() -> str:
        response = _get_client().messages.create(
            model=REGEN_MODEL,
            max_tokens=16000,
            system=system_blocks,
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text

    try:
        new_history = await asyncio.to_thread(_call_sync)
        new_history = new_history.strip()
        if new_history.startswith("```"):
            lines = new_history.split("\n")
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            new_history = "\n".join(lines)
        if not new_history or len(new_history) < 200:
            log.warning("Регенерация MEDICAL_HISTORY вернула подозрительно короткий результат — не записываю")
            return None
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(new_history, encoding="utf-8")
        log.info("MEDICAL_HISTORY.md обновлён (%d симв)", len(new_history))
        return HISTORY_FILE
    except Exception as e:
        log.error("Регенерация MEDICAL_HISTORY упала: %s", e, exc_info=False)
        return None


async def scheduled_regenerate(app=None) -> None:
    """Wrapper для apscheduler — игнорирует app, просто запускает регенерацию."""
    await regenerate_medical_history()
