"""
bot/reconcile.py — авто-сверка лекарственной схемы между специалистами.

Реализует commands/reconcile-meds.md как Python-обёртку:
- собирает разделы 4 «ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ» из всех 8 specialists/<spec>/profile.md
- через LLM-вызов формирует структурированный reconciliation отчёт
- пишет в reports/reconciliation_<YYYY-MM-DD>.md (копится, не перезаписывается)
- сравнивает с прошлым отчётом, при наличии новых сигналов — уведомляет в группу
- throttle 10 минут между запусками одного и того же триггера
- quiet hours 22:00-08:00 МСК — уведомление с disable_notification=True

Триггеры:
- auto-hook после refresh у фарм-активного специалиста (cardiologist, nephrologist,
   endocrinologist, hematologist, gastroenterologist)
- monthly cron 1-го в 09:00 МСК (за час до monthly digest)
- manual через CommandHandler /reconcile_meds
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, time as dt_time, timezone, timedelta
from pathlib import Path
from typing import Optional

import anthropic
import patient_identity
from telegram.ext import Application

from agents import discover_specialists
from context import IDENTITY_RULES
from meds_section import extract_meds_section

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent
SPECIALISTS_DIR = PROJECT_DIR / "specialists"
REPORTS_DIR = PROJECT_DIR / "reports"
COMMANDS_DIR = PROJECT_DIR / "commands"
DATA_DIR = PROJECT_DIR / "data"

RECONCILE_SPEC_FILE = COMMANDS_DIR / "reconcile-meds.md"
STATE_FILE = DATA_DIR / ".reconcile_state.json"

# Cost-tiering (2026-05-30): сверка лекарств → Haiku по умолчанию.
# Откат: env RECONCILE_MODEL=claude-sonnet-4-6.
RECONCILE_MODEL = os.environ.get("RECONCILE_MODEL", "claude-haiku-4-5-20251001")

# «Лекарственный» список теперь ДИНАМИЧЕСКИЙ (согласовано 2026-05-16).
# По умолчанию КАЖДЫЙ специалист из реестра считается назначающим и
# попадает в сверку — чтобы при добавлении нового врача его нельзя было
# случайно забыть (раньше хардкод из 5 пропускал невролога/ортопеда —
# дыра в безопасности, инцидент-13 показал цену пропусков).
# Исключение — явный короткий список НЕ-назначающих.
NON_PRESCRIBING_SPECS = {
    "lab-analyst",                # трактует анализы (цифры), сам лекарств не назначает
    "eating-disorder-therapist",  # психотерапия РПП, медикаменты — не его зона
    "dietitian",                  # питание/рацион, лекарств не назначает
}


def pharmaco_active_specs(agents_dir=None) -> set[str]:
    """Slug'и специалистов, чьи назначения участвуют в сверке лекарств.

    = все специалисты динамического реестра минус NON_PRESCRIBING_SPECS.
    Любой новый approve'нутый специалист входит автоматически."""
    slugs = set(discover_specialists(agents_dir).values())
    return slugs - NON_PRESCRIBING_SPECS

# Сколько секунд должно пройти между двумя запусками одного триггера (anti-throttle)
THROTTLE_SECONDS = int(os.environ.get("RECONCILE_THROTTLE_SECONDS", "600"))

# Тихие часы по МСК — в этом окне уведомление в группу идёт с disable_notification=True
QUIET_HOURS_START = 22  # 22:00 МСК включительно
QUIET_HOURS_END = 8     # до 08:00 МСК исключительно

MOSCOW_TZ = timezone(timedelta(hours=3))

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _get_chat_id() -> Optional[int]:
    raw = os.environ.get("GROUP_CHAT_ID", "").strip()
    return int(raw) if raw else None


def _get_owner_chat_id() -> Optional[int]:
    """Личный chat_id владельца/оператора (для manual-уведомлений). Если не
    задан — вернёт None, и manual-уведомления уйдут в группу с warning'ом."""
    raw = os.environ.get("OWNER_CHAT_ID", "").strip()
    return int(raw) if raw else None


