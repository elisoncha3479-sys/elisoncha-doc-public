"""
bot/digest.py — еженедельный (вс 10:00) и ежемесячный (1-е 10:00) digest для мамы.
Запускается планировщиком из main.py. Также доступен через ручные команды /digest_weekly и /digest_monthly.
"""
import asyncio
import logging
import os
from typing import Optional

import anthropic
from telegram.ext import Application

from context import (
    IDENTITY_RULES,
    PATIENT_BRIEF,
    load_recent_history,
    load_recent_reports,
    load_specialist_profiles,
)
from reconcile import load_latest_reconciliation
from staff_audit import audit_and_propose, format_proposal_message

log = logging.getLogger(__name__)


def _get_chat_id() -> Optional[int]:
    raw = os.environ.get("GROUP_CHAT_ID", "").strip()
    return int(raw) if raw else None

# Cost-tiering (2026-05-30): дайджест → Haiku по умолчанию.
# Откат: env DIGEST_MODEL=claude-sonnet-4-6.
DIGEST_MODEL = os.environ.get("DIGEST_MODEL", "claude-haiku-4-5-20251001")

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


WEEKLY_SYSTEM_PROMPT = f"""Ты пишешь пациентке еженедельное письмо о её здоровье.

{IDENTITY_RULES}

{PATIENT_BRIEF}

Тон: тёплый, заботливый, спокойный, без сюсюканья. Обращаться по имени-отчеству и на «вы».

Фокус еженедельного: ОБРАЗ ЖИЗНИ (сон, еда, движение, настроение, давление если есть данные), мягкие напоминания, мотивация через её жизнь (прогулки, сад, дача, любимые дела), а не через страхи.

Принципы:
— Объясняй любые медицинские термины простыми словами в скобках.
— Не назначай лечение, не отменяй и не корректируй назначения реальных врачей.
— Если за неделю были тревожные сигналы — упомяни прямо, но дай понятную следующую опцию (позвонить лечащему врачу, измерить давление дома, записаться на приём).
— Не нагнетай.
— Если данных за неделю мало — пиши коротко и честно: «у нас на этой неделе мало записей про ваше самочувствие, расскажите нам — как давление, сон, настроение?»

Длина: 5–8 коротких абзацев. Без заголовков и буллет-пунктов — обычное письмо."""


MONTHLY_SYSTEM_PROMPT = f"""Ты пишешь пациентке ежемесячный медицинский обзор.

{IDENTITY_RULES}

{PATIENT_BRIEF}

Тон: серьёзнее еженедельного, но всё равно тёплый. На «вы». Это разбор здоровья за месяц — в форме письма, не таблицы.

Структура:
1. Что хорошо за месяц — конкретные факты (анализы, ощущения, активность), 1–2 абзаца.
2. На что обратить внимание — спокойно, с понятным следующим шагом по каждому пункту. Если ссылаешься на реальный анализ или заключение — указывай источник (см. правило выше); если рекомендация исходит от нашей команды — так и пиши.
3. Напоминания: какие визиты, анализы, контрольные точки на горизонте.
4. ОДИН практический фокус на следующий месяц (одна привычка, одна цель — не пять).

Принципы:
— Объясняй термины простыми словами в скобках.
— Не назначай лечение, не отменяй назначения реальных врачей.
— Если есть тревожный сигнал — обязательно дать понятное «что делать прямо сейчас» (кому звонить, что мерить, когда идти к врачу).
— Никакой паники.

Длина: 8–14 абзацев."""


async def _generate(system_prompt: str, user_payload: str, max_tokens: int) -> str:
    """system передаётся как cache-блок — стабильные правила и схема дешевле
    на 90% при повторных вызовах в течение 5 минут."""
    system_blocks = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]

    def _call_sync():
        response = _get_client().messages.create(
            model=DIGEST_MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_payload}],
        )
        return response.content[0].text
    return await asyncio.to_thread(_call_sync)


async def generate_weekly_digest() -> str:
    profiles = load_specialist_profiles()
    history = load_recent_history(days=7)
    payload = (
        "Профили специалистов (контекст, не цитировать в письме):\n"
        f"{profiles}\n\n"
        "Записи и диалоги за последние 7 дней:\n"
        f"{history}\n\n"
        "Напиши маме письмо за эту неделю по правилам выше."
    )
    return await _generate(WEEKLY_SYSTEM_PROMPT, payload, max_tokens=2000)


