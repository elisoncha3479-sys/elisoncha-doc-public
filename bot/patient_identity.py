"""bot/patient_identity.py — единый источник идентичности пациента.

Публичная версия НЕ хранит ничьих личных данных в коде. Кто пациент —
определяется при первом запуске через онбординг-анкету и/или переменными
окружения. Пока ничего не задано — бот общается тепло, но без имени.

Приоритет источников:
  1. переменные окружения (PATIENT_*) — побеждают всегда;
  2. файл персоны data/patient_persona.json (пишет онбординг) —
     действует сразу, без рестарта;
  3. нейтральные значения (никаких зашитых в код реальных людей).

Переменные окружения:
  PATIENT_FULL_NAME      — полное ФИО («Фамилия Имя Отчество»)
  PATIENT_ADDRESS_NAME   — как обращаться вслух («Имя Отчество»)
  PATIENT_DOB            — дата рождения (свободный формат)
  PATIENT_CITY           — город
  PATIENT_GENDER         — "f" | "m"
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PERSONA_JSON = Path(__file__).resolve().parent.parent / "data" / "patient_persona.json"

_cache: dict | None = None
_cache_key: tuple | None = None


def _invalidate_cache() -> None:
    """Сбросить кеш персоны (онбординг записал новый файл / тесты)."""
    global _cache, _cache_key
    _cache, _cache_key = None, None


def _persona() -> dict:
    """Содержимое data/patient_persona.json или {}. Кешируется по mtime."""
    global _cache, _cache_key
    try:
        st = PERSONA_JSON.stat()
        key = (str(PERSONA_JSON), st.st_mtime_ns)
    except OSError:
        _cache, _cache_key = {}, None
        return _cache
    if _cache_key == key and _cache is not None:
        return _cache
    try:
        data = json.loads(PERSONA_JSON.read_text(encoding="utf-8"))
        _cache = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _cache = {}
    _cache_key = key
    return _cache


def _resolve(env_var: str, persona_key: str) -> str:
    val = os.environ.get(env_var, "").strip()
    if val:
        return val
    pv = _persona().get(persona_key, "")
    return pv.strip() if isinstance(pv, str) else ""


def full_name() -> str:
    """Полное ФИО или пусто. Используется охранником на входе."""
    return _resolve("PATIENT_FULL_NAME", "full_name")


def address_name() -> str:
    """Имя-отчество для обращения вслух. Пусто → обращаемся без имени."""
    name = _resolve("PATIENT_ADDRESS_NAME", "address_name")
    if name:
        return name
    parts = full_name().split()
    if len(parts) >= 3:
        return f"{parts[1]} {parts[2]}"
    return ""


def dob() -> str:
    return _resolve("PATIENT_DOB", "dob")


def city() -> str:
    return _resolve("PATIENT_CITY", "city")


def is_female() -> bool:
    """Грамматический род для «пациент/пациентка». По умолчанию женский,
    переопределяется env/файлом персоны."""
    g = _resolve("PATIENT_GENDER", "gender") or "f"
    return g.lower() != "m"


def patient_noun() -> str:
    return "пациентка" if is_female() else "пациент"


def greeting(emoji: str = "") -> str:
    """«Здравствуйте, Имя Отчество» или «Здравствуйте» — если имя не задано."""
    name = address_name()
    base = f"Здравствуйте, {name}" if name else "Здравствуйте"
    return f"{base} {emoji}".strip() if emoji else base


def address(fallback: str = "Добрый день") -> str:
    """Обращение в начале сообщения: имя-отчество или нейтральный fallback."""
    return address_name() or fallback


def descriptor() -> str:
    """Строка-описание пациента для шапок/примеров. Без личных данных,
    если ничего не задано."""
    bits = [full_name() or patient_noun().capitalize()]
    if dob():
        bits.append(f"д.р. {dob()}")
    if city():
        bits.append(city())
    return ", ".join(bits)
