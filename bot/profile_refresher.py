"""
bot/profile_refresher.py — авто-refresh specialists/<spec>/profile.md
после добавления per-doc .md в папку специалиста.

Имитирует логику commands/refresh-profile.md, но через один LLM-вызов на
специалиста. Входы: agent.md (роль), все per-doc .md (кроме profile.md),
текущий profile.md. Выход — перезаписанный profile.md.

Стабильная часть system-промпта (agent.md + REFRESH_INSTRUCTION) кэшируется
через cache_control — повторные refresh за 5 мин платят 10% от input.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, NamedTuple, Optional

import anthropic

from agents import discover_specialists
from meds_section import meds_section_changed
from profile_template import PROFILE_SKELETON

log = logging.getLogger(__name__)


class RefreshResult(NamedTuple):
    """Итог refresh одного специалиста. meds_changed=True → раздел 4
    «Лекарства» содержательно изменился (нужен reconcile, backlog #4)."""

    path: Path
    meds_changed: bool

PROJECT_DIR = Path(__file__).parent.parent
SPECIALISTS_DIR = PROJECT_DIR / "specialists"
AGENTS_DIR = PROJECT_DIR / "agents"

# Cost-tiering (2026-05-30): механический шаг (переписать память врача) → Haiku.
# Откат: env PROFILE_REFRESHER_MODEL=claude-sonnet-4-6.
REFRESHER_MODEL = os.environ.get("PROFILE_REFRESHER_MODEL", "claude-haiku-4-5-20251001")
# Сколько свежих per-doc файлов передавать в refresh. Слишком много = долго и
# дорого, слишком мало = теряется история. 12 — компромисс на ~1.5 года активности.
REFRESH_PERDOC_LIMIT = int(os.environ.get("REFRESH_PERDOC_LIMIT", "12"))

# Бюджет вывода LLM. 8000 обрезал полиморбидный 8-секционный свод после
# раздела «Лекарства» (bug-011): профиль кардиолога терял секции 5–8.
# 16000 даёт запас под полный свод; конфигурируется через env.
REFRESH_MAX_TOKENS = int(os.environ.get("PROFILE_REFRESHER_MAX_TOKENS", "16000"))

# specialists/<dir> совпадает со slug агента (agents/<slug>.md). Набор валидных
# slug'ов берётся из динамического реестра agents/*.md, а не из захардкоженного
# словаря (backlog #2): следующий approve'нутый специалист подхватывается без
# правки Python. Резолвится при каждом вызове — approve нового специалиста
# срабатывает без перезапуска бота (как в agents.run_multi_agent).
def _known_specialist_slugs(agents_dir=None) -> set:
    return set(discover_specialists(agents_dir).values())

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _backup_existing(profile_path: Path) -> Optional[Path]:
    """Перед перезаписью кладёт текущий profile.md в profile.md.bak —
    последняя-известная-хорошая версия. Защита от плохой регенерации в бою:
    refresh — полная переписка, и без бэкапа откатывать было бы нечем.
    Если файла ещё нет — бэкапить нечего, возвращаем None."""
    if not profile_path.exists():
        return None
    bak = profile_path.with_name(profile_path.name + ".bak")
    try:
        shutil.copyfile(profile_path, bak)
        return bak
    except OSError as exc:
        log.warning("Не удалось сделать бэкап %s: %s", profile_path, exc)
        return None


_HEADER_RE = re.compile(r"^\*\*Последнее обновление:\*\*[^\n]*$", re.MULTILINE)
_FOOTER_RE = re.compile(r"^_Обновлено автоматически:[^\n]*_\s*$", re.MULTILINE)


def _stamp_timestamp(text: str, now_str: str) -> str:
    """Проставляет реальную дату обновления программно. LLM не знает
    текущего времени и выдумывал её (баг-приёмка 2026-05-15); причём эту
    дату из шапки reconcile читает как сигнал свежести профиля.

    - Шапка `**Последнее обновление:** …` → ставим now_str (или добавляем
      сразу после первого `# ` заголовка, если шапки нет).
    - Любой footer `_Обновлено автоматически: …_` убираем и добавляем
      ровно один канонический в конце.
    """
    if _HEADER_RE.search(text):
        text = _HEADER_RE.sub(f"**Последнее обновление:** {now_str}", text, count=1)
    else:
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("# "):
                lines.insert(i + 1, f"**Последнее обновление:** {now_str}")
                break
        text = "\n".join(lines)

    text = _FOOTER_RE.sub("", text).rstrip()
    return f"{text}\n\n_Обновлено автоматически: {now_str}_\n"


# REFRESH_INSTRUCTION встраивает ЯВНЫЙ канонический каркас (PROFILE_SKELETON,
# единый источник из commands/refresh-profile.md), а не описывает структуру
# прозой. Иначе LLM сочиняет произвольную разбивку и теряет `## 4. ЛЕКАРСТВА`,
# на которой держится smart-фильтр reconcile (#4).
REFRESH_INSTRUCTION = (
    """ЗАДАЧА: переписать profile.md полностью как актуальный свод по пациентке.

Это **чистая перезапись**, не дописывание в конец. Один источник правды без хвостов.

Используй:
- Текущий profile.md — как уже проделанную клиническую аналитику (память).
- Все per-doc .md из твоей папки — как фактуру (свежие документы и их разбор).

Принципы:
— Если новый per-doc делает старую запись неактуальной — старую убираешь, заменяешь свежим фактом.
— Если новые данные противоречат старым — пометь, что обсудить с лечащим врачом.
— Не выдумывай данных, которых нет ни в per-doc, ни в старом profile.md.
— Дату «Последнее обновление» НЕ заполняй и не выдумывай — она
  проставляется программно после генерации (LLM не знает реального времени).

СТРУКТУРА ВЫХОДА: строго следуй шаблону ниже — те же заголовки секций
(`## 1.` … `## 8.`) в том же порядке, дословно. Не переименовывай и не
пропускай секции; если данных по секции нет — оставь секцию с явной пометкой
«нет данных по моей области». Раздел `## 4. ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ`
обязателен всегда.

ВЫХОД: чистый markdown, начинай с `#` заголовка. Без markdown-обёртки, без комментариев.

=== ОБЯЗАТЕЛЬНЫЙ ШАБЛОН profile.md (структура неизменна) ===

"""
    + PROFILE_SKELETON
)


async def refresh_specialist_profile(specialist_dir: str) -> Optional[RefreshResult]:
    """Перезаписывает specialists/<specialist_dir>/profile.md.
    Возвращает RefreshResult(path, meds_changed), либо None при ошибке/skip."""
    spec_path = SPECIALISTS_DIR / specialist_dir
    if not spec_path.exists():
        log.warning("Папка %s не найдена — пропускаю refresh", spec_path)
        return None

    if specialist_dir not in _known_specialist_slugs():
        log.warning("Неизвестный специалист %s — пропускаю refresh", specialist_dir)
        return None
    agent_name = specialist_dir

    agent_md = AGENTS_DIR / f"{agent_name}.md"
    if not agent_md.exists():
        log.warning("agents/%s.md не найден — пропускаю refresh", agent_name)
        return None

    try:
        agent_role = agent_md.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Не прочла %s: %s", agent_md, e)
        return None

    profile_path = spec_path / "profile.md"
    current_profile = ""
    if profile_path.exists():
        try:
            current_profile = profile_path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Не прочла текущий profile.md: %s", e)

    # Берём не все per-doc, а N самых свежих по mtime — чтобы refresh не
    # становился квадратичным по времени с ростом архива.
    candidate_files = [f for f in spec_path.glob("*.md") if f.name != "profile.md"]
    candidate_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    selected = candidate_files[:REFRESH_PERDOC_LIMIT]

    per_docs = []
    for f in selected:
        try:
            content = f.read_text(encoding="utf-8")
            per_docs.append(f"### Файл: {f.name}\n\n{content[:6000]}")
        except Exception as e:
            log.warning("Не прочла %s: %s", f, e)

    if not per_docs:
        log.info("В %s нет per-doc .md — нечего рефрешить", spec_path)
        return None

    per_docs_text = "\n\n---\n\n".join(per_docs)
    if len(candidate_files) > REFRESH_PERDOC_LIMIT:
        per_docs_text += (
            f"\n\n_(показаны {REFRESH_PERDOC_LIMIT} самых свежих per-doc из "
            f"{len(candidate_files)}; более старые учитывать через текущий profile.md)_"
        )

    # System (стабильное — кэш): роль специалиста + инструкция refresh
    system_text = f"{agent_role}\n\n---\n\n{REFRESH_INSTRUCTION}"
    system_blocks = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    user_msg = (
        f"## ТЕКУЩИЙ profile.md\n\n{current_profile if current_profile else '(пустой/новый)'}\n\n"
        f"---\n\n"
        f"## ВСЕ PER-DOC .md ФАЙЛЫ В ТВОЕЙ ПАПКЕ\n\n{per_docs_text}\n\n"
        f"---\n\nПерепиши profile.md полностью."
    )

    log.info("Refresh профиля %s: %d per-doc", specialist_dir, len(per_docs))

    def _call_sync() -> str:
        response = _get_client().messages.create(
            model=REFRESHER_MODEL,
            max_tokens=REFRESH_MAX_TOKENS,
            system=system_blocks,
            messages=[{"role": "user", "content": user_msg}],
        )
        return response.content[0].text

    try:
        new_profile = await asyncio.to_thread(_call_sync)
        new_profile = new_profile.strip()
        # Снимаем markdown-обёртку, если модель её добавила
        if new_profile.startswith("```"):
            lines = new_profile.split("\n")
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            new_profile = "\n".join(lines)
        if not new_profile or len(new_profile) < 200:
            log.warning("Refresh %s вернул подозрительно короткий результат — пропускаю запись", specialist_dir)
            return None
        new_profile = _stamp_timestamp(
            new_profile, datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        meds_changed = meds_section_changed(current_profile, new_profile)
        _backup_existing(profile_path)
        profile_path.write_text(new_profile, encoding="utf-8")
        log.info(
            "Profile %s обновлён (%d симв), раздел «Лекарства» изменён: %s",
            specialist_dir,
            len(new_profile),
            meds_changed,
        )
        return RefreshResult(profile_path, meds_changed)
    except Exception as e:
        log.error("Refresh профиля %s упал: %s", specialist_dir, e, exc_info=False)
        return None


async def refresh_profiles_for_specialists(
    specialist_dirs: List[str],
) -> List[RefreshResult]:
    """Параллельно обновляет profile.md для нескольких специалистов."""
    if not specialist_dirs:
        return []
    tasks = [refresh_specialist_profile(d) for d in specialist_dirs]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, RefreshResult)]
