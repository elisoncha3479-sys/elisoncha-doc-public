"""
bot/ocr_validator.py — OCR-валидатор для live-flow.

Принимает распознанный OCR-текст, сверяет численные значения с
`data/reference-ranges.yaml`, возвращает структурированный список флагов.
Не модифицирует файлы — это лёгкий runtime-валидатор перед синтезом.

— HARD-флаг: физиологически невозможное значение (Hb=20, MPV=92).
  Бот не генерирует отчёт, просит переснять конкретный участок.
— SOFT-флаг: подозрительное по OCR-эвристике, но в пределах живого.
  Передаётся в synthesis как контекст. Главврач решит, как упомянуть.

Принцип: не клиника, только OCR-гигиена. «Высокий ЛПНП» — не наша зона.
«MPV=92 fL» — наша.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent
RANGES_FILE = PROJECT_DIR / "data" / "reference-ranges.yaml"
# Cost-tiering (2026-05-30): механический шаг (валидация OCR) → Haiku по
# умолчанию (3–5× дешевле). Откат: env VALIDATOR_MODEL=claude-sonnet-4-6.
VALIDATOR_MODEL = os.environ.get("VALIDATOR_MODEL", "claude-haiku-4-5-20251001")

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _load_reference_ranges() -> str:
    if not RANGES_FILE.exists():
        log.warning("data/reference-ranges.yaml не найден — валидатор отключён")
        return ""
    try:
        return RANGES_FILE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Не прочла reference-ranges: %s", e)
        return ""


VALIDATOR_SYSTEM = """Ты — OCR-валидатор для медицинских документов пациентки. Твоя задача: пройтись по сырому OCR-тексту и пометить значения, которые скорее всего являются ошибками распознавания, а не реальной клиникой.

ГЛАВНОЕ ПРАВИЛО: ты НЕ ставишь диагнозы и НЕ оцениваешь «высокий/низкий» в клиническом смысле. Ты ловишь только «не бывает у живого человека» и «единицы перепутаны».

Используй справочник `data/reference-ranges.yaml` (передан в user-сообщении). У каждого показателя:
- `physiological_impossible` — границы жизни. Выход = 🔴 HARD флаг.
- `patient_adjusted` / `default_*` — клинический диапазон. Выход — НЕ твой флаг (это разберёт главврач).
- `ocr_suspicion[]` — конкретные эвристики срабатывания (потерянный десятичный, неверные единицы и т.д.).
- `alt_units` с factor — если без единиц и значение похоже на другие единицы.

КАСКАД ПРОВЕРОК:
1. Значение вне `physiological_impossible` → 🔴 **HARD**.
2. Срабатывает правило из `ocr_suspicion` → 🟡 **SOFT**.
3. Значение похоже на `alt_units` (юниты не указаны явно) → 🟡 **SOFT**.
4. Значение просто «вне нормы», но в пределах physiological_impossible → НЕ флаг. Молчи.

Если показатель встретился в тексте, но его нет в YAML — добавь в `indicators_unknown` (для информации, не флаг).

ВЕРНИ СТРОГО JSON (без markdown-обёртки):
{
  "flags": [
    {
      "label": "Гемоглобин",
      "value": "значение из текста как есть",
      "unit": "единица из текста или пустая строка",
      "expected_range": "ожидаемый диапазон в человеческой форме",
      "severity": "hard" | "soft",
      "hypothesis": "одна фраза: что вероятно случилось при OCR"
    }
  ],
  "indicators_found": ["список всех численных показателей, что увидел"],
  "indicators_unknown": ["показатели, которых нет в YAML"]
}

