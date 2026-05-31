"""
Elisoncha Doc — Telegram-бот с AI-анализом медицинских документов.
Мультиагентная система: главврач → специалисты (параллельно) → синтез.
Батчинг: все сообщения за 15 секунд группируются в один запрос.
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from context import (
    CLINICAL_RULES,
    IDENTITY_RULES,
    REPORT_JSON_SCHEMA,
    build_clinical_context,
    build_report_text_summary,
)
from digest import _maybe_propose_new_specialist, send_weekly_digest, send_monthly_digest
from health import heartbeat, start_health_server, HEARTBEAT_INTERVAL
from historical_check import historical_check
from ocr_validator import (
    format_hard_flag_message,
    format_soft_flags_for_synthesis,
    validate_ocr_text,
)
from history_regenerator import scheduled_regenerate as scheduled_history_regen
from log_filters import install_token_masking_on_root
from profile_refresher import refresh_profiles_for_specialists
import patient_identity
from identity_check import evaluate_identity, should_vision_recheck_identity
from identity_triage import (
    TRIAGE_BUTTONS,
    build_callback_data,
    parse_callback_data,
    triage_response,
)
from failure_modes import is_degraded, make_fallback_analysis
from persist import persist_analysis_to_memory
from profile_writer import write_perdoc_files
from questions_io import append_question, record_answer, reserve_question_id
from questions_state import lookup as lookup_question_state
from questions_state import register as register_question_state
import pending_batches
from reconcile import (
    maybe_run_reconcile_after_refresh,
    run_reconcile_and_notify,
    scheduled_monthly_reconcile,
)
from report_renderer import render_pdf_to_file
from staff_audit import (
    approve_draft,
    draft_path,
    read_draft,
    reject_draft,
)

from ocr import extract_text, assess_quality, save_text_sidecar
from agents import run_multi_agent
from patient_message import handle_patient_message, is_patient_author
from recent_dialog import messages_with_recent
from onboarding import (
    ONBOARDING_QUESTIONS,
    add_answer_part,
    build_persona,
    finish_onboarding,
    is_awaiting,
    is_finalize,
    pop_answers,
    resolve_roster,
    save_persona,
    start_onboarding,
    write_active_roster,
)

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
OCR_ENABLED = os.environ.get("OCR_ENABLED", "true").lower() == "true"
MULTI_AGENT = os.environ.get("MULTI_AGENT", "true").lower() == "true"
BATCH_WINDOW = int(os.environ.get("BATCH_WINDOW", "25"))
DOWNLOAD_RETRIES = int(os.environ.get("DOWNLOAD_RETRIES", "3"))
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "").strip()
ALLOWED_CHAT_ID = int(GROUP_CHAT_ID) if GROUP_CHAT_ID else None

# Модель для vision-вызовов (когда модель «смотрит глазами» в картинку).
# Дёшевая Haiku по умолчанию: vision-проходы у нас аварийные (когда
# tesseract не дал текста, либо vision-перепроверка ФИО на сжатом фото),
# чистое OCR — Haiku справляется почти идентично Sonnet. Текстовый
# разбор после OCR и врачебные ответы по-прежнему идут на Sonnet
# (через AGENTS_MODEL и др.) — там качество критично.
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")

INBOX = Path(__file__).parent.parent / "inbox"
HISTORY = Path(__file__).parent.parent / "history"
REPORTS = Path(__file__).parent.parent / "reports"
INBOX.mkdir(parents=True, exist_ok=True)
HISTORY.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
install_token_masking_on_root()
log = logging.getLogger(__name__)

# ====== BATCH QUEUE ======
# Per-chat queue: собирает сообщения за BATCH_WINDOW секунд
_batch_queues = defaultdict(list)  # chat_id -> list of items
_batch_timers = {}  # chat_id -> asyncio.Task
# token -> {"ocr","source","reason"} для триажа чужого документа (in-memory:
# при рестарте бота просто переспросит — данные в карте пациента не пишутся)
# token -> {chat_id, ts, paths, ocr, caption, has_vision_fallback,
# fallback_image_bytes} — документ ждёт ответа на 2 кнопки охранника.
# In-memory: при рестарте бота просто переспросит (в карту ничего не
# писалось). На кнопку «Подтверждаю» по этому контексту догоняем разбор.
_identity_triage_pending: dict[str, dict] = {}
_batch_ack_sent = {}  # chat_id -> bool (отправлено ли "получила, минуточку")


# ====== SYSTEM PROMPTS ======

SYSTEM_PROMPT_TEMPLATE = """Ты анализируешь медицинский документ пациентки и формируешь отчёт-письмо для неё. Отчёт автоматически свёрстается в PDF и отправится в семейный чат.

Принципы:
- Это письмо, не выписка. Связки между разделами (opening, before_lab_table, after_lab_summary, before_action_plan) — главные «склейки», превращающие документ в письмо.
- Прозой везде, где можно. Списки только в action_plan и lifestyle_text.concrete.
- Опирайся на клинический контекст пациентки (диагнозы, терапия, lifestyle) — он подгружен выше отдельным блоком. Не выдумывай данных, которых там нет.
- Если документ не лабораторный — lab_tables оставь null. Если нет повода для lifestyle — lifestyle_text null.
- Если документ нечитаемый — верни JSON с title="Не удалось прочитать", в opening и personal_closing мягко попроси прислать более чёткое фото."""


CHAT_PROMPT_TEMPLATE = """Пациентка задаёт вопрос в семейном чате. Ответь тепло, по-человечески, как заботливый семейный врач.