def _resolve_target(target: str) -> tuple[Optional[int], str]:
    """Возвращает (chat_id, имя_цели). target ∈ {'group', 'owner'}.
    Fallback: 'owner' → 'group' если OWNER_CHAT_ID не задан."""
    if target == "owner":
        owner_id = _get_owner_chat_id()
        if owner_id is not None:
            return owner_id, "owner"
        log.warning("OWNER_CHAT_ID не задан — manual-уведомление уйдёт в группу")
        return _get_chat_id(), "group_fallback"
    return _get_chat_id(), "group"


# ====== STATE (throttle + last report path) ======

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Не прочла reconcile state: %s", e)
        return {}


def _save_state(state: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Не сохранила reconcile state: %s", e)


def _should_throttle(reason: str) -> bool:
    """True если с прошлого запуска того же reason прошло < THROTTLE_SECONDS."""
    state = _load_state()
    last = state.get("last_run_by_reason", {}).get(reason)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        # сравниваем aware-aware: нормализуем оба к UTC
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=MOSCOW_TZ)
        delta = (datetime.now(MOSCOW_TZ) - last_dt).total_seconds()
        return delta < THROTTLE_SECONDS
    except Exception:
        return False


def _mark_run(reason: str, report_path: Path) -> None:
    state = _load_state()
    state.setdefault("last_run_by_reason", {})[reason] = datetime.now(MOSCOW_TZ).isoformat()
    state["last_report_path"] = str(report_path.relative_to(PROJECT_DIR))
    _save_state(state)


