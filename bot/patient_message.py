"""
bot/patient_message.py — свободное сообщение пациента в чат как клинический вход.

Письма и сводка лекарств заканчиваются вопросами к пациенту. Раньше его ответ
(если это не реплай на уточнение по документу Q-XXX) уходил в обычный
чат-ответ и оседал в общей истории — в память специалистов не попадал.

Этот модуль закрывает дырку (владелец, 2026-05-16, путь А):
1. Главврач сортирует намерение сообщения: clinical / question / both / social.
2. Если есть клинические сведения — переиспользуем существующий
   мультиагентный разбор (`run_multi_agent`), пишем per-doc нужным
   специалистам (`write_perdoc_files`) с явной меткой провенанса
   «со слов пациента», обновляем профили и дёргаем reconcile-hook.
   Применяется автономно, без подтверждения владельца (решение владельца).
3. Пациенту отвечаем тёплым голосом «Команда „Elisoncha Doc“» — без
   назначений лечения, термины простыми словами.

Границы v1: только текст; сообщения владельца/оператора в клинику не идут.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import patient_identity
from agents import run_multi_agent
from context import IDENTITY_RULES
from profile_writer import write_perdoc_files
from profile_refresher import refresh_profiles_for_specialists
from reconcile import maybe_run_reconcile_after_refresh
from recent_dialog import messages_with_recent

log = logging.getLogger(__name__)

# Папка с историей чата — для подмешивания недавних реплик в LLM-вызовы.
# Лежит рядом с пакетом bot/, mount'ится Docker-volume, переживает рестарт.
from pathlib import Path
_HISTORY_DIR = Path(__file__).resolve().parent.parent / "history"

PATIENT_MSG_MODEL = os.environ.get("PATIENT_MSG_MODEL", "claude-sonnet-4-6")

# Метка источника для per-doc: видно, что это пересказ пациентки, а не
# документ. Защищает долгую память от «отравления» (забота заложена
# аудитом 2026-05-16, Тема 2). Это прозрачность, НЕ гейт подтверждения.
CHAT_PROVENANCE_SOURCE = (
    "Сообщение пациента в чате (со слов пациентки, не подтверждено документом)"
)

_VALID_INTENTS = {"clinical", "question", "both", "social"}


# ====== ШАГ 1: СОРТИРОВКА НАМЕРЕНИЯ ======

INTENT_SYSTEM_PROMPT = """Ты — приёмный администратор медицинской команды. Пациентка написала свободное сообщение в чат. Определи его НАМЕРЕНИЕ — что с ним делать дальше.

Категории (выбери ровно одну):
- "clinical" — пациентка сообщает клинические сведения: симптомы, самочувствие, измерения (давление, сахар), что сказал/назначил/отменил врач, изменение или отмена лекарства. Это надо занести в память специалистов.
- "question" — пациентка о чём-то спрашивает, сведений для записи нет.
- "both" — и сведения, и вопрос одновременно.
- "social" — благодарность, приветствие, бытовая реплика. Записывать в память нечего.