Правила:
- Обращайся на «вы» (как обращаться по имени — см. блок «КАК ОБРАЩАТЬСЯ»)
- Тон: тёплый, спокойный, уверенный — как разговор с хорошим доктором
- Отвечай понятным языком, без медицинского жаргона; термины — расшифровывай в скобках
- Опирайся на клинический контекст ниже (текущие диагнозы, терапия, последние данные)
- Если данных не хватает — скажи честно и предложи: либо позвонить лечащему врачу, либо прислать недостающие данные
- НЕ ставь новых диагнозов, не отменяй и не корректируй назначения реальных врачей
- Используй эмодзи умеренно (1-2 на сообщение)
- Отвечай коротко — 3-6 предложений, это чат, не отчёт"""


def _build_image_system() -> list:
    """Возвращает system как список блоков с cache_control на стабильной части —
    повторные вызовы платят 10% от input по этому блоку (5 мин жизнь кэша)."""
    text = (
        f"{IDENTITY_RULES}\n\n"
        f"{CLINICAL_RULES}\n\n"
        f"## КЛИНИЧЕСКИЙ КОНТЕКСТ ПАЦИЕНТКИ\n\n{build_clinical_context()}\n\n"
        f"## ЗАДАЧА\n\n{SYSTEM_PROMPT_TEMPLATE}\n\n"
        f"{REPORT_JSON_SCHEMA}"
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _build_chat_system() -> list:
    # Имя берём из профиля, а не из головы модели — иначе при инструкции
    # «по имени-отчеству» модель выдумывает имя (инцидент «Тамара Ивановна»).
    _who = patient_identity.address_name()
    if _who:
        name_rule = (
            f"## КАК ОБРАЩАТЬСЯ\n\nНазывай пациентку по имени «{_who}». "
            f"Не добавляй и не выдумывай отчество или фамилию.\n\n"
        )
    else:
        name_rule = (
            "## КАК ОБРАЩАТЬСЯ\n\nИмя пациентки неизвестно — обращайся тепло "
            "и на «вы», БЕЗ имени. Категорически не придумывай имя или отчество.\n\n"
        )
    text = (
        f"{IDENTITY_RULES}\n\n"
        f"{CLINICAL_RULES}\n\n"
        f"{name_rule}"
        f"## КЛИНИЧЕСКИЙ КОНТЕКСТ ПАЦИЕНТКИ\n\n{build_clinical_context()}\n\n"
        f"## ЗАДАЧА\n\n{CHAT_PROMPT_TEMPLATE}"
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# Честный fallback в схеме рендерера живёт в failure_modes.make_fallback_analysis()
# (аудит 2026-05-16, Тема 2): помечен _synthesis_failed → в память не пишется.


# ====== HELPERS ======

async def _chat_gate(update: Update) -> bool:
    """True если чат разрешён. В setup-режиме (GROUP_CHAT_ID пуст) отвечает chat_id и возвращает False."""
    chat = update.effective_chat
    chat_id = chat.id
    chat_title = chat.title or "(личка)"
    if ALLOWED_CHAT_ID is None:
        log.info("SETUP MODE: chat_id=%s title=%r", chat_id, chat_title)
        try:
            await update.message.reply_text(
                f"Setup mode.\n\nchat_id: {chat_id}\nНазвание: {chat_title}\n\n"
                "Передай chat_id владельцу — она пропишет меня в .env, и я заработаю в этом чате."
            )
        except Exception as e:
            log.warning("Не удалось ответить в setup mode: %s", e)
        return False
    if chat_id != ALLOWED_CHAT_ID:
        log.warning("Игнорирую chat_id=%s (разрешён только %s)", chat_id, ALLOWED_CHAT_ID)
        return False
    return True


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


async def _safe_download(ctx, file_id, path: Path, max_attempts: int = None) -> bool:
    """Скачивает Telegram-файл с retry на транзиентных ошибках (SSL, network).
    Возвращает True при успехе, False если все попытки упали."""
    attempts = max_attempts or DOWNLOAD_RETRIES
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            file = await ctx.bot.get_file(file_id)
            await file.download_to_drive(path)
            return True
        except Exception as e:
            last_err = e
            log.warning("Скачивание попытка %d/%d упала: %s", attempt, attempts, e)
            if attempt < attempts:
                # Экспоненциальная пауза: 0.5s, 1s, 2s
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
    log.error("Скачивание окончательно упало после %d попыток: %s", attempts, last_err)
    return False


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = text.rstrip()
        if fixed.count('"') % 2 == 1:
            fixed += '"'
        open_braces = fixed.count("{") - fixed.count("}")
        open_brackets = fixed.count("[") - fixed.count("]")
        fixed += "]" * open_brackets + "}" * open_braces
        return json.loads(fixed)


async def _analyze_ocr_text(ocr_text: str, caption: str = "", extra_context: str = "") -> dict:
    prompt = f"Пациент прислал фото медицинского документа. Текст распознан через OCR:\n\n{ocr_text}"
    if caption:
        prompt += f"\n\nКомментарий пациентки: {caption}"
    if extra_context:
        prompt += f"\n\n---\n\n{extra_context}"
    # АРХ1: в отдельный поток — не блокируем event loop.
    response = await asyncio.to_thread(
        lambda: claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=_build_image_system(),
            messages=[{"role": "user", "content": prompt}],
        )
    )
    return _parse_json(response.content[0].text)


async def _analyze_image(image_bytes: bytes, caption: str = "", extra_context: str = "") -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    user_content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
    ]
    prompt = f"Комментарий пациентки: {caption}" if caption else "Проанализируйте этот медицинский документ."
    if extra_context:
        prompt += f"\n\n---\n\n{extra_context}"
    user_content.append({"type": "text", "text": prompt})
    # АРХ1: в отдельный поток — не блокируем event loop.
    response = await asyncio.to_thread(
        lambda: claude.messages.create(
            model=VISION_MODEL,
            max_tokens=4096,
            system=_build_image_system(),
            messages=[{"role": "user", "content": user_content}],
        )
    )
    return _parse_json(response.content[0].text)


async def _vision_extract_text(image_bytes: bytes, media_type: str = "image/jpeg") -> str:
    """Распознать текст документа зрением модели (B, 2026-05-16).

    Используется как запасной заход, когда сжатое «фото» в Telegram
    лишило OCR текста (в т.ч. ФИО пациента). Best-effort: при любой
    ошибке возвращает '' — вызывающий код тогда спросит кнопками."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    content = [
        {"type": "image", "source": {"type": "base64",
                                     "media_type": media_type, "data": b64}},
        {"type": "text", "text": (
            "Перепиши ВЕСЬ распознаваемый текст с этого медицинского "
            "документа как есть, без интерпретации. Особенно точно — "
            "шапку: ФИО пациента, дату рождения, дату документа, "
            "учреждение. Только текст документа, без комментариев."
        )},
    ]
    resp = await asyncio.to_thread(
        lambda: claude.messages.create(
            model=VISION_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": content}],
        )
    )
    return resp.content[0].text.strip()


async def _chat_reply(text: str) -> str:
    # АРХ1: в отдельный поток — не блокируем event loop.
    response = await asyncio.to_thread(
        lambda: claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=_build_chat_system(),
            messages=messages_with_recent(text, HISTORY),
        )
    )
    return response.content[0].text


