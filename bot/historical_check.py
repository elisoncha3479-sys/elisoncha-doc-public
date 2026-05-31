"""
bot/historical_check.py — пост-synthesis проверка против истории пациентки.

Принимает уже сгенерированный JSON-отчёт + специалистские profile.md,
проверяет: нет ли каждого `highlight` и пункта `action_plan` уже в профилях
как «разобрано», «закрыто», «в работе». Корректирует JSON: либо снимает
лишний highlight, либо добавляет контекст «ранее уже обсуждалось».

Цель: не пугать пациентку повторно тем, что у неё уже учтено и решается.
Кейс из живого фидбека (2026-05-09): фолиевая кислота, которая в одном
анализе ниже нормы, может быть уже принята на контроль с прошлого месяца —
тогда не нужно подавать её как «новая критическая находка».
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import anthropic

from context import IDENTITY_RULES, load_specialist_profiles

from agents import discover_specialists


def _relevant_specialist_dirs(report: dict) -> list:
    """Из отчёта вытаскиваем список dir-имён специалистов, чьи profile.md
    надо подгрузить для historical-check. Берём из `_routing.specialists`,
    если есть; иначе — пустой список (загрузим все).

    name_ru→slug берётся из динамического реестра agents/*.md (backlog #2),
    а не из захардкоженного словаря: новый approve'нутый специалист
    подхватывается без правки Python. Циклического импорта нет — agents.py
    не импортирует historical_check."""
    routing = report.get("_routing") or {}
    spec_names = routing.get("specialists") or []
    dir_map = discover_specialists()
    dirs = [dir_map.get(s) for s in spec_names]
    return [d for d in dirs if d]

log = logging.getLogger(__name__)

# Cost-tiering (2026-05-30): механический шаг (старый ли документ) → Haiku.
# Откат: env HISTORICAL_CHECK_MODEL=claude-sonnet-4-6.
CHECK_MODEL = os.environ.get("HISTORICAL_CHECK_MODEL", "claude-haiku-4-5-20251001")

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


HISTORICAL_CHECK_SYSTEM = f"""{IDENTITY_RULES}

## ТВОЯ РОЛЬ ЗДЕСЬ

Ты — главврач команды «Elisoncha Doc» на втором проходе. Тебе передан уже сгенерированный отчёт по новому документу пациентки + актуальные профили специалистов (`specialists/<spec>/profile.md`).

Твоя единственная задача: проверить, не разобрана ли уже каждая находка из `highlights` и `action_plan` в профилях. Если разобрана — скорректировать.

## ПРАВИЛА КОРРЕКЦИИ

1. Если `highlight` про находку, которая уже **закрыта/нормализована** в profile.md — убери его из массива highlights ИЛИ перепиши в положительном ключе («раньше было X, теперь стабильно — продолжаем то, что работает»).
2. Если `highlight` про находку, которая уже **в работе** (начата терапия, ждём контроля) — оставь, но добавь контекст в `body`: «продолжаем с прошлого месяца», «уже на контроле».
3. Если в `action_plan` есть шаг, который **уже выполнен** по профилю — убери его из массива.
4. Если в `action_plan` есть шаг, который **уже запланирован** в профиле — оставь, но в `action` упомяни «как и было запланировано».
5. Если ничего не пересекается — верни отчёт без изменений.

## ЧТО НЕЛЬЗЯ

- Никогда не выдумывай, что в profile.md написано. Опирайся только на переданный текст.
- Никогда не добавляй новых находок — это работа предыдущего этапа синтеза, не твоя.
- Никогда не убирай НЕ-разобранные находки — только подтверждённо повторяющиеся.
- Не меняй структуру JSON и не переименовывай поля.

## ВЫХОД — СТРОГО

ВСЕГДА возвращай **полный отчёт целиком** в той же JSON-схеме, что был на входе. Никогда не возвращай пустой ответ, никогда не возвращай только текст-комментарий, никогда не возвращай только список корректировок. Если корректировок нет — всё равно верни весь отчёт целиком, идентичный входу, плюс служебное поле `_historical_check` в конце.

Без markdown-обёртки. Только чистый JSON. Начинай ответ с `{{` и заканчивай `}}`.

В конец JSON добавь служебное поле `"_historical_check"`:
{{
  "adjustments": [
    {{"target": "highlights[1]", "action": "removed | rewritten | kept", "reason": "одна фраза"}}
  ],
  "summary": "одной фразой: что и почему изменил, или 'без изменений'"
}}

Если корректировок нет — `"adjustments": []`, `"summary": "без изменений"`. И при этом весь остальной отчёт всё равно копируется на выход."""


async def historical_check(report: dict) -> dict:
    """Пропускает report через проверку против profile.md, возвращает откорректированный JSON."""
    relevant_dirs = _relevant_specialist_dirs(report)
    profiles = load_specialist_profiles(only=relevant_dirs if relevant_dirs else None)
    if not profiles or profiles.startswith("("):
        log.info("Profile.md недоступны — пропускаю historical-check")
        return report
    log.info(
        "Historical-check: загружены профили %s",
        relevant_dirs if relevant_dirs else "все",
    )

    user_msg = (
        f"## ПРОФИЛИ СПЕЦИАЛИСТОВ\n\n{profiles}\n\n"
        f"## СГЕНЕРИРОВАННЫЙ ОТЧЁТ (JSON)\n\n```json\n{json.dumps(report, ensure_ascii=False, indent=2)}\n```\n\n"
        "Проверь по правилам в системной инструкции. Верни весь отчёт целиком в JSON, "
        "со своими корректировками или без них (всё равно весь). Без markdown."
    )

    system_blocks = [
        {"type": "text", "text": HISTORICAL_CHECK_SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]

    raw_text = ""
    try:
        def _call_sync() -> str:
            response = _get_client().messages.create(
                model=CHECK_MODEL,
                max_tokens=8000,
                system=system_blocks,
                messages=[{"role": "user", "content": user_msg}],
            )
            return response.content[0].text

        raw_text = await asyncio.to_thread(_call_sync)
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        text = text.strip()
        if not text:
            log.warning("Historical-check: модель вернула пустой ответ; возвращаю оригинал")
            return {**report, "_historical_check_failed": True}
        result = json.loads(text)
        adj = result.get("_historical_check", {}).get("adjustments", []) or []
        summary = result.get("_historical_check", {}).get("summary", "")
        log.info("Historical-check: %d корректировок (%s)", len(adj), summary)
        return result
    except Exception as e:
        # Логируем сырой ответ (первые 800 симв) — чтоб видеть, что модель реально прислала
        snippet = (raw_text or "")[:800].replace("\n", "⏎")
        log.error("Historical-check упал: %s | RAW (800c): %s", e, snippet, exc_info=False)
        # Помечаем, что сверка с историей не отработала: раньше падение
        # было молчаливым и уже закрытая находка могла снова напугать
        # (аудит 2026-05-16, Тема 2, B4). Маркер виден в сохранённом JSON.
        return {**report, "_historical_check_failed": True}