async def generate_monthly_digest() -> str:
    profiles = load_specialist_profiles()
    history = load_recent_history(days=30)
    reports = load_recent_reports(days=30)
    reconcile_text = load_latest_reconciliation(max_age_days=35)
    reconcile_block = (
        "Свежий обзор лекарственной схемы (свод по всем специалистам, готовится перед "
        "ежемесячным письмом — используй для пунктов «обсудить с врачом», "
        "не цитируй таблицы дословно):\n"
        f"{reconcile_text}\n\n"
        if reconcile_text else ""
    )
    payload = (
        "Профили специалистов:\n"
        f"{profiles}\n\n"
        "Записи и диалоги за последние 30 дней:\n"
        f"{history}\n\n"
        f"Отчёты за последние 30 дней:\n{reports if reports else '(нет)'}\n\n"
        f"{reconcile_block}"
        "Напиши маме ежемесячный обзор по правилам выше."
    )
    return await _generate(MONTHLY_SYSTEM_PROMPT, payload, max_tokens=4000)


async def _send_long(app: Application, chat_id: int, text: str) -> None:
    """Telegram режет сообщения > 4096 симв. Шлём кусками."""
    LIMIT = 4000
    if len(text) <= LIMIT:
        await app.bot.send_message(chat_id=chat_id, text=text)
        return
    chunks = []
    cur = ""
    for paragraph in text.split("\n\n"):
        if len(cur) + len(paragraph) + 2 > LIMIT:
            chunks.append(cur)
            cur = paragraph
        else:
            cur = f"{cur}\n\n{paragraph}" if cur else paragraph
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        await app.bot.send_message(chat_id=chat_id, text=chunk)


def _humanize_error(e: Exception) -> str:
    """Переводит ошибку Anthropic SDK / прочее в понятный текст для группы.
    Пишем нейтрально, без обращения по имени — в чате может быть пациентка."""
    msg = str(e).lower()
    if isinstance(e, anthropic.BadRequestError) and "credit balance" in msg:
        return (
            "Закончился баланс на стороне нашего поставщика модели. "
            "Письмо пока собрать не получилось — нужно пополнить, и мы автоматически продолжим работу."
        )
    if isinstance(e, anthropic.AuthenticationError):
        return "Сейчас не получается обратиться к языковой модели — проблема с авторизацией ключа. Решим и вернёмся."
    if isinstance(e, anthropic.RateLimitError):
        return "Поставщик модели временно ограничил запросы. Попробуем собрать письмо позже."
    if isinstance(e, anthropic.APIConnectionError):
        return "Нет связи с языковой моделью. Проверим связь и пришлём письмо при ближайшей возможности."
    return f"Сейчас письмо собрать не получилось. Мы посмотрим логи и вернёмся. ({type(e).__name__})"


async def send_weekly_digest(app: Application) -> None:
    chat_id = _get_chat_id()
    if chat_id is None:
        log.warning("GROUP_CHAT_ID не задан, weekly digest пропущен")
        return
    log.info("Генерирую weekly digest...")
    try:
        text = await generate_weekly_digest()
        await _send_long(app, chat_id, text)
        log.info("Weekly digest отправлен (%d симв)", len(text))
    except Exception as e:
        log.error("Weekly digest упал: %s", e, exc_info=True)
        try:
            await app.bot.send_message(chat_id=chat_id, text=_humanize_error(e))
        except Exception:
            pass


async def _maybe_propose_new_specialist(app: Application, chat_id: int) -> Optional[dict]:
    """Один цикл аудита команды. Если есть теневая папка и бот считает,
    что нужен новый специалист — шлёт в группу заявку с черновиком и
    подсказкой по командам approve/reject. Возвращает proposal-dict если
    отправлено, либо None (нет кандидатов или LLM сказал «не нужен»).

    Failsafe — все ошибки логируются, наружу не пробрасываются. Это нужно,
    чтобы ежемесячное письмо мамы не сломалось из-за упавшего audit."""
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


async def send_monthly_digest(app: Application) -> None:
    chat_id = _get_chat_id()
    if chat_id is None:
        log.warning("GROUP_CHAT_ID не задан, monthly digest пропущен")
        return
    log.info("Генерирую monthly digest...")
    try:
        text = await generate_monthly_digest()
        await _send_long(app, chat_id, text)
        log.info("Monthly digest отправлен (%d симв)", len(text))
    except Exception as e:
        log.error("Monthly digest упал: %s", e, exc_info=True)
        try:
            await app.bot.send_message(chat_id=chat_id, text=_humanize_error(e))
        except Exception:
            pass
        return

    # После основного письма — auto staff audit (A3). Независимо от digest:
    # если письмо ушло, audit запускаем. Если audit упал — digest уже отправлен.
    await _maybe_propose_new_specialist(app, chat_id)