def _previous_report_path(exclude: Optional[Path] = None) -> Optional[Path]:
    """Самый свежий reconciliation_*.md в reports/, исключая текущий."""
    if not REPORTS_DIR.exists():
        return None
    candidates = sorted(
        (p for p in REPORTS_DIR.glob("reconciliation_*.md") if p != exclude),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _in_quiet_hours() -> bool:
    now_msk = datetime.now(MOSCOW_TZ).time()
    if QUIET_HOURS_START >= QUIET_HOURS_END:
        # полночь пересекается: [22:00, 24:00) ∪ [00:00, 08:00)
        return now_msk >= dt_time(QUIET_HOURS_START) or now_msk < dt_time(QUIET_HOURS_END)
    return dt_time(QUIET_HOURS_START) <= now_msk < dt_time(QUIET_HOURS_END)


# ====== INPUT BUILDERS ======

# _extract_meds_section вынесен в meds_section.extract_meds_section —
# единый источник для reconcile и profile_refresher (backlog #4).


def _extract_profile_header(profile_text: str) -> str:
    """Вытаскивает дату «Последнее обновление» из шапки profile.md (или 'неизвестно')."""
    m = re.search(r"\*\*Последнее обновление:\*\*\s*([^\n]+)", profile_text)
    return m.group(1).strip() if m else "неизвестно"


def _gather_specs_meds() -> tuple[str, dict]:
    """Возвращает (текст для LLM, метаданные о свежести профилей).
    Метаданные: {spec_name: refresh_date_str}."""
    if not SPECIALISTS_DIR.exists():
        return "", {}

    parts: list[str] = []
    freshness: dict[str, str] = {}
    for spec_dir in sorted(SPECIALISTS_DIR.iterdir()):
        if not spec_dir.is_dir() or spec_dir.name.startswith("_"):
            continue
        profile = spec_dir / "profile.md"
        if not profile.exists():
            freshness[spec_dir.name] = "(profile.md отсутствует)"
            continue
        try:
            text = profile.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Не прочла %s: %s", profile, e)
            continue
        meds = extract_meds_section(text)
        if not meds:
            log.info("В %s раздел 4 не найден или пуст", profile.name)
        freshness[spec_dir.name] = _extract_profile_header(text)
        parts.append(f"## Специалист: {spec_dir.name}\n\n{meds if meds else '(раздел 4 пуст)'}")
    return "\n\n---\n\n".join(parts), freshness


def _load_reconcile_spec() -> str:
    if not RECONCILE_SPEC_FILE.exists():
        log.error("commands/reconcile-meds.md не найден — reconcile невозможен")
        return ""
    try:
        return RECONCILE_SPEC_FILE.read_text(encoding="utf-8")
    except Exception as e:
        log.error("Не прочла спеку reconcile-meds: %s", e)
        return ""


# ====== LLM CALLS ======

RECONCILE_TASK_INSTRUCTION = """ЗАДАЧА: следуй спецификации команды reconcile-meds.md выше и выдай готовый markdown-отчёт reconciliation.

Источник данных — единственный: разделы 4 «ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ» из 8 profile.md, которые приходят в user-сообщении ниже.

Соблюдай шаблон отчёта из спеки точно: разделы 1 (Актуальная схема) … 9 (Свежесть refresh-profile) и Источники, в указанном порядке.

В конец добавь сводную строку формата:
`reconcile-meds: записан reports/reconciliation_<дата>.md. Активных препаратов: N. Противоречий: M. Consensus-сигналов: K. Открытых вопросов: L.`

ВЫХОД: чистый markdown, начинающийся с `# Reconciliation лекарственной схемы — <дата>`.
Без markdown-обёртки ```, без комментариев модели."""


def _build_reconcile_system() -> list:
    spec = _load_reconcile_spec()
    text = f"{spec}\n\n---\n\n{RECONCILE_TASK_INSTRUCTION}"
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


async def _call_reconcile_llm(today: str, meds_text: str, freshness: dict) -> Optional[str]:
    freshness_block = "\n".join(f"- {k}: {v}" for k, v in sorted(freshness.items()))
    user_msg = (
        f"Сегодняшняя дата: {today}\n\n"
        f"## Свежесть refresh-profile (из шапок profile.md)\n\n{freshness_block}\n\n"
        f"---\n\n"
        f"## Разделы 4 «ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ» из 8 profile.md\n\n{meds_text}\n\n"
        f"---\n\n"
        f"Сформируй reconciliation-отчёт по спецификации."
    )

    def _call_sync() -> str:
        response = _get_client().messages.create(
            model=RECONCILE_MODEL,
            max_tokens=8000,
            system=_build_reconcile_system(),
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text

    try:
        text = await asyncio.to_thread(_call_sync)
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines).strip()
        if not text or len(text) < 500:
            log.warning("Reconcile LLM вернул подозрительно короткий результат — пропускаю запись")
            return None
        return text
    except Exception as e:
        log.error("Reconcile LLM упал: %s", e, exc_info=False)
        return None


DIFF_SYSTEM_PROMPT = """Ты сравниваешь два reconciliation-отчёта по лекарственной схеме одной пациентки и готовишь сводку ТОЛЬКО ПО ИЗМЕНЕНИЯМ — её потом перескажут пациентке тёплым человеческим языком. Полный отчёт остаётся в архиве; пересказывать всю схему НЕ нужно.

Сравни прошлую и новую версию. Возьми только то, что реально изменилось:
- 🆕 новый препарат в активной схеме
- ⬇️ снятый / отменённый препарат
- 🔄 изменённая доза или режим приёма
- ⚠️ новое противоречие между врачами или новый consensus-сигнал
- ✅ закрытое противоречие / снятый вопрос

Если изменений по содержанию НЕТ (поменялись только дата или служебные поля) — верни РОВНО одну строку: `NO_CHANGES`

Иначе верни 1–5 блоков, по одному на каждое изменение. Каждый блок — 4 короткие строки именно в таком порядке и с такими метками:

ЧТО: <привычное название препарата/изменения; класс/аббревиатуру — простыми словами в скобках>
ПОЧЕМУ ВАЖНО: <одна спокойная фраза, без алармизма, зачем за этим следить>
ЧТО ДЕЛАТЬ: <конкретное действие и к какому ИМЕННО специалисту: кардиолог / нефролог / терапевт и т.п.>
СРОЧНОСТЬ: <«срочно» | «при ближайшем визите» | «к сведению»>

Без вступлений, без итогов, без markdown-заголовков и без ``` — только блоки. Это рабочий материал для писателя, не финальный текст пациентке."""


async def _diff_with_previous(new_report: str, prev_report_path: Optional[Path]) -> Optional[str]:
    """Сравнивает новый отчёт с прошлым через LLM. Возвращает summary либо None,
    если изменений нет или сравнить не с чем."""
    if prev_report_path is None or not prev_report_path.exists():
        # Первый прогон — отдельный кейс: уведомлять не надо (мы только что создали базу)
        return None
    try:
        prev_text = prev_report_path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Не прочла прошлый reconcile %s: %s", prev_report_path, e)
        return None

    user_msg = (
        f"## Прошлый отчёт ({prev_report_path.name})\n\n{prev_text}\n\n"
        f"---\n\n"
        f"## Новый отчёт\n\n{new_report}\n\n"
        f"---\n\n"
        f"Выдай summary изменений по правилам выше."
    )

    def _call_sync() -> str:
        response = _get_client().messages.create(
            model=RECONCILE_MODEL,
            max_tokens=500,
            system=[{"type": "text", "text": DIFF_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text

    try:
        text = (await asyncio.to_thread(_call_sync)).strip()
        if not text or text.upper().startswith("NO_CHANGES"):
            return None
        return text
    except Exception as e:
        log.warning("Diff LLM упал: %s", e, exc_info=False)
        return None


# ====== NOTIFICATION ======

NOTIFICATION_SYSTEM_PROMPT = f"""Ты пишешь КОРОТКОЕ тёплое сообщение в семейный чат от лица команды «Elisoncha Doc» — тем же простым человеческим языком, что и обычные письма-разборы (PDF). Это не технический отчёт, а короткая человеческая выжимка.

{IDENTITY_RULES}

Контекст: тебе дают ТОЛЬКО сводку изменений в схеме лекарств (по каждому изменению: что · почему важно · что делать и к какому специалисту · срочность). Полная схема живёт у нас в архиве и здесь НЕ нужна — ты не видишь её и не должна её придумывать или дописывать. Твоя работа — превратить сухую сводку изменений в тёплое человеческое сообщение про то, что поменялось и что с этим делать. Ничего, кроме присланных изменений, не упоминай.

ФОРМАТ — КОРОТКО:
- Одно короткое вступление: мы обновили обзор ваших лекарств, вот что важно.
- 2–5 главных пунктов, каждый — одна-две простые фразы: что именно (привычное название в скобках, если есть) · почему это важно — одной фразой, без алармизма · что сделать — к какому ИМЕННО специалисту (кардиолог / нефролог / терапевт и т.п.), срочно или при ближайшем визите.
- Одна живая закрывающая фраза.
- Только самое важное. Обоснования, детали, кто когда назначил — НЕ переписывай, это в нашем архиве. Жёсткий потолок — **1200 символов, одно сообщение**.

ЗАПРЕЩЕНО: «уточните у лечащего врача» без указания специальности; аббревиатуры/классы препаратов без простого пояснения в скобках; «не волнуйтесь / не пугайтесь»; упоминание дочери/семьи; больше 2 эмодзи.

Тон: обращение к пациенту по имени-отчеству (если известно), на «вы»; заботливый, но взрослый; без сюсюканья и терминов-страшилок.

ЕСЛИ ИЗМЕНЕНИЙ НЕТ (manual без новых сигналов): 2–3 строки — обзор обновили, в схеме лекарств без изменений, всё стабильно, тёплый финал."""


async def _format_group_notification(diff_summary: Optional[str]) -> str:
    """Через LLM превращает сводку ИЗМЕНЕНИЙ в тёплое человеческое сообщение.

    Писателю даётся ТОЛЬКО сводка изменений (что · почему · что делать ·
    срочность) — полный свежий reconcile и прошлый отчёт сюда НЕ попадают,
    они остаются файлами в архиве. Так сообщение в чат опирается только на
    то, что поменялось, и не превращается в пересказ всей схемы (владелец,
    2026-05-16).

    Если diff_summary=None — это manual без изменений; LLM пишет короткое
    подтверждение «схема стабильна»."""
    if diff_summary:
        body = (
            f"## Сводка изменений в схеме лекарств\n\n{diff_summary}\n\n"
            f"---\n\n"
            f"Перепиши эти изменения как КОРОТКОЕ тёплое сообщение по правилам выше: "
            f"только присланное здесь, ничего не додумывай, к какому именно "
            f"специалисту и насколько срочно — потолок 1200 символов."
        )
    else:
        body = (
            "## Изменений с прошлой сверки нет.\n\n"
            "Это manual-запуск без новых сигналов. Напиши короткое подтверждение "
            "по правилам выше (блок «ЕСЛИ ИЗМЕНЕНИЙ НЕТ»): обзор обновили, "
            "в схеме без изменений, всё стабильно, тёплый финал."
        )
    user_msg = body

    def _call_sync() -> str:
        # Короткое сообщение: потолок 1200 символов в промте, max_tokens
        # с запасом на кириллицу, но без «портянки» (согласовано 2026-05-16).
        response = _get_client().messages.create(
            model=RECONCILE_MODEL,
            max_tokens=1300,
            system=[{"type": "text", "text": NOTIFICATION_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text

    try:
        return (await asyncio.to_thread(_call_sync)).strip()
    except Exception as e:
        log.warning("Notification LLM упал — отправляю фолбэк: %s", e)
        who = patient_identity.address_name()
        lead = f"{who}, мы" if who else "Мы"
        if diff_summary:
            return (
                f"{lead} обновили общий обзор ваших назначений. "
                "Что изменилось:\n"
                f"{diff_summary}\n\n"
                "Подробности обсудим в ближайшем письме или на очном визите. "
                "Если есть вопросы — напишите нам."
            )
        return (
            f"{lead} обновили общий обзор ваших назначений — "
            "изменений с прошлой сверки нет, текущая схема стабильна. "
            "Если есть вопросы — напишите нам."
        )


async def _send_notification(app: Application, message: str, target: str = "group") -> None:
    """target ∈ {'group', 'owner'}. 'owner' уйдёт в группу с warning,
    если OWNER_CHAT_ID не настроен."""
    chat_id, resolved = _resolve_target(target)
    if chat_id is None:
        log.info("Целевой chat_id не задан (target=%s) — пропускаю уведомление", target)
        return
    quiet = _in_quiet_hours()
    LIMIT = 4000  # Telegram режет на 4096, оставляем запас
    chunks: list[str] = []
    if len(message) <= LIMIT:
        chunks = [message]
    else:
        cur = ""
        for paragraph in message.split("\n\n"):
            if len(cur) + len(paragraph) + 2 > LIMIT:
                if cur:
                    chunks.append(cur)
                cur = paragraph
            else:
                cur = f"{cur}\n\n{paragraph}" if cur else paragraph
        if cur:
            chunks.append(cur)
    for i, chunk in enumerate(chunks):
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                disable_notification=quiet,
            )
        except Exception as e:
            log.error("Reconcile notification chunk %d/%d упал: %s", i + 1, len(chunks), e, exc_info=False)
            return
    log.info(
        "Reconcile notification отправлен (target=%s, quiet=%s, chunks=%d)",
        resolved, quiet, len(chunks),
    )


# ====== PUBLIC API ======

async def run_reconcile(reason: str = "manual") -> Optional[Path]:
    """Главная точка входа. Возвращает путь к новому отчёту либо None."""
    if _should_throttle(reason):
        log.info("Reconcile throttled (reason=%s, окно %d сек)", reason, THROTTLE_SECONDS)
        return None

    meds_text, freshness = _gather_specs_meds()
    if not meds_text:
        log.warning("Не собрала разделы 4 — reconcile отменён")
        return None

    today = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
    log.info("Reconcile старт (reason=%s, дата=%s)", reason, today)

    new_report = await _call_reconcile_llm(today, meds_text, freshness)
    if new_report is None:
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    candidate = REPORTS_DIR / f"reconciliation_{today}.md"
    if candidate.exists():
        # уже есть на сегодня — добавляем суффикс времени
        hhmm = datetime.now(MOSCOW_TZ).strftime("%H%M")
        candidate = REPORTS_DIR / f"reconciliation_{today}_{hhmm}.md"

    prev_path = _previous_report_path(exclude=candidate)
    candidate.write_text(new_report, encoding="utf-8")
    log.info("Reconcile отчёт записан: %s (%d симв)", candidate.name, len(new_report))
    _mark_run(reason, candidate)

    return candidate


async def run_reconcile_and_notify(app: Application, reason: str = "manual") -> Optional[Path]:
    """Запускает reconcile и уведомляет в группу:
    - hook/cron: только если есть diff с прошлым отчётом (молчим при отсутствии изменений)
    - manual: всегда (даже без diff отправляем подтверждение «изменений нет, схема стабильна»)
    """
    candidate = await run_reconcile(reason=reason)
    if candidate is None:
        return None

    is_manual = reason == "manual"
    prev_path = _previous_report_path(exclude=candidate)

    # Первый прогон в истории: ни manual, ни hook не уведомляют (нечего сравнивать)
    if prev_path is None and not is_manual:
        log.info("Прошлого reconcile нет — это первый авто-прогон, уведомление не шлю")
        return candidate

    try:
        new_report = candidate.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Не перечитала свежий отчёт для notification: %s", e)
        return candidate

    # Полный прошлый отчёт читает сам _diff_with_previous; в notification он
    # больше не передаётся — сообщение строится только из сводки изменений.
    diff_summary: Optional[str] = None
    if prev_path is not None:
        diff_summary = await _diff_with_previous(new_report, prev_path)

    # hook/cron + diff пустой → молчим
    if not is_manual and diff_summary is None:
        log.info("Reconcile diff пустой и reason=%s — уведомление не шлю", reason)
        return candidate

    # manual без изменений или с изменениями, либо hook/cron с изменениями → уведомляем
    notification = await _format_group_notification(diff_summary=diff_summary)
    # Routing: manual инициировал владелец → ему в личку. Hook/cron — сигнал пациенту → в группу.
    target = "owner" if is_manual else "group"
    await _send_notification(app, notification, target=target)
    return candidate


async def maybe_run_reconcile_after_refresh(
    refreshed_dirs: list[str],
    app: Application,
    meds_changed_dirs: Optional[set[str]] = None,
) -> Optional[Path]:
    """Hook для вызова из pipeline после refresh_profiles_for_specialists.

    Стреляет только если среди обновлённых есть фарм-активный специалист,
    у которого ФАКТИЧЕСКИ изменился раздел 4 «Лекарства» (smart-фильтр,
    backlog #4). Раньше сверка запускалась при любом refresh фарм-активного
    специалиста — например, после УЗДС-документа в кардио-профиль, где раздел
    лекарств не трогается, — и слала лишнее уведомление маме/владельцу.

    meds_changed_dirs=None → старое поведение (стрелять на любой refresh
    фарм-активного): нужно для обратной совместимости и вызовов без
    профиль-диффа.
    """
    pharm = pharmaco_active_specs()
    pharm_touched = [d for d in refreshed_dirs if d in pharm]
    if not pharm_touched:
        log.info("Refresh не затронул фарм-активных специалистов — reconcile не нужен")
        return None
    if meds_changed_dirs is not None:
        pharm_touched = [d for d in pharm_touched if d in meds_changed_dirs]
        if not pharm_touched:
            log.info(
                "Фарм-активные специалисты обновлены, но раздел 4 «Лекарства» "
                "не изменился — reconcile пропущен (smart-фильтр)"
            )
            return None
    log.info("Refresh затронул %s — запускаю reconcile (hook)", pharm_touched)
    try:
        return await run_reconcile_and_notify(app, reason=f"hook:{','.join(sorted(pharm_touched))}")
    except Exception as e:
        # failsafe: hook никогда не должен ломать основной pipeline
        log.error("Reconcile hook упал (не критично): %s", e, exc_info=False)
        return None


async def scheduled_monthly_reconcile(app: Application) -> None:
    """Cron-точка: 1-го числа в 09:00 МСК (за час до monthly digest)."""
    log.info("Monthly reconcile (cron) старт")
    try:
        await run_reconcile_and_notify(app, reason="monthly_cron")
    except Exception as e:
        log.error("Monthly reconcile упал: %s", e, exc_info=True)


def load_latest_reconciliation(max_age_days: int = 35) -> Optional[str]:
    """Возвращает текст самого свежего reconciliation_*.md в reports/, если он не старше
    max_age_days. Используется в monthly digest для подмешивания в контекст."""
    if not REPORTS_DIR.exists():
        return None
    candidates = sorted(
        REPORTS_DIR.glob("reconciliation_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    latest = candidates[0]
    age = (datetime.now() - datetime.fromtimestamp(latest.stat().st_mtime)).days
    if age > max_age_days:
        log.info("Свежий reconcile старше %d дней — не подмешиваю в monthly", max_age_days)
        return None
    try:
        return latest.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Не прочла свежий reconcile %s: %s", latest, e)
        return None
