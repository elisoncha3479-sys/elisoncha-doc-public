"""
Мультиагентная система «Elisoncha Doc».
Главврач → маршрутизация → специалисты (параллельно) → синтез.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Tuple

import anthropic

from failure_modes import make_fallback_analysis
from context import (
    CLINICAL_RULES,
    IDENTITY_RULES,
    REPORT_JSON_SCHEMA,
    SPECIALIST_DIRECTIVE,
    SYNTHESIS_DIRECTIVES,
    build_clinical_context,
    load_medical_history,
)
from cost_log import log_call

# D2 (2026-05-30): рабочая память разговора (recent_dialog) подмешивается
# ТОЛЬКО в chat-точки (patient_message + _chat_reply), не в медпайплайн.
# Роутингу/специалисту/синтезу история чата не нужна, а размер запроса
# критичен для watchdog — поэтому здесь чистые messages без подмешивания.

log = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"

# Активный состав под конкретного человека. agents/ — это БИБЛИОТЕКА
# общих архетипов; онбординг пишет сюда подмножество slug'ов нужных
# именно этому пользователю. Файла нет / пуст / битый → активны ВСЕ
# (обратная совместимость: репо-дефолт и любая старая установка не
# меняются, человек никогда не остаётся без врачей).
ACTIVE_ROSTER_PATH = Path(__file__).parent.parent / "data" / "active_specialists.json"

# Модель агентского конвейера (роутинг / специалист / синтез). По умолчанию
# Sonnet для качества интерактивных ответов. Для больших разовых задач
# (например, bulk-импорт архива) имеет смысл переключиться на более
# дешёвую модель (Haiku 4.5 — ~3× дешевле при ~90% качества Sonnet),
# передав AGENTS_MODEL через `docker exec -e AGENTS_MODEL=…` — это
# повлияет только на разовый запуск и не затронет интерактивный путь.
AGENTS_MODEL = os.environ.get("AGENTS_MODEL", "claude-sonnet-4-6")

# Cost-tiering (2026-05-30): routing (выбор врачей) — механика, дешёвый
# Haiku по умолчанию; специалисты + синтез остаются на AGENTS_MODEL
# (Sonnet) — там медрассуждение. Откат routing: env ROUTING_MODEL.
ROUTING_MODEL = os.environ.get("ROUTING_MODEL", "claude-haiku-4-5-20251001")

# Лимиты текста для модели. Раньше документ резался до 2000–3000 знаков,
# история — до 3000: 8-страничный PDF анализировался по первой странице,
# и модель ложно просила «пришлите скан получше» (баг 2026-05-16, аудит
# E3). Лимиты большие и настраиваемые: длинные выписки идут целиком.
DOC_TEXT_LIMIT = int(os.environ.get("DOC_TEXT_LIMIT", "40000"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "15000"))


def _clip(text: str, limit: int) -> str:
    """Отдаёт полный текст, если он короче лимита; иначе обрезает."""
    return text if len(text) <= limit else text[:limit]


def _load_agent(name: str) -> str:
    """Загружает промпт агента из .md файла."""
    path = AGENTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ---------- Data-driven реестр специалистов ----------
#
# Раньше SPECIALIST_MAP и список специалистов в ROUTING_PROMPT были захардкожены.
# Это блокировало approve-цикл нового специалиста: положить agents/neurologist.md
# было недостаточно — нужно было лезть в Python.
#
# Теперь реестр собирается из YAML-frontmatter каждого agents/*.md:
#
#     ---
#     slug: cardiologist
#     name_ru: кардиолог
#     role: specialist
#     domain: ишемическая болезнь сердца, артериальная гипертония, ...
#     ---
#
# Файлы с role != specialist (chief, ocr-validator) автоматически исключаются.

def parse_frontmatter(text: str) -> Tuple[dict, str]:
    """Парсит простой YAML-frontmatter (плоские key: value пары + folded/literal scalars).
    Возвращает (frontmatter_dict, body). Если блока нет — ({}, text).

    Поддерживает:
        key: value                  — однострочное значение
        key: "value"  /  key: 'v'   — с кавычками
        key: >                      — folded scalar (переносы → пробелы)
          строка 1
          строка 2
        key: |                      — literal scalar (переносы сохраняются)
          строка 1
          строка 2
        key:                        — пустое значение, эквивалент `>` если есть продолжение
          одна строка
          вторая
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return {}, text

    fm = {}
    body_start = None
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.rstrip() == "---":
            body_start = i + 1
            break

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Пропускаем отступленные строки, не относящиеся к открытому block scalar
        if line.startswith((" ", "\t")):
            i += 1
            continue

        if ":" not in stripped:
            i += 1
            continue

        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()

        if raw_value in (">", "|", ""):
            # Block scalar: читаем все отступленные строки до next top-level key / ---
            block_type = "|" if raw_value == "|" else ">"
            i += 1
            parts = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.rstrip() == "---":
                    break
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    # начинается следующий top-level key
                    break
                if nxt.strip():
                    parts.append(nxt.strip())
                i += 1

            if not parts:
                fm[key] = ""
            elif block_type == "|":
                fm[key] = "\n".join(parts)
            else:
                fm[key] = " ".join(parts)
            continue

        # Однострочное значение
        fm[key] = raw_value.strip('"').strip("'")
        i += 1

    if body_start is None:
        return {}, text
    return fm, "".join(lines[body_start:])