def _save_and_render(ts: str, analysis: dict) -> Path:
    """Сохраняем JSON для истории + рендерим PDF через report_renderer."""
    analysis["date"] = datetime.now().strftime("%d.%m.%Y")
    json_path = HISTORY / f"{ts}_analysis.json"
    json_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    pdf_path = REPORTS / f"{ts}_report.pdf"
    render_pdf_to_file(analysis, pdf_path)
    # Текстовая .md-сводка рядом с PDF: иначе месячный дайджест
    # (load_recent_reports читает .md/.txt) всегда пуст — аудит 2026-05-16,
    # Тема 4, C1.
    try:
        (REPORTS / f"{ts}_report.md").write_text(
            build_report_text_summary(analysis), encoding="utf-8"
        )
    except Exception as e:
        log.error("Не записала .md-сводку отчёта (не критично): %s", e)
    return pdf_path


def _build_caption(analysis: dict) -> str:
    """Тизер для подписи к PDF в Telegram. Не превышаем 1024 — лимит TG."""
    explicit = (analysis.get("telegram_caption") or "").strip()
    if explicit:
        return explicit[:1000]
    h = analysis.get("headline") or {}
    if h.get("headline"):
        icon = (h.get("icon") or "").strip()
        prefix = f"{icon} " if icon else ""
        line1 = f"{prefix}{h['headline']}"
        body = (h.get("body") or "").strip()
        return (f"{line1}\n\n{body}" if body else line1)[:1000]
    opening = (analysis.get("opening") or "").strip()
    if opening:
        return opening[:1000]
    return "Разбор готов."


async def _publish_question_message(
    chat_id: int,
    question: dict,
    source: str,
    specialist: str,
    app: Application,
) -> tuple[str, int] | None:
    """Резервирует Q-ID, пишет блок в pending.md, публикует встречный вопрос
    в группу с тегом и регистрирует mapping message_id↔qid. Возвращает
    (qid, message_id) или None если вопрос пуст."""
    topic = (question.get("topic") or "").strip()
    doubt = (question.get("doubt") or "").strip()
    why = (question.get("why_matters") or "").strip()
    options = question.get("options") or []
    if not doubt:
        return None

    now = datetime.now()
    qid = reserve_question_id(now)
    append_question(qid, question, source=source, specialist=specialist, now=now)

    lines = [f"📋 *{qid}* — {topic}" if topic else f"📋 *{qid}*", "", doubt]
    if options:
        lines.append("")
        lines.append("Варианты:")
        for i, opt in enumerate(options, 1):
            lines.append(f"{i}. {opt}")
    if why:
        lines.append("")
        lines.append(f"_Зачем: {why}_")
    lines.append("")
    lines.append("Ответьте *reply'ем* на это сообщение — текстом или номером варианта.")

    sent = await app.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
    )
    register_question_state(
        message_id=sent.message_id,
        qid=qid,
        chat_id=chat_id,
        ts=now.timestamp(),
    )
    log.info("Опубликован встречный вопрос %s (msg_id=%s)", qid, sent.message_id)
    return qid, sent.message_id


async def _defer_for_clarification(
    chat_id: int,
    first_msg,
    question: dict,
    analysis: dict,
    ts: str,
    source_name: str,
    file_paths: list,
    combined_ocr: str,
    combined_caption: str,
    soft_context: str,
    app: Application,
) -> str:
    """Откладывает финализацию батча до ответа на встречный вопрос.
    Сохраняет batch state на диск (чтобы пережить рестарт контейнера),
    публикует Q-сообщение в группу и шлёт короткое подтверждение, что
    разбор придёт после ответа."""
    published = await _publish_question_message(
        chat_id=chat_id,
        question=question,
        source=source_name,
        specialist="synthesis",
        app=app,
    )
    if not published:
        raise RuntimeError("Встречный вопрос пуст — не публикую")
    qid, _ = published

    pending_batches.save(qid, {
        "qid": qid,
        "chat_id": chat_id,
        "first_msg_id": first_msg.message_id,
        "ts": ts,
        "source_name": source_name,
        "file_paths": [str(p) for p in file_paths],
        "combined_ocr": combined_ocr,
        "combined_caption": combined_caption,
        "soft_context": soft_context,
    })

    await first_msg.reply_text(
        "Уточняю один момент, разбор пришлю после ответа."
    )
    return qid