Если флагов нет — `"flags": []`. Всегда верни валидный JSON."""


async def validate_ocr_text(ocr_text: str) -> dict:
    """Принимает сырой OCR-текст, возвращает dict с флагами.

    YAML-справочник кладём в system как cached-блок — стабильно и
    тяжеловесно, повторные валидации платят 10% на этот блок (5 мин кэш).
    """
    empty = {
        "flags": [],
        "indicators_found": [],
        "indicators_unknown": [],
        "any_hard": False,
        "any_soft": False,
    }
    if not ocr_text or not ocr_text.strip():
        return empty

    ranges = _load_reference_ranges()
    if not ranges:
        return empty

    # Стабильная часть системного промпта + YAML-справочник — кэшируется.
    system_text = (
        f"{VALIDATOR_SYSTEM}\n\n"
        f"## СПРАВОЧНИК reference-ranges.yaml\n\n"
        f"```yaml\n{ranges}\n```"
    )
    system_blocks = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    user_msg = (
        f"## OCR-ТЕКСТ ДОКУМЕНТА\n\n{ocr_text[:8000]}\n\n"
        "Пройдись по тексту, извлеки все численные показатели, проверь по каскаду. "
        "Верни JSON по схеме из системной инструкции."
    )

    def _call_sync() -> str:
        response = _get_client().messages.create(
            model=VALIDATOR_MODEL,
            max_tokens=2000,
            system=system_blocks,
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text

    try:
        text = await asyncio.to_thread(_call_sync)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        result = json.loads(text)
        flags = result.get("flags", []) or []
        result["any_hard"] = any(f.get("severity") == "hard" for f in flags)
        result["any_soft"] = any(f.get("severity") == "soft" for f in flags)
        result.setdefault("indicators_found", [])
        result.setdefault("indicators_unknown", [])
        log.info(
            "OCR-validator: %d флагов (hard=%s, soft=%s)",
            len(flags), result["any_hard"], result["any_soft"],
        )
        return result
    except Exception as e:
        log.error("OCR-validator упал: %s", e, exc_info=True)
        empty["error"] = str(e)
        return empty


def format_hard_flag_message(result: dict) -> str:
    """Текст в чат, когда есть HARD-флаги — просьба переснять."""
    hard = [f for f in result.get("flags", []) if f.get("severity") == "hard"]
    if not hard:
        return ""
    lines = [
        "Документ получили, но в нём несколько значений выглядят так, будто OCR не справился. "
        "Перепроверьте, пожалуйста, и при возможности пришлите снимок ещё раз — этот участок крупнее, при хорошем свете и без бликов.",
        "",
        "Какие значения вызывают сомнение:",
    ]
    for f in hard:
        label = f.get("label") or "показатель"
        value = f.get("value") or "?"
        line = f"— {label}: распозналось как «{value}»"
        if f.get("expected_range"):
            line += f" (для живого человека ожидается {f['expected_range']})"
        lines.append(line)
    lines.append("")
    lines.append("Как только пришлёте чёткий снимок — мы сразу обработаем и вернёмся с разбором.")
    return "\n".join(lines)


def format_soft_flags_for_synthesis(result: dict) -> str:
    """Контекст для synthesis-промпта, когда есть SOFT-флаги."""
    soft = [f for f in result.get("flags", []) if f.get("severity") == "soft"]
    if not soft:
        return ""
    lines = [
        "## OCR-ВАЛИДАТОР: подозрительные значения",
        "",
        "Эти значения распознались, но валидатор поднял сомнение в качестве OCR. "
        "Это не клиника, а гигиена данных. Учти при формировании отчёта:",
        "",
    ]
    for f in soft:
        label = f.get("label") or "показатель"
        value = f.get("value") or "?"
        hyp = f.get("hypothesis") or ""
        lines.append(f"— **{label}** = «{value}». {hyp}")
    lines.append("")
    lines.append(
        "Как использовать в отчёте: для этих значений в `lab_tables.rows` ставь `flag: \"warning\"` "
        "и в `comment` мягко отметь «значение требует перепроверки из-за качества снимка». "
        "В `after_lab_summary` упомяни одной фразой: «несколько значений выглядят спорно из-за "
        "качества снимка, имеет смысл уточнить при следующем визите врача» — без алармизма."
    )
    return "\n".join(lines)
