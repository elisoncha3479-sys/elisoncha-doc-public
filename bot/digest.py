"""
bot/digest.py — ежемесячная точка контакта с пациентом + аудит команды.

История: раньше тут жили еженедельное и ежемесячное письма-«простыни», которые
модель генерировала каждый раз заново. Решение владельца (2026-06): письма
путаные, огромные и бесполезные — убрать оба.

Теперь вместо них:
1. Короткий ЗАРАНЕЕ НАПИСАННЫЙ человеком месячный вопрос (без обращения к
   модели — короче, предсказуемо, без расходов). Спрашиваем, что изменилось за
   месяц (лекарства, анализы, самочувствие). Ответ пациента дальше едет по
   существующим рельсам patient_message.py: главврач разбирает → специалисты
   обновляют память «со слов пациента».
2. Аудит команды, отцепленный от письма (раньше шёл хвостом за месячным
   письмом) — отдельное месячное задание scheduled_staff_audit().
"""
import asyncio
import logging
import os
from typing import Optional

from telegram.ext import Application

from staff_audit import audit_and_propose, format_proposal_message

log = logging.getLogger(__name__)


def _get_chat_id() -> Optional[int]:
    raw = os.environ.get("GROUP_CHAT_ID", "").strip()
    return int(raw) if raw else None


# Текст месячного вопроса. Заранее написан человеком, не генерируется моделью.
# Вариант A. Бренд: «Elisoncha Doc».
MONTHLY_SURVEY_MESSAGE = (
    "Здравствуйте! Это команда «Elisoncha Doc». Нам важно ничего о вас не упустить. "
    "Расскажите, пожалуйста, что изменилось за этот месяц:\n"
    "— отменяли или меняли дозу какого-то лекарства;\n"
    "— начали принимать что-то новое;\n"
    "— сдавали анализы или были у врача — если да, пришлите нам, что получили;\n"
    "— и самое главное: как вы себя чувствуете? Что-то болит, тревожит, случилось?\n\n"
    "Просто напишите нам в ответ своими словами — мы всё внесём в вашу карту, "
    "чтобы вся команда видела полную картину и заботилась о вас точнее. "
    "Берегите себя, Elisoncha."
)


async def send_monthly_survey(app: Application) -> None:
    """1-го числа в 10:00: короткий месячный вопрос пациенту. Ответ пациента
    обрабатывается обычным путём (patient_message.py) — отдельный код не нужен."""
    chat_id = _get_chat_id()
    if chat_id is None:
        log.warning("GROUP_CHAT_ID не задан, monthly survey пропущен")
        return
    try:
        await app.bot.send_message(chat_id=chat_id, text=MONTHLY_SURVEY_MESSAGE)
        log.info("Monthly survey отправлен")
    except Exception as e:
        log.error("Monthly survey упал: %s", e, exc_info=True)


async def _maybe_propose_new_specialist(app: Application, chat_id: int) -> Optional[dict]:
    """Один цикл аудита команды. Если есть теневая папка и бот считает,
    что нужен новый специалист — шлёт в группу заявку с черновиком и
    подсказкой по командам approve/reject. Возвращает proposal-dict если
    отправлено, либо None (нет кандидатов или LLM сказал «не нужен»).

    Failsafe — все ошибки логируются, наружу не пробрасываются."""
    try:
        proposal = await asyncio.to_thread(audit_and_propose)
    except Exception as e:
        log.warning("staff_audit упал: %s", e, exc_info=True)
        return None
    if proposal is None:
        log.info("staff_audit: новых специалистов не предлагается")
        return None
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=format_proposal_message(proposal),
            parse_mode="Markdown",
        )
        log.info("staff_audit: предложение %s отправлено в группу", proposal["slug"])
        return proposal
    except Exception as e:
        log.warning("staff_audit: не удалось отправить предложение: %s", e)
        return None


async def scheduled_staff_audit(app: Application) -> None:
    """Отдельное месячное задание (отцеплено от письма, 1-го числа после
    месячного вопроса): проверяет, не нужен ли команде новый специалист, и если
    да — шлёт предложение в группу. Все ошибки гасятся внутри."""
    chat_id = _get_chat_id()
    if chat_id is None:
        log.warning("GROUP_CHAT_ID не задан, staff audit пропущен")
        return
    log.info("Staff audit (cron) старт")
    await _maybe_propose_new_specialist(app, chat_id)