async def _resume_batch_after_answer(
    qid: str,
    answer_text: str,
    author: str,
    app: Application,
) -> bool:
    """Снимает отложенный batch с диска и финализирует с учётом ответа:
    повторный multi-agent synthesis с ответом как extra_context, затем
    стандартный per-doc + refresh + reconcile + PDF. Если pending batch
    для этого qid нет — возвращает False (это просто ответ на старый Q)."""
    batch = pending_batches.pop(qid)
    if not batch:
        return False

    chat_id = batch["chat_id"]
    ts = batch["ts"]
    file_paths = [Path(p) for p in batch.get("file_paths", [])]
    combined_ocr = batch.get("combined_ocr", "")
    combined_caption = batch.get("combined_caption", "")
    soft_context = batch.get("soft_context", "")

    try:
        first_msg = await app.bot.send_message(
            chat_id=chat_id,
            text="Получила ответ, делаю разбор. Минуту.",
        )
    except Exception as e:
        log.error("Не смогла отправить ack после ответа на %s: %s", qid, e)
        return False

    answer_block = (
        f"\n\nУТОЧНЕНИЕ К {qid} (ответ {author}): {answer_text.strip()}\n"
        f"Учти это уточнение при синтезе. Если после ответа всё ещё остаются "
        f"сомнения — НЕ задавай новых вопросов, верни пустой clarifying_questions=[] "
        f"и зафиксируй оставшуюся неопределённость в action_plan."
    )
    extra_context = (soft_context + answer_block).strip()

    try:
        # Identity-гейт по ФИО стоит выше по потоку (в _process_batch до
        # defer). Отложенный clarifying-batch уже прошёл его или подтверждён
        # кнопкой — повторно по имени не перепроверяем, иначе подтверждённый
        # документ пациента без ФИО в OCR заблокировался бы здесь.

        # HARD-стоп OCR-валидатора на ветке resume. Раньше «после ответа»
        # этой проверки не было: документ с физиологически невозможным
        # значением (Hb=20) + уточняющим вопросом проскакивал в отчёт
        # (аудит 2026-05-16, Тема 2, B3).
        if combined_ocr:
            resume_validator = await validate_ocr_text(combined_ocr)
            if resume_validator.get("any_hard"):
                log.warning("Resume %s: HARD-флаг OCR — отчёт не строю", qid)
                json_path = HISTORY / f"{ts}_validator_hard.json"
                json_path.write_text(
                    json.dumps(resume_validator, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                await first_msg.reply_text(format_hard_flag_message(resume_validator))
                return True

        analysis = await run_multi_agent(
            claude,
            combined_ocr,
            caption=combined_caption,
            extra_context=extra_context,
        )
        if is_degraded(analysis):
            log.warning("Resume %s: разбор деградирован, historical-check пропущен", qid)
        else:
            analysis = await historical_check(analysis)
        # Второй раунд вопросов игнорируем независимо от того, что вернул LLM
        analysis["clarifying_questions"] = []

        await _finalize_analysis(
            analysis=analysis,
            chat_id=chat_id,
            first_msg=first_msg,
            ts=ts,
            file_paths=file_paths,
            combined_ocr=combined_ocr,
            app=app,
        )
        return True
    except Exception as e:
        log.error("Resume после ответа на %s упал: %s", qid, e, exc_info=True)
        try:
            await first_msg.reply_text(
                "Не получилось дособрать разбор после уточнения. "
                "Сохраню ответ в очередь, владелец разберётся вручную."
            )
        except Exception:
            pass
        return False


async def _handle_reply_as_answer(update: Update, app: Application) -> bool:
    """Если сообщение — reply на наше Q-сообщение, записывает ответ в pending.md.
    Если есть отложенный batch (синтез был приостановлен) — поднимает batch и
    делает финальный разбор с PDF. Возвращает True если обработали как ответ."""
    msg = update.message
    if not msg or not msg.reply_to_message or not msg.reply_to_message.from_user:
        return False
    if not msg.reply_to_message.from_user.is_bot:
        return False
    qid = lookup_question_state(msg.reply_to_message.message_id)
    if not qid:
        return False

    answer_text = (msg.text or "").strip()
    if not answer_text:
        return False

    author = (msg.from_user.first_name or msg.from_user.username or "пользователь").strip()
    ok = record_answer(qid, answer_text, author)
    if not ok:
        await msg.reply_text(
            f"Получила ответ, но не нашла {qid} в очереди. Сообщите владельцу."
        )
        return True

    resumed = await _resume_batch_after_answer(qid, answer_text, author, app)
    if not resumed:
        # Не было отложенного батча — просто ответ на старый Q
        await msg.reply_text(f"Записала ответ на {qid}. Спасибо.")
    return True


async def on_identity_triage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Две кнопки охранника (владелец, инцидент 013).

    «Случайно» → ничего не делаем, ничего не сохраняем.
    «Подтверждаю — это анализ мамы» → запускаем полный обычный разбор
    как принадлежащий пациенту (человек поручился глазами — страховка от ошибки OCR)."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    parsed = parse_callback_data(query.data or "")
    if not parsed:
        return
    code, token = parsed
    resp = triage_response(code)
    if resp is None:
        return
    pending = _identity_triage_pending.pop(token, None)

    try:
        await query.edit_message_text(resp["ack"])
    except Exception:
        try:
            await query.message.reply_text(resp["ack"])
        except Exception as e:
            log.error("Триаж: не смогла ответить пользователю: %s", e)

    if resp["action"] == "discard":
        # Ничего не анализируем и НИЧЕГО никуда не сохраняем.
        log.info("Триаж: документ помечен как случайный — игнорирую")
        return

    # action == "process": человек подтвердил, что документ принадлежит пациенту.
    if not pending:
        log.warning("Триаж: подтверждение, но контекст устарел (token нет)")
        try:
            await query.message.reply_text(
                "Не нашла этот документ — пришлите его, пожалуйста, "
                "ещё раз, и я сразу разберу."
            )
        except Exception:
            pass
        return

    log.info("Триаж: подтверждено как документ пациента — запускаю разбор")
    await _run_pipeline(
        pending["chat_id"],
        query.message,
        pending["ts"],
        [Path(p) for p in pending["paths"]],
        pending["ocr"],
        pending["caption"],
        context.application,
        pending.get("has_vision_fallback", False),
        pending.get("fallback_image_bytes"),
    )


async def _finalize_analysis(
    analysis: dict,
    chat_id: int,
    first_msg,
    ts: str,
    file_paths: list,
    combined_ocr: str,
    app: Application,
) -> None:
    """Финальные шаги после готового analysis: per-doc memory, refresh профилей,
    reconcile-hook, рендер PDF и отправка. БЕЗ публикации clarifying_questions —
    эта функция используется и для прямого прохода (без вопроса), и для прохода
    после ответа на встречный вопрос (где вопросы уже задавать нельзя)."""

    # 1) ОТЧЁТ ПАЦИЕНТУ — ПЕРВЫМ ДЕЛОМ. Раньше PDF уходил после refresh +
    # медленной сверки лекарств (мама ждала ~11 минут). Теперь память и
    # сверка идут ПОСЛЕ доставки и её не задерживают (согласовано
    # 2026-05-16). Их падение на уже отправленный отчёт не влияет.
    pdf_path = _save_and_render(ts, analysis)
    caption_text = _build_caption(analysis)
    with open(pdf_path, "rb") as fh:
        await first_msg.reply_document(
            document=fh,
            filename=f"Elisoncha_Doc_{ts}.pdf",
            caption=caption_text,
        )

    # 1b) Документный ход — в ленту чата, чтобы follow-up видел контекст
    # разбора. recent_dialog читает только *_chat.txt; раньше документ
    # сохранялся лишь как *_analysis.json и в память разговора не попадал —
    # уточняющий вопрос отвечался «с чистого листа», без связи с разбором
    # (живой тест Алисы 2026-05-30). Деградированный разбор в память
    # разговора не пишем — как и в долгую память (Тема 2).
    if not is_degraded(analysis):
        try:
            summary = build_report_text_summary(analysis)
            dialog_path = HISTORY / f"{ts}_chat.txt"
            dialog_path.write_text(
                "Сообщение мамы (документ): прислала медицинский документ на разбор\n\n"
                f"Ответ: {summary}",
                encoding="utf-8",
            )
        except Exception as e:
            log.error("Не записала документный ход в историю чата (не критично): %s", e)

    # 2) Память + сверка — ПОСЛЕ доставки отчёта. Единый источник
    # правды записи в память: ту же функцию зовёт и массовый загрузчик
    # bulk_import (parity-guard, уроки багов 009/010/011). Telegram:
    # app задан + run_reconcile=True → reconcile-hook с уведомлением
    # в чат включён. Деградированный разбор отсекается внутри
    # persist (аудит 2026-05-16, Тема 2).
    await persist_analysis_to_memory(
        analysis,
        file_paths=file_paths,
        combined_ocr=combined_ocr,
        app=app,
        run_reconcile=True,
    )


# ====== BATCH PROCESSING ======

async def _run_pipeline(
    chat_id: int,
    first_msg,
    ts: str,
    all_paths: list,
    combined_ocr: str,
    combined_caption: str,
    app: Application,
    has_vision_fallback: bool = False,
    fallback_image_bytes=None,
) -> None:
    """Конвейер ПОСЛЕ identity-гейта: OCR-валидатор → анализ →
    historical-check → defer/финал. Вынесен, чтобы его мог запустить и
    обычный батч, и кнопка «Подтверждаю — это анализ мамы» (инцидент 013).
    Имеет свой try/except: вызывается из двух мест."""
    source_name = all_paths[0].name if all_paths else "—"
    try:
        validator_result = None
        soft_context = ""
        if combined_ocr:
            log.info("OCR-валидатор...")
            validator_result = await validate_ocr_text(combined_ocr)
            if validator_result.get("any_hard"):
                retake_msg = format_hard_flag_message(validator_result)
                await first_msg.reply_text(retake_msg)
                json_path = HISTORY / f"{ts}_validator_hard.json"
                json_path.write_text(
                    json.dumps(validator_result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return
            if validator_result.get("any_soft"):
                soft_context = format_soft_flags_for_synthesis(validator_result)

        analysis = None
        if MULTI_AGENT and combined_ocr:
            log.info("Мультиагентный анализ (%d документов)...", len(all_paths))
            analysis = await run_multi_agent(
                claude, combined_ocr, caption=combined_caption, extra_context=soft_context
            )
        elif combined_ocr:
            analysis = await _analyze_ocr_text(combined_ocr, combined_caption, extra_context=soft_context)
        elif has_vision_fallback and fallback_image_bytes:
            analysis = await _analyze_image(fallback_image_bytes, combined_caption, extra_context=soft_context)

        if analysis is None:
            analysis = make_fallback_analysis()

        # Деградированный разбор не сверяем с историей: модель вернула бы
        # свежий JSON и потеряла бы маркер _synthesis_failed → память
        # отравилась бы (аудит 2026-05-16, Тема 2).
        if is_degraded(analysis):
            log.warning("Historical-check пропущен: разбор деградирован")
        else:
            log.info("Historical-check...")
            analysis = await historical_check(analysis)

        questions = analysis.get("clarifying_questions") or []
        if questions:
            try:
                qid = await _defer_for_clarification(
                    chat_id=chat_id,
                    first_msg=first_msg,
                    question=questions[0],
                    analysis=analysis,
                    ts=ts,
                    source_name=source_name,
                    file_paths=all_paths,
                    combined_ocr=combined_ocr,
                    combined_caption=combined_caption,
                    soft_context=soft_context,
                    app=app,
                )
                log.info("Батч отложен в ожидании ответа на %s", qid)
                return
            except Exception as e:
                log.error("Defer-флоу упал, отдаю PDF без уточнения: %s", e, exc_info=True)

        await _finalize_analysis(
            analysis=analysis,
            chat_id=chat_id,
            first_msg=first_msg,
            ts=ts,
            file_paths=all_paths,
            combined_ocr=combined_ocr,
            app=app,
        )
    except Exception as e:
        log.error("Ошибка в конвейере анализа: %s", e, exc_info=True)
        await first_msg.reply_text(
            "Извините, что-то пошло не так при анализе. "
            "Попробуйте прислать ещё раз."
        )


async def _process_batch(chat_id: int, app: Application) -> None:
    """Обрабатывает накопленный батч сообщений для чата."""
    items = _batch_queues.pop(chat_id, [])
    _batch_timers.pop(chat_id, None)
    _batch_ack_sent.pop(chat_id, None)

    if not items:
        return

    ts = _ts()
    # Берём первое сообщение для reply
    first_msg = items[0]["message"]

    log.info("Батч: обрабатываю %d сообщений для chat %s", len(items), chat_id)

    try:
        # Собираем все тексты и файлы
        all_ocr_texts = []
        all_captions = []
        all_paths = []
        unsupported_files = []
        has_vision_fallback = False
        fallback_image_bytes = None
        # Первое изображение батча — для перепроверки ФИО зрением, если
        # сжатое «фото» лишило OCR имени пациента (B, 2026-05-16).
        gate_img_bytes = None
        gate_img_media = None

        for item in items:
            if item["type"] == "text":
                all_captions.append(item["text"])
            elif item["type"] in ("photo", "document"):
                path = item["path"]
                all_paths.append(path)
                if gate_img_bytes is None:
                    _suf = path.suffix.lower()
                    if _suf in (".jpg", ".jpeg"):
                        gate_img_bytes, gate_img_media = path.read_bytes(), "image/jpeg"
                    elif _suf == ".png":
                        gate_img_bytes, gate_img_media = path.read_bytes(), "image/png"
                caption = item.get("caption", "")
                if caption:
                    all_captions.append(caption)

                # Vision (зрение) уместно ТОЛЬКО для настоящих картинок и
                # сканов. Бинарь office-документа или неизвестного формата
                # туда слать нельзя — Anthropic ответит 400 «Could not process
                # image», и весь разбор упадёт в общую заглушку (инцидент .odt,
                # 2026-05-31).
                _is_visual = path.suffix.lower() in (
                    ".jpg", ".jpeg", ".png", ".heic", ".pdf"
                )
                if OCR_ENABLED:
                    ocr_result = extract_text(str(path))
                    save_text_sidecar(str(path), ocr_result)
                    if assess_quality(ocr_result):
                        log.info("OCR OK (%.1f%%): %s", ocr_result.confidence, path.name)
                        all_ocr_texts.append(f"--- {path.name} ---\n{ocr_result.text}")
                    elif _is_visual:
                        log.info("OCR слабый (%.1f%%), отдаю зрению: %s", ocr_result.confidence, path.name)
                        has_vision_fallback = True
                        fallback_image_bytes = path.read_bytes()
                    else:
                        log.info("Формат не для зрения, текст не извлечён: %s", path.name)
                        unsupported_files.append(path.name)
                elif _is_visual:
                    has_vision_fallback = True
                    fallback_image_bytes = path.read_bytes()
                else:
                    unsupported_files.append(path.name)

        # Если были только текстовые сообщения (вопросы) — отвечаем в чате
        if not all_paths and all_captions:
            combined_text = "\n".join(all_captions)
            name = f"{ts}_text.txt"
            (INBOX / name).write_text(combined_text, encoding="utf-8")

            reply = await _chat_reply(combined_text)
            dialog_path = HISTORY / f"{ts}_chat.txt"
            dialog_path.write_text(f"Вопрос: {combined_text}\n\nОтвет: {reply}", encoding="utf-8")
            await first_msg.reply_text(reply)
            return

        # Собираем единый текст для анализа
        combined_ocr = "\n\n".join(all_ocr_texts)
        combined_caption = " ".join(all_captions) if all_captions else ""
        source_name = all_paths[0].name if all_paths else "—"

        # Неподдержанный формат и анализировать больше нечего → честно скажем,
        # какие форматы понимаем, а не валимся в общую заглушку (инцидент .odt).
        if unsupported_files and not combined_ocr and not has_vision_fallback:
            names = ", ".join(unsupported_files)
            _lead = patient_identity.address("Здравствуйте")
            await first_msg.reply_text(
                f"{_lead}! Я разбираю фото, сканы, PDF, Word (.docx) и .odt. "
                f"А это пока открыть не получилось: {names}. Пришлите, "
                f"пожалуйста, как фото, PDF или .docx 🙏"
            )
            return

        # IDENTITY GATE по ФИО — ДО анализа и доставки (инцидент 013).
        # Документ родственника с той же фамилией не должен пройти как
        # документ пациента. Дату рождения не используем — ненадёжно.
        verdict = evaluate_identity(
            combined_ocr, os.environ.get("PATIENT_FULL_NAME", "")
        )

        # B: сжатое «фото» могло лишить OCR имени → даём ещё один заход
        # зрением, прежде чем дёргать пациента кнопками (2026-05-16).
        if should_vision_recheck_identity(verdict, gate_img_bytes is not None):
            try:
                log.info("Identity suspect — перепроверяю ФИО зрением")
                vtext = await _vision_extract_text(gate_img_bytes, gate_img_media)
                if vtext:
                    v2 = evaluate_identity(
                        vtext, os.environ.get("PATIENT_FULL_NAME", "")
                    )
                    if v2.configured and not v2.suspect:
                        log.info("Identity: ФИО подтверждено зрением — без вопроса")
                        verdict = v2
                        # фото пожато → OCR беднее зрения; берём более
                        # полный текст для качественного разбора
                        if len(vtext.strip()) > len(combined_ocr.strip()):
                            combined_ocr = vtext
            except Exception as e:
                log.warning("Vision-перепроверка ФИО упала — спрошу кнопками: %s", e)

        if verdict.suspect:
            log.warning("Identity gate: ФИО пациента не подтверждено — спрашиваю")
            token = uuid.uuid4().hex[:8]
            _identity_triage_pending[token] = {
                "chat_id": chat_id,
                "ts": ts,
                "paths": [str(p) for p in all_paths],
                "ocr": combined_ocr,
                "caption": combined_caption,
                "has_vision_fallback": has_vision_fallback,
                "fallback_image_bytes": fallback_image_bytes,
            }
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=build_callback_data(code, token))]
                for code, label in TRIAGE_BUTTONS
            ])
            _who = patient_identity.address_name()
            _lead = f"{_who}, кажется" if _who else "Кажется"
            await first_msg.reply_text(
                f"{_lead}, этот документ не ваш — я не "
                "нашла в нём вашего имени. Если прислали случайно — не беру "
                "в работу. Если это всё-таки ваш анализ — нажмите «Это мой "
                "анализ», и я сразу разберу.\n\nМаленький совет: лучше "
                "присылать «как файл», а не «как фото» — так я распознаю "
                "документ точнее и реже переспрашиваю.",
                reply_markup=keyboard,
            )
            return
        if not verdict.configured:
            log.info(
                "Identity gate без эталона ФИО (PATIENT_FULL_NAME пуст) — "
                "беру документ в работу молча"
            )

        await _run_pipeline(
            chat_id, first_msg, ts, all_paths, combined_ocr,
            combined_caption, app, has_vision_fallback, fallback_image_bytes,
        )

    except Exception as e:
        log.error("Ошибка при обработке батча: %s", e, exc_info=True)
        await first_msg.reply_text(
            "Извините, что-то пошло не так при анализе. "
            "Попробуйте прислать ещё раз."
        )