def _load_active_roster(path: Path) -> set | None:
    """Читает data/active_specialists.json → множество активных slug.

    Возвращает None (= фильтрации нет, активны все), если файла нет,
    он пуст, не список, или не парсится. Это намеренно безопасно:
    битый список НИКОГДА не оставляет человека без врачей.
    """
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("active_specialists.json не прочитан (%s) — активны все", exc)
        return None
    if not isinstance(data, list) or not data:
        log.warning("active_specialists.json пуст/не список — активны все")
        return None
    return {str(s).strip() for s in data if str(s).strip()}


def discover_specialists(agents_dir: Path = None,
                         active_roster_path: Path = None) -> dict:
    """Сканирует agents/*.md, собирает реестр специалистов из frontmatter.

    Возвращает упорядоченный по slug dict {name_ru: slug} только для тех файлов,
    у которых role == 'specialist'. Файлы без frontmatter — пропускаются с warning,
    с frontmatter но без обязательных полей — тоже пропускаются.

    Если есть активный состав (data/active_specialists.json) — отдаётся
    только его подмножество (agents/ при этом остаётся библиотекой).
    Нет файла/пуст/битый → активны все (обратная совместимость).

    agents_dir / active_roster_path по умолчанию — глобальные; параметры
    нужны для тестов.
    """
    directory = agents_dir if agents_dir is not None else AGENTS_DIR
    roster_path = active_roster_path if active_roster_path is not None else ACTIVE_ROSTER_PATH
    active = _load_active_roster(roster_path)
    entries = []
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Не удалось прочитать %s: %s", path.name, exc)
            continue

        fm, _ = parse_frontmatter(text)
        if not fm:
            log.warning("agents/%s: нет YAML-frontmatter, пропускаю", path.name)
            continue

        role = fm.get("role")
        slug = fm.get("slug")
        name_ru = fm.get("name_ru")

        if role != "specialist":
            if role and role != "meta":
                log.warning("agents/%s: неизвестный role=%r, пропускаю", path.name, role)
            continue

        if not slug or not name_ru:
            log.warning("agents/%s: role=specialist, но нет slug/name_ru", path.name)
            continue

        if active is not None and slug not in active:
            continue

        entries.append((slug, name_ru))

    entries.sort(key=lambda x: x[0])
    return {name_ru: slug for slug, name_ru in entries}


def _format_specialist_list(spec_map: dict) -> str:
    """Перечисление имён через запятую — для подстановки в ROUTING_PROMPT."""
    return ", ".join(spec_map.keys())


def resolve_specialist_slug(token: str, spec_map: dict) -> str | None:
    """Возвращает slug (= имя директории) для специалиста.

    Принимает русское имя (обычный путь, как в prompt) ИЛИ сам slug —
    модель иногда возвращает slug вместо русского имени для дефисных
    названий (стоматолог-гнатолог → dental-gnathologist). Оба варианта
    разрешаются в одно и то же — slug. Если token не известен ни как
    имя, ни как slug — None.
    """
    if not token:
        return None
    slug = spec_map.get(token)
    if slug:
        return slug
    if token in set(spec_map.values()):
        return token
    return None