Отвечай СТРОГО одним JSON-объектом без markdown-обёртки:
{"intent": "clinical|question|both|social"}"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def classify_patient_message(client, text: str) -> dict:
    """Возвращает {"intent": <одна из _VALID_INTENTS>}.

    При любой ошибке парсинга — безопасный фолбэк "question": просто
    ответить пациенту, в память ничего не писать."""
    try:
        resp = client.messages.create(
            model=PATIENT_MSG_MODEL,
            max_tokens=100,
            system=[{"type": "text", "text": INTENT_SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=messages_with_recent(text.strip(), _HISTORY_DIR),
        )
        raw = _strip_fences(resp.content[0].text)
        intent = json.loads(raw).get("intent", "question")
        if intent not in _VALID_INTENTS:
            intent = "question"
        return {"intent": intent}
    except Exception as e:
        log.warning("classify_patient_message: фолбэк question (%s)", e)
        return {"intent": "question"}


# ====== ОТВЕТ ПАЦИЕНТУ ======

PATIENT_REPLY_SYSTEM_PROMPT = f"""Ты пишешь пациентке короткий ответ в чат от лица команды «Elisoncha Doc» — тем же тёплым человеческим языком, что и письма-разборы.

{IDENTITY_RULES}

Контекст: пациентка написала сообщение. Если ниже есть «что отметили специалисты» — мягко подтверди, что её сведения приняты и зафиксированы у нужного врача, и одной фразой скажи понятный следующий шаг. Если это вопрос — ответь простыми словами. Если просто доброе слово — тепло ответь в 1–2 строки.

Правила:
— На «вы», заботливо, без сюсюканья. Как обращаться по имени — см. блок «КАК ОБРАЩАТЬСЯ» ниже.
— Термины — простыми словами в скобках.
— НЕ назначай, не отменяй и не корректируй лечение сам. Можно: «мы отметили это у кардиолога, обсудите на ближайшем визите».
— Без паники и «не волнуйтесь».
— Коротко: 2–6 фраз, одно сообщение.
— Не упоминай дочь/семью, не более 2 эмодзи."""


def generate_patient_reply(client, text: str, brief: str = "") -> str:
    """Тёплый короткий ответ пациенту. brief — краткая выжимка того, что
    отметили специалисты (для intent clinical/both); пусто для question/social."""
    brief_block = (
        f"## Что отметили специалисты по её сообщению\n\n{brief}\n\n---\n\n"
        if brief else ""
    )
    user_msg = (
        f"## Сообщение пациентки\n\n{text.strip()}\n\n---\n\n"
        f"{brief_block}"
        f"Напиши короткий тёплый ответ по правилам выше."
    )
    # КАК ОБРАЩАТЬСЯ: имя берём из профиля пациента, а НЕ из головы модели.
    # Без этого блока модель, видя «обращайся по имени», выдумывает имя
    # (инцидент «Тамара Ивановна», 2026-05-31).
    who = patient_identity.address_name()
    if who:
        name_rule = (
            f"\n\nКАК ОБРАЩАТЬСЯ: называй пациентку по имени «{who}». "
            f"Не добавляй и не выдумывай отчество или фамилию."
        )
    else:
        name_rule = (
            "\n\nКАК ОБРАЩАТЬСЯ: имя пациентки неизвестно — обращайся тепло "
            "и на «вы», БЕЗ имени. Категорически не придумывай имя или отчество."
        )
    system_text = PATIENT_REPLY_SYSTEM_PROMPT + name_rule
    try:
        resp = client.messages.create(
            model=PATIENT_MSG_MODEL,
            max_tokens=800,
            system=[{"type": "text", "text": system_text,
                     "cache_control": {"type": "ephemeral"}}],
            messages=messages_with_recent(user_msg, _HISTORY_DIR),
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning("generate_patient_reply упал — отдаю фолбэк: %s", e)
        who = patient_identity.address_name()
        lead = f"{who}, мы" if who else "Мы"
        return (
            f"{lead} получили ваше сообщение и всё отметили. "
            "Если что-то будет беспокоить — сразу напишите нам."
        )


# ====== АВТОР: ТОЛЬКО ПАЦИЕНТКА ======

def is_patient_author(msg) -> bool:
    """True если сообщение писала пациентка.

    Если задан env PATIENT_TG_USER_ID — строгая сверка по Telegram-id.
    Если не задан — фолбэк v1: считаем автором пациентку (пациент пишет
    свободным текстом, владелец работает командами/в личке). Известное
    ограничение задокументировано в PLAN-mom-message-triage.md."""
    pid = os.environ.get("PATIENT_TG_USER_ID", "").strip()
    if not pid:
        return True
    author_id = getattr(getattr(msg, "from_user", None), "id", None)
    return str(author_id) == pid


# ====== ОРКЕСТРАЦИЯ ======

async def _ingest_clinical(client, app, text: str) -> str:
    """Прогоняет текст через мультиагент, пишет per-doc с провенансом,
    обновляет профили, дёргает reconcile-hook. Возвращает краткую
    выжимку заключений специалистов (для ответа пациенту). Failsafe —
    ошибки логируются, наружу не пробрасываются."""
    try:
        analysis = await run_multi_agent(
            client,
            document_text=text,
            caption="сообщение пациентки в чате",
        )
    except Exception as e:
        log.error("patient_message: мультиагент упал: %s", e, exc_info=False)
        return ""

    opinions = analysis.get("_opinions") or {}
    brief = "\n".join(
        f"- {spec}: {op}" for spec, op in opinions.items() if op
    )

    try:
        written = write_perdoc_files(
            analysis,
            raw_ocr_text=text,
            source_filename=CHAT_PROVENANCE_SOURCE,
        )
    except Exception as e:
        log.error("patient_message: write_perdoc упал: %s", e, exc_info=False)
        return brief

    engaged_dirs = sorted({p.parent.name for p in written})
    if not engaged_dirs:
        log.info("patient_message: per-doc никому не записан")
        return brief

    try:
        refreshed = await refresh_profiles_for_specialists(engaged_dirs)
        refreshed_dirs = sorted({r.path.parent.name for r in refreshed})
        meds_changed_dirs = {
            r.path.parent.name for r in refreshed if r.meds_changed
        }
    except Exception as e:
        log.error("patient_message: refresh профилей упал: %s", e, exc_info=False)
        return brief

    if refreshed_dirs:
        try:
            await maybe_run_reconcile_after_refresh(
                refreshed_dirs, app, meds_changed_dirs=meds_changed_dirs
            )
        except Exception as e:
            log.error("patient_message: reconcile-hook упал: %s", e, exc_info=False)

    return brief


async def handle_patient_message(client, app, text: str, ts: str) -> dict:
    """Главная точка входа. Возвращает
    {"intent": str, "reply": str, "ingested": bool}."""
    # АРХ1: синхронные LLM-вызовы в отдельный поток — не блокируем
    # event loop, чтобы бот продолжал принимать сообщения.
    intent = (await asyncio.to_thread(classify_patient_message, client, text))["intent"]
    log.info("patient_message: намерение=%s (%d симв)", intent, len(text))

    brief = ""
    ingested = False
    if intent in {"clinical", "both"}:
        brief = await _ingest_clinical(client, app, text)
        ingested = bool(brief) or True  # обработка состоялась

    reply = await asyncio.to_thread(generate_patient_reply, client, text, brief=brief)
    return {"intent": intent, "reply": reply, "ingested": ingested}