async def _add_to_batch(chat_id: int, item: dict, app: Application) -> None:
    """Добавляет сообщение в батч и (пере)запускает таймер."""
    _batch_queues[chat_id].append(item)

    # Отправляем подтверждение только один раз за батч
    if not _batch_ack_sent.get(chat_id):
        _batch_ack_sent[chat_id] = True
        _who = patient_identity.address_name()
        _ack = (f"{_who}, получила 🤗\n" if _who else "Получила 🤗\n")
        await item["message"].reply_text(
            _ack + "Подождите немного — сейчас всё посмотрим..."
        )

    # Перезапускаем таймер
    old_timer = _batch_timers.get(chat_id)
    if old_timer and not old_timer.done():
        old_timer.cancel()

    async def _timer():
        await asyncio.sleep(BATCH_WINDOW)
        await _process_batch(chat_id, app)

    _batch_timers[chat_id] = asyncio.create_task(_timer())


# ====== HANDLERS ======

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return
    await update.message.reply_text(
        f"{patient_identity.greeting('👋')}\n\n"
        "Я — ваш медицинский помощник «Elisoncha Doc».\n\n"
        "Присылайте мне фото анализов, выписки или просто напишите вопрос — "
        "наша команда врачей всё разберёт и объяснит понятным языком.\n\n"
        "Если мы ещё не знакомы — напишите /onboarding, я расспрошу вас "
        "о себе, чтобы врачи говорили с вами на вашем языке.\n\n"
        "Не стесняйтесь, я здесь для вас."
    )