def build_routing_prompt() -> str:
    """Главврач-роутинг. Список специалистов подставляется динамически
    из discover_specialists(). Двухшаговая классификация:
    тип документа → команда специалистов по типу. Принцип «недобор
    лучше перебора» — лишний специалист = долгий synthesis и размытый
    отчёт (инцидент 2026-05-30: выписка от одного врача подняла четверых,
    synthesis не уложился в watchdog)."""
    spec_list = _format_specialist_list(discover_specialists())
    return (
        "Ты — главврач системы «Elisoncha Doc». Тебе прислали "
        "медицинский документ пациентки.\n\n"
        "Твоя задача — определить, каких специалистов нужно подключить "
        "для анализа этого документа.\n\n"
        f"Доступные специалисты: {spec_list}.\n\n"
        "ШАГ 1 — классифицируй тип документа:\n"
        '- "lab_result"   — результат анализа из лаборатории (бланк со '
        "столбцами показателей и референсных значений). Главное "
        "содержание = цифры.\n"
        '- "consultation" — выписка/заключение/рекомендации от '
        "конкретного врача-специалиста после визита/обследования. "
        "Главное содержание = диагностика и план врача.\n"
        '- "instrumental" — результат инструментального исследования '
        "(УЗИ, ЭКГ, МРТ, КТ, эндоскопия). Главное содержание = "
        "описание + заключение.\n"
        '- "discharge"    — выписной эпикриз из стационара.\n'
        '- "other"        — справка, рецепт, что-то ещё.\n\n'
        "ШАГ 2 — подбери специалистов ПО ТИПУ:\n"
        '- "lab_result": лаборант ВСЕГДА + 1-2 узких врача по '
        "показателям (креатинин → нефролог, холестерин → кардиолог, "
        "глюкоза → эндокринолог). Не больше 3 суммарно.\n"
        '- "consultation": ТОЛЬКО автор выписки — один врач той '
        "специальности, чьё это заключение. Никаких смежных врачей, "
        "даже если в тексте упомянуты другие специалисты. "
        "Цифры внутри выписки — это упоминания прошлых анализов, "
        "лаборанта НЕ подключай.\n"
        '- "instrumental": специалист по органу исследования '
        "(УЗИ почек → нефролог, ЭКГ → кардиолог, "
        "гастроскопия/колоноскопия → гастроэнтеролог).\n"
        '- "discharge": 2-3 ключевых специалиста, упомянутых в '
        "эпикризе.\n"
        '- "other": 0-1 специалистов по смыслу; если ничего не '
        "подходит — пустой список.\n\n"
        "Принцип: «недобор лучше перебора». Если колеблешься между N "
        "и N+1 специалистом — выбирай N. Лишний специалист = долгий "
        "синтез и размытый отчёт. Точная команда из 1-2 врачей всегда "
        "лучше команды из 4.\n\n"
        "Верни ТОЛЬКО JSON (без markdown):\n"
        "{\n"
        '  "document_type": "lab_result|consultation|instrumental|discharge|other",\n'
        '  "specialists": ["..."],\n'
        '  "reasoning": "почему именно эти специалисты, без воды"\n'
        "}"
    )


# Маппинг имён → slug. Собирается один раз при импорте модуля,
# но обновляется на лету в run_multi_agent, чтобы approve нового специалиста
# срабатывал без перезапуска бота.
SPECIALIST_MAP = discover_specialists()
ROUTING_PROMPT = build_routing_prompt()


def build_specialist_system(agent_prompt: str, history_summary: str) -> str:
    """Системный промт специалиста: его роль + сокращённая история +
    директива не работать в силосе и называть связь с другими областями
    явно (аудит 2026-05-16, Тема 3)."""
    return (
        f"{agent_prompt}\n\n"
        f"---\n\n"
        f"## История болезни (сокращённая)\n\n{history_summary}\n\n"
        f"---\n\n"
        f"{SPECIALIST_DIRECTIVE}\n\n"
        f"---\n\n"
        f"Проанализируй этот документ с точки зрения СВОЕЙ специализации. "
        f"Дай заключение: что в норме, что требует внимания, что делать. "
        f"Ответ — простым текстом, 5-10 предложений."
    )


