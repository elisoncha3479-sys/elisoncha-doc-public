"""bot/meds_section.py — раздел 4 «Лекарства» из profile.md специалиста.

Единый источник правды (backlog #4): и reconcile.py (вход для сверки), и
profile_refresher.py (smart-фильтр hook'а) используют один экстрактор, без
дублирования логики и без затягивания telegram-зависимостей в refresher.
"""
from __future__ import annotations

import re


def extract_meds_section(profile_text: str) -> str:
    """Вытаскивает раздел 4 «ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ» из profile.md.
    Это блок от заголовка `## 4.` до следующего `## ` (или конца файла).
    Если раздела нет — возвращает пустую строку."""
    lines = profile_text.splitlines()
    in_section = False
    out: list[str] = []
    for ln in lines:
        if not in_section:
            if re.match(r"^##\s*4\.", ln) and "ЛЕКАРСТВ" in ln.upper():
                in_section = True
                out.append(ln)
            continue
        # Вышли в следующий раздел верхнего уровня
        if re.match(r"^##\s*\d", ln) or re.match(r"^---\s*$", ln):
            break
        out.append(ln)
    return "\n".join(out).strip()


def _normalize(section: str) -> str:
    """Схлопывает незначащие различия: лишние пробелы внутри строки, пустые
    строки, хвостовые пробелы. Так косметическая перегенерация LLM не считается
    изменением схемы лекарств."""
    norm_lines = []
    for ln in section.splitlines():
        collapsed = re.sub(r"\s+", " ", ln).strip()
        if collapsed:
            norm_lines.append(collapsed)
    return "\n".join(norm_lines)


def meds_section_changed(old_profile_text: str, new_profile_text: str) -> bool:
    """True, если раздел 4 «Лекарства» содержательно изменился между двумя
    версиями profile.md. Косметика (пробелы, пустые строки) изменением не
    считается. Оба профиля без раздела 4 → не изменилось."""
    old_norm = _normalize(extract_meds_section(old_profile_text))
    new_norm = _normalize(extract_meds_section(new_profile_text))
    return old_norm != new_norm
