"""bot/cost_log.py — приблизительный лог стоимости LLM-вызовов.

Цель: видеть, сколько денег уходит на единицу работы (документ), а не
гадать по счёту в конце месяца (повод — траты Anthropic API > $100/мес,
почти всё на Sonnet по умолчанию; cost-tiering 2026-05-30).

Цены — $/млн токенов (вход/выход), ориентир на 2026-05. Кэш-чтение
считаем по входной цене (лёгкая переоценка — для ориентира достаточно).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# $/1M токенов: (input, output)
_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-opus-4-8": (15.0, 75.0),
}
_DEFAULT = (3.0, 15.0)  # неизвестная модель — считаем консервативно как Sonnet


def estimate_usd(model: str, in_tokens: int, out_tokens: int) -> float:
    in_p, out_p = _PRICES.get(model, _DEFAULT)
    return in_tokens / 1_000_000 * in_p + out_tokens / 1_000_000 * out_p


def log_call(tag: str, model: str, response) -> float:
    """Логирует токены и примерную цену одного вызова. Возвращает USD.
    Best-effort: при любой ошибке чтения usage — тихо 0.0 (на пайплайн
    влиять не должен)."""
    try:
        u = response.usage
        in_t = int(getattr(u, "input_tokens", 0) or 0)
        out_t = int(getattr(u, "output_tokens", 0) or 0)
        cache_r = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        usd = estimate_usd(model, in_t + cache_r, out_t)
        log.info(
            "COST %s model=%s in=%d cache=%d out=%d ~$%.4f",
            tag, model, in_t, cache_r, out_t, usd,
        )
        return usd
    except Exception as e:  # noqa: BLE001 — лог затрат не должен ронять пайплайн
        log.warning("cost_log: не смог посчитать usage (%s)", e)
        return 0.0