async def cmd_onboarding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Знакомство: шлём анкету и ждём ответ-письмо следующим сообщением."""
    if not await _chat_gate(update):
        return
    start_onboarding(update.effective_user.id)
    await update.message.reply_text(ONBOARDING_QUESTIONS)


async def cmd_digest_weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return
    await update.message.reply_text("Готовлю еженедельное письмо, минуту…")
    await send_weekly_digest(ctx.application)


async def cmd_digest_monthly(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return
    await update.message.reply_text("Готовлю ежемесячный обзор, это может занять пару минут…")
    await send_monthly_digest(ctx.application)


async def cmd_reconcile_meds(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return
    owner_id = os.environ.get("OWNER_CHAT_ID", "").strip()
    target_note = "пришлю в личку" if owner_id else "пришлю сюда (личка владельца не настроена)"
    await update.message.reply_text(f"Сверяю общую лекарственную схему, минуту… ({target_note})")
    path = await run_reconcile_and_notify(ctx.application, reason="manual")
    if path is None:
        await update.message.reply_text(
            "Сверку сейчас сделать не получилось — посмотрим логи и вернёмся."
        )


async def cmd_propose_specialist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск аудита команды. Бот посмотрит, у кого накопились документы
    без отдельной роли, и если найдёт кандидата — предложит подключить."""
    if not await _chat_gate(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "Смотрю, не пора ли подключить нового специалиста, минуту…"
    )
    proposal = await _maybe_propose_new_specialist(ctx.application, chat_id)
    if proposal is None:
        await update.message.reply_text(
            "Сейчас никого нового подключать не нужно — все теневые папки "
            "либо пустые, либо уже разбираются командой. Если ждёте, что "
            "бот предложит конкретного специалиста — проверьте, что в "
            "specialists/<slug>/ накопилось ≥ 3 документов."
        )