SYNTHESIS_PROMPT_TEMPLATE = """Специалисты команды дали свои заключения по документу пациентки. Твоя задача как главврача:
1. Собрать единый отчёт для пациентки.
2. Не просто проверить противоречия между специалистами, но и СВЯЗАТЬ находки разных областей в одну картину (см. блок «Комплексный взгляд» выше); противоречия — упомянуть и предложить решение.
3. Сформировать отчёт строго в JSON-формате (см. схему ниже) — он будет автоматически свёрстан в PDF и отправлен в семейный чат.

ЦЕЛЬ ОТЧЁТА: пациентка получит красиво свёрстанный PDF. Должно получиться **письмо**, не выписка. Связки между разделами (`opening`, `before_lab_table`, `after_lab_summary`, `before_action_plan`) — главные «склейки», превращающие документ в письмо.

Прозой везде, где можно. Списки уместны только в `action_plan` и `lifestyle_text.concrete`. Опирайся на клинический контекст пациентки выше — текущие диагнозы, терапию, lifestyle. Не выдумывай и не цитируй устаревших данных.

Если документ не лабораторный (например, заключение визита, выписка) — `lab_tables` оставь null. Если в документе нет повода для lifestyle — `lifestyle_text` null.
"""


def build_synthesis_system() -> list:
    """System как список блоков с cache_control — повторные вызовы дешевле на 90%."""
    text = (
        f"{IDENTITY_RULES}\n\n"
        f"{CLINICAL_RULES}\n\n"
        f"{SYNTHESIS_DIRECTIVES}\n\n"
        f"## КЛИНИЧЕСКИЙ КОНТЕКСТ ПАЦИЕНТКИ\n\n{build_clinical_context()}\n\n"
        f"## ЗАДАЧА\n\n{SYNTHESIS_PROMPT_TEMPLATE}\n\n"
        f"{REPORT_JSON_SCHEMA}"
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


async def run_multi_agent(
    client: anthropic.Anthropic,
    document_text: str,
    image_bytes: bytes = None,
    caption: str = "",
    extra_context: str = "",
    recent_context: str = "",
) -> dict:
    """
    Мультиагентный анализ документа.
    1. Главврач → маршрутизация
    2. Специалисты → параллельно
    3. Главврач → синтез
    """
    history = load_medical_history()
    history_summary = _clip(history, HISTORY_LIMIT)

    # --- Шаг 1: Маршрутизация ---
    log.info("Мультиагент: маршрутизация...")

    routing_msg = f"Документ пациентки:\n\n{_clip(document_text, DOC_TEXT_LIMIT)}"
    if caption:
        routing_msg += f"\n\nКомментарий пациентки: {caption}"
    if recent_context:
        routing_msg += (
            "\n\n---\nНЕДАВНИЙ РАЗГОВОР В ЧАТЕ (документ пришёл по ходу этого "
            f"обсуждения):\n{recent_context}"
        )

    # Зовём функции на каждый вызов — после approve нового специалиста
    # бот должен подключить его без перезапуска (A4).
    spec_map = discover_specialists()
    routing_prompt = build_routing_prompt()
    # АРХ1: синхронный вызов в отдельный поток — не блокируем event loop,
    # чтобы бот продолжал забирать новые сообщения из Telegram.
    routing_response = await asyncio.to_thread(
        lambda: client.messages.create(
            model=ROUTING_MODEL,
            max_tokens=500,
            system=[{"type": "text", "text": routing_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": routing_msg}],
        )
    )
    routing_cost = log_call("routing", ROUTING_MODEL, routing_response)

    try:
        routing_text = routing_response.content[0].text.strip()
        if routing_text.startswith("```"):
            routing_text = routing_text.split("\n", 1)[1].rsplit("```", 1)[0]
        routing = json.loads(routing_text)
    except (json.JSONDecodeError, IndexError):
        routing = {"specialists": ["лаборант", "кардиолог"], "document_type": "неизвестный"}

    specialists = routing.get("specialists", ["лаборант"])
    log.info("Мультиагент: подключаем %s", ", ".join(specialists))

    # --- Шаг 2: Специалисты параллельно ---
    async def consult_specialist(spec_name: str) -> dict:
        agent_file = resolve_specialist_slug(spec_name, spec_map)
        if not agent_file:
            return {"specialist": spec_name, "opinion": "Специалист не найден"}

        agent_prompt = _load_agent(agent_file)

        system = build_specialist_system(agent_prompt, history_summary)

        user_msg = f"Документ:\n\n{_clip(document_text, DOC_TEXT_LIMIT)}"
        if caption:
            user_msg += f"\n\nКомментарий: {caption}"

        # АРХ1: в отдельный поток — плюс настоящая параллельность
        # специалистов (раньше gather + sync create шли по очереди).
        response = await asyncio.to_thread(
            lambda: client.messages.create(
                model=AGENTS_MODEL,
                max_tokens=1000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_msg}],
            )
        )

        opinion = response.content[0].text
        cost = log_call(f"specialist:{spec_name}", AGENTS_MODEL, response)
        log.info("Мультиагент: %s ответил (%d символов)", spec_name, len(opinion))
        return {"specialist": spec_name, "opinion": opinion, "cost": cost}

    # Запускаем всех параллельно
    tasks = [consult_specialist(s) for s in specialists]
    opinions = await asyncio.gather(*tasks)

    # --- Шаг 3: Синтез ---
    log.info("Мультиагент: синтез от главврача...")

    opinions_text = ""
    for o in opinions:
        opinions_text += f"\n\n### {o['specialist'].upper()}\n{o['opinion']}"

    extra_block = f"\n\n---\n\n{extra_context}\n" if extra_context else ""
    recent_block = ""
    if recent_context:
        recent_block = (
            "\n\n---\n\nНЕДАВНИЙ РАЗГОВОР В ЧАТЕ (документ пришёл по ходу этого "
            f"обсуждения):\n{recent_context}\n\n"
            "Если документ — продолжение этого разговора (например, ранее "
            "просили прислать повторные/прошлые анализы), рассматривай его в "
            "связке: сравни с тем, что уже обсуждали, отметь динамику, НЕ "
            "разбирай с чистого листа. Если из разговора НЕ ясно, продолжение "
            "это или отдельная новая тема — задай это уточняющим вопросом "
            "(clarifying_questions)."
        )
    synthesis_msg = (
        f"Тип документа: {routing.get('document_type', 'неизвестный')}\n\n"
        f"Исходный документ:\n{_clip(document_text, DOC_TEXT_LIMIT)}\n\n"
        f"---\n\n"
        f"ЗАКЛЮЧЕНИЯ СПЕЦИАЛИСТОВ:{opinions_text}"
        f"{extra_block}"
        f"{recent_block}"
        f"\n---\n\n"
        f"Собери единый отчёт для пациентки. Проверь противоречия."
    )

    # АРХ1: синтез тоже в отдельный поток — это самый долгий вызов,
    # на нём и ловили watchdog при заблокированном loop.
    synthesis_response = await asyncio.to_thread(
        lambda: client.messages.create(
            model=AGENTS_MODEL,
            max_tokens=6000,
            system=build_synthesis_system(),
            messages=[{"role": "user", "content": synthesis_msg}],
        )
    )
    synthesis_cost = log_call("synthesis", AGENTS_MODEL, synthesis_response)
    total_cost = routing_cost + sum(o.get("cost", 0.0) for o in opinions) + synthesis_cost
    log.info(
        "COST TOTAL документ: routing+%d врачей+synthesis ~$%.4f",
        len(opinions), total_cost,
    )

    result_text = synthesis_response.content[0].text.strip()
    if result_text.startswith("```"):
        result_text = result_text.split("\n", 1)[1].rsplit("```", 1)[0]

    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as first_err:
        # Попробуем починить обрезанный JSON
        fixed = result_text.rstrip()
        if fixed.count('"') % 2 == 1:
            fixed += '"'
        open_braces = fixed.count("{") - fixed.count("}")
        open_brackets = fixed.count("[") - fixed.count("]")
        fixed += "]" * open_brackets + "}" * open_braces
        try:
            result = json.loads(fixed)
            log.warning("Синтез JSON починен авторемонтом скобок")
        except json.JSONDecodeError as second_err:
            snippet = result_text[:1200].replace("\n", "⏎")
            log.error(
                "Не удалось распарсить синтез главврача. err1=%s err2=%s | RAW(1200): %s",
                first_err, second_err, snippet,
            )
            # Сбой парсинга синтеза → честный fallback, помеченный
            # _synthesis_failed: вызывающий код НЕ пишет его в долгую
            # память (аудит 2026-05-16, Тема 2, B5).
            result = make_fallback_analysis()

    # Добавляем мета-информацию
    result["_routing"] = routing
    result["_opinions"] = {o["specialist"]: o["opinion"][:200] for o in opinions}

    return result