async def cmd_show_draft(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать содержимое agents/<slug>.md.draft текстом в чат.

    Раньше пытались отправлять файл через send_document, но Telegram
    таймаутит на нестандартном расширении `.draft`. Текстом — надёжнее
    и удобнее: пациентка/владелец видит черновик сразу, без скачивания.
    """
    if not await _chat_gate(update):
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /show_draft <slug>")
        return
    slug = ctx.args[0].strip()
    draft = read_draft(slug)
    if draft is None:
        await update.message.reply_text(f"Черновика agents/{slug}.md.draft нет.")
        return

    # Без parse_mode — чтобы спецсимволы внутри draft'а (звёздочки в markdown
    # тела, подчёркивания, скобки) не ломались форматировщиком.
    LIMIT = 3800
    chunks = [draft[i:i + LIMIT] for i in range(0, len(draft), LIMIT)]
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        header = (
            f"Черновик {slug} (часть {i}/{total})\n\n" if total > 1
            else f"Черновик {slug}\n\n"
        )
        await update.message.reply_text(header + chunk)

    await update.message.reply_text(
        f"Это весь черновик. Дальше:\n"
        f"• /approve_specialist {slug} — подключить\n"
        f"• /reject_specialist {slug} — отклонить (черновик удалится)"
    )


async def cmd_approve_specialist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Переименовать .md.draft → .md. После этого discover_specialists() подхватит
    нового специалиста на следующем разборе документа без рестарта контейнера."""
    if not await _chat_gate(update):
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /approve_specialist <slug>")
        return
    slug = ctx.args[0].strip()
    try:
        path = approve_draft(slug)
    except FileNotFoundError:
        await update.message.reply_text(
            f"Черновика agents/{slug}.md.draft нет — нечего одобрять."
        )
        return
    except FileExistsError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    await update.message.reply_text(
        f"✅ Специалист «{slug}» подключён.\n"
        f"Файл: agents/{path.name}.\n"
        f"Со следующего разбора главврач сможет его звать в маршрутизации.\n\n"
        f"Если папка specialists/{slug}/ уже содержит документы — "
        f"profile.md обновится автоматически после первого разбора в этой теме, "
        f"либо его можно собрать вручную через `commands/refresh-profile`."
    )
    log.info("approve_specialist: slug=%s → %s", slug, path)


async def cmd_reject_specialist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Удалить agents/<slug>.md.draft."""
    if not await _chat_gate(update):
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /reject_specialist <slug>")
        return
    slug = ctx.args[0].strip()
    removed = reject_draft(slug)
    if removed:
        await update.message.reply_text(f"🗑 Черновик `{slug}` удалён.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Черновика `{slug}` не было.", parse_mode="Markdown")
    log.info("reject_specialist: slug=%s removed=%s", slug, removed)


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Без _chat_gate — нужно для разовой настройки OWNER_CHAT_ID в личке.
    Возвращает chat_id отправителя и подсказку, как прописать в .env."""
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id
    chat_kind = chat.type  # private / group / supergroup / channel
    user_handle = f"@{user.username}" if user and user.username else (user.full_name if user else "—")
    await update.message.reply_text(
        f"chat_id: {chat_id}\n"
        f"тип чата: {chat_kind}\n"
        f"пользователь: {user_handle}\n\n"
        f"Если это твоя личка с ботом — пропиши в bot/.env:\n"
        f"OWNER_CHAT_ID={chat_id}\n"
        f"и перезапусти бота (docker compose up -d)."
    )
    log.info("whoami: chat_id=%s type=%s user=%s", chat_id, chat_kind, user_handle)


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Без _chat_gate — нужно для разовой настройки PATIENT_TG_USER_ID.
    Дружелюбно сообщает отправителю его numeric Telegram-id."""
    user = update.effective_user
    uid = user.id if user else "—"
    name = user.full_name if user else "—"
    await update.message.reply_text(
        f"Здравствуйте, {name}!\n\n"
        f"Ваш Telegram-номер: {uid}\n\n"
        f"Этого достаточно — спасибо."
    )
    log.info("myid: user_id=%s name=%s", uid, name)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return
    ts = _ts()
    photo = update.message.photo[-1]
    name = f"{ts}_photo.jpg"
    path = INBOX / name
    ok = await _safe_download(ctx, photo.file_id, path)
    if not ok:
        await update.message.reply_text(
            "Одно из фото не удалось скачать (Telegram прервал соединение). "
            "Перешлите его ещё раз — мы соберём всё в один разбор."
        )
        return
    log.info("Фото сохранено: %s", name)
    try:
        await _add_to_batch(update.effective_chat.id, {
            "type": "photo",
            "path": path,
            "caption": update.message.caption or "",
            "message": update.message,
        }, ctx.application)
    except Exception as e:
        log.error("Ошибка при добавлении фото в батч: %s", e)
        await update.message.reply_text("Что-то пошло не так с обработкой, попробуйте ещё раз.")


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return
    ts = _ts()
    doc = update.message.document
    ext = Path(doc.file_name).suffix if doc.file_name else ".bin"
    name = f"{ts}_doc{ext}"
    path = INBOX / name
    ok = await _safe_download(ctx, doc.file_id, path)
    if not ok:
        await update.message.reply_text(
            "Документ не удалось скачать (Telegram прервал соединение). "
            "Пришлите его ещё раз."
        )
        return
    log.info("Документ сохранён: %s", name)
    try:
        await _add_to_batch(update.effective_chat.id, {
            "type": "document",
            "path": path,
            "caption": update.message.caption or "",
            "message": update.message,
        }, ctx.application)
    except Exception as e:
        log.error("Ошибка при добавлении документа в батч: %s", e)
        await update.message.reply_text("Что-то пошло не так с обработкой, попробуйте ещё раз.")


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return
    log.info("Голосовое получено — отправляю редирект на диктовку")
    await update.message.reply_text(
        "Голосовые мы пока не разбираем. "
        "Если удобнее говорить — нажмите значок микрофона на клавиатуре телефона "
        "и проговорите вопрос, он сам наберётся текстом. Тогда мы сразу ответим."
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _chat_gate(update):
        return

    # Если это reply на наш встречный вопрос Q-XXX — записываем ответ в pending.md
    # и выходим, не пускаем в чат-режим и не добавляем в фото-батч.
    if await _handle_reply_as_answer(update, ctx.application):
        return

    # Онбординг: ответ может прийти НЕСКОЛЬКИМИ сообщениями (длинный
    # рассказ не влезает в одно сообщение Telegram). Копим части,
    # собираем по слову-маркеру ГОТОВО (баг dogfooding 2026-05-17).
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is not None and is_awaiting(user_id):
        text = update.message.text or ""
        if not is_finalize(text):
            parts = add_answer_part(user_id, text)
            await update.message.reply_text(
                f"Приняла часть {parts} ✍️ Продолжайте, если есть ещё. "
                "Когда закончите — пришлите одним словом: ГОТОВО"
            )
            return
        combined = pop_answers(user_id)
        if not combined:
            await update.message.reply_text(
                "Я пока ничего не получила. Напишите рассказ о себе "
                "(можно несколькими сообщениями), потом — ГОТОВО."
            )
            return
        try:
            await update.message.reply_text(
                "Спасибо! Собираю профиль и команду врачей — это займёт "
                "до минуты…"
            )
            persona = build_persona(claude, combined)
            save_persona(persona)
            roster = resolve_roster(persona)
            write_active_roster(roster)
            finish_onboarding(user_id)
            who = patient_identity.address_name()
            hello = f"Спасибо, {who}!" if who else "Спасибо!"
            n = len(roster)
            if n % 10 == 1 and n % 100 != 11:
                spec_word = "специалист"
            elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
                spec_word = "специалиста"
            else:
                spec_word = "специалистов"
            await update.message.reply_text(
                f"{hello} Я записала ваш профиль и собрала под вас команду "
                f"врачей ({n} {spec_word}). Они будут говорить с "
                "вами на вашем языке. Присылайте анализы или выписки — я всё "
                "разберу и объясню понятно."
            )
        except Exception as e:
            log.error("Онбординг: сбой обработки ответа: %s", e, exc_info=True)
            await update.message.reply_text(
                "Извините, не получилось сохранить профиль. Попробуйте "
                "ещё раз: /onboarding"
            )
        return

    ts = _ts()
    chat_id = update.effective_chat.id

    # Если в очереди уже есть фото/документы — текст добавляется как комментарий к батчу
    if _batch_queues.get(chat_id):
        _batch_queues[chat_id].append({
            "type": "text",
            "text": update.message.text,
            "message": update.message,
        })
        log.info("Текст добавлен в батч: %s", update.message.text[:50])
        return

    # Свободное сообщение пациентки → разбор главврачом: сведения уходят
    # в память специалистов, маме — тёплый ответ (владелец, 2026-05-16,
    # путь А). Сообщения владельца/оператора в клинику не идут — для них
    # остаётся обычный чат-ответ ниже.
    if is_patient_author(update.message):
        try:
            text = update.message.text
            (INBOX / f"{ts}_text.txt").write_text(text, encoding="utf-8")
            res = await handle_patient_message(
                claude, ctx.application, text=text, ts=ts
            )
            reply = res["reply"]
            dialog_path = HISTORY / f"{ts}_chat.txt"
            dialog_path.write_text(
                f"Сообщение мамы ({res['intent']}): {text}\n\nОтвет: {reply}",
                encoding="utf-8",
            )
            await update.message.reply_text(reply)
        except Exception as e:
            log.error("Ошибка при обработке сообщения мамы: %s", e, exc_info=True)
            await update.message.reply_text(
                "Извините, не смогла ответить. Попробуйте ещё раз."
            )
        return

    # Иначе (оператор/владелец свободным текстом) — обычный чат-ответ
    try:
        text = update.message.text
        name = f"{ts}_text.txt"
        (INBOX / name).write_text(text, encoding="utf-8")
        log.info("Текст сохранён: %s", name)

        reply = await _chat_reply(text)
        dialog_path = HISTORY / f"{ts}_chat.txt"
        dialog_path.write_text(f"Вопрос: {text}\n\nОтвет: {reply}", encoding="utf-8")
        await update.message.reply_text(reply)

    except Exception as e:
        log.error("Ошибка при обработке текста: %s", e, exc_info=True)
        await update.message.reply_text("Извините, не смогла ответить. Попробуйте ещё раз.")


async def _post_init(app: Application) -> None:
    """Запускаем планировщик после старта бота, в его же event loop."""
    await start_health_server()
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        heartbeat,
        "interval",
        seconds=HEARTBEAT_INTERVAL,  # было захардкожено 20 — env-var не работал (Тема 5, D3)
        id="health_heartbeat",
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_digest,
        CronTrigger(day_of_week="sun", hour=10, minute=0),
        args=[app],
        id="weekly_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        send_monthly_digest,
        CronTrigger(day=1, hour=10, minute=0),
        args=[app],
        id="monthly_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_history_regen,
        CronTrigger(hour=6, minute=0),
        args=[app],
        id="history_regen_daily",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_monthly_reconcile,
        CronTrigger(day=1, hour=9, minute=0),
        args=[app],
        id="monthly_reconcile",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "Планировщик: weekly=вс 10:00, monthly=1-е 10:00, "
        "reconcile=1-е 09:00, history-regen=ежедневно 06:00 МСК"
    )


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("onboarding", cmd_onboarding))
    app.add_handler(CommandHandler("digest_weekly", cmd_digest_weekly))
    app.add_handler(CommandHandler("digest_monthly", cmd_digest_monthly))
    app.add_handler(CommandHandler("reconcile_meds", cmd_reconcile_meds))
    app.add_handler(CommandHandler("propose_specialist", cmd_propose_specialist))
    app.add_handler(CommandHandler("show_draft", cmd_show_draft))
    app.add_handler(CommandHandler("approve_specialist", cmd_approve_specialist))
    app.add_handler(CommandHandler("reject_specialist", cmd_reject_specialist))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_identity_triage))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if not os.environ.get("PATIENT_FULL_NAME"):
        log.info(
            "Identity gate без эталона: PATIENT_FULL_NAME не задан — "
            "документы берутся в работу молча, без проверки ФИО"
        )

    log.info("Бот запущен (batch=%ds). Inbox: %s | Reports: %s", BATCH_WINDOW, INBOX, REPORTS)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
