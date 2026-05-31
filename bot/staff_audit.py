"""
Auto staff audit — модуль A2 из плана «Auto staff audit».

Сканирует папки `specialists/<slug>/`, сравнивает с реестром специалистов из
`agents/*.md` (через `discover_specialists`), находит «теневые» папки —
те, где документы накапливаются, но отдельного `agents/<slug>.md` нет.

Дальше `propose_new_specialist()` через LLM формирует заявку: нужен ли
новый специалист, и если да — черновик `agents/<slug>.md` по структуре
существующих агентов. Запись на диск, рассылку в Telegram и approve-flow
делают A3 (встройка в monthly digest) и A4 (telegram-команды).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic

from agents import AGENTS_DIR, discover_specialists
from context import IDENTITY_RULES

DRAFT_SUFFIX = ".md.draft"

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent
SPECIALISTS_DIR = PROJECT_DIR / "specialists"

# Папки внутри specialists/ которые НЕ являются теневыми кандидатами.
# uncategorized — это «OCR не понял к кому отнести», не повод заводить специалиста.
# Префикс «_» (как _excluded) — наша конвенция «не трогать».
EXCLUDED_DIR_NAMES = {"uncategorized"}

RECENT_WINDOW_DAYS = 90

REFERENCE_AGENT_SLUG = "cardiologist"


def collect_stats(specialists_dir: Path = None, agents_dir: Path = None) -> dict:
    """Возвращает {slug: stats} по всем папкам specialists/<slug>/.

    Каждая запись:
        total          — всего .md документов (без profile.md)
        recent_90d     — из них за последние 90 дней (по mtime)
        last_doc_date  — ISO-дата последнего изменения (или None)
        has_agent_md   — есть ли соответствующий agents/<slug>.md в реестре

    Папки с префиксом '_' и из EXCLUDED_DIR_NAMES пропускаются.
    Параметры — для тестов с tmp_path.
    """
    sdir = specialists_dir if specialists_dir is not None else SPECIALISTS_DIR
    adir = agents_dir if agents_dir is not None else AGENTS_DIR

    registered = set(discover_specialists(adir).values())
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)

    stats: dict = {}
    if not sdir.exists():
        return stats

    for path in sorted(sdir.iterdir()):
        if not path.is_dir():
            continue
        name = path.name
        if name.startswith("_") or name in EXCLUDED_DIR_NAMES:
            continue

        per_doc = [
            f for f in path.iterdir()
            if f.is_file() and f.suffix == ".md" and f.name != "profile.md"
        ]
        total = len(per_doc)
        recent = 0
        last_dt: Optional[datetime] = None
        for f in per_doc:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime >= recent_cutoff:
                recent += 1
            if last_dt is None or mtime > last_dt:
                last_dt = mtime

        stats[name] = {
            "slug": name,
            "total": total,
            "recent_90d": recent,
            "last_doc_date": last_dt.date().isoformat() if last_dt else None,
            "has_agent_md": name in registered,
        }
    return stats


def find_shadow_specialists(stats: dict, min_docs: int = 3) -> list:
    """Возвращает slug'и теневых папок: есть документы, нет agents/<slug>.md.
    Сортировка по убыванию количества документов — самые «зрелые» первыми."""
    shadows = [
        s for s in stats.values()
        if not s["has_agent_md"] and s["total"] >= min_docs
    ]
    shadows.sort(key=lambda s: s["total"], reverse=True)
    return [s["slug"] for s in shadows]


# ---------- LLM-предложение ----------

PROPOSE_SYSTEM_PROMPT = """Ты — старший координатор команды «Elisoncha Doc». Раз в месяц
анализируешь накопление документов в теневых папках specialists/<slug>/ —
тех, у которых нет соответствующего agents/<slug>.md.

Твоя задача для одной теневой папки: решить, нужен ли отдельный профильный
специалист, и если да — подготовить черновик agents/<slug>.md по структуре
существующих агентов команды.

Критерий «нужен»:
- тема устойчиво повторяется (≥ 3 документов),
- требует профильной экспертизы,
- текущее распределение по существующим специалистам не покрывает её адекватно.

Текущий состав команды передан тебе в user-сообщении (список доступных слотов).

Внутри draft_agent_md НЕ упоминай пациентку, имена, конкретные диагнозы или
дозировки. Это шаблон роли, а не персонализированный документ. Структура должна
повторять референс из user-сообщения (Роль / Зона ответственности /
Источники фактуры / Принципы работы / Контракт оркестрации / Запрещено /
Формат ответа). Обязательно включи YAML-frontmatter в начало draft_agent_md:

    ---
    slug: <slug>
    name_ru: <русское название в нижнем регистре>
    role: specialist
    domain: <через запятую, ключевые зоны компетенции>
    ---

Верни ТОЛЬКО JSON (без ```-обёртки), строго по схеме:
{
  "needed": true,
  "slug": "neurologist",
  "name_ru": "невролог",
  "reasoning": "одна-две фразы — почему нужен или почему нет",
  "doc_count": 16,
  "last_doc_date": "2026-04-04",
  "draft_agent_md": "---\\nslug: neurologist\\n...---\\n\\n# Невролог\\n\\n## Роль\\n..."
}

Если не нужен — needed=false, draft_agent_md можно опустить или вернуть пустую строку.
"""


def _load_reference_agent(slug: str = REFERENCE_AGENT_SLUG) -> str:
    """Возвращает текст референсного agents/<slug>.md (для подсказки структуры LLM)."""
    path = AGENTS_DIR / f"{slug}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _build_user_message(slug: str, stats_entry: dict, existing_specialists: list) -> str:
    reference = _load_reference_agent()
    return (
        f"## Теневая папка для разбора\n\n"
        f"- slug: `{slug}`\n"
        f"- путь: `specialists/{slug}/`\n"
        f"- всего документов: {stats_entry['total']}\n"
        f"- за последние 90 дней: {stats_entry['recent_90d']}\n"
        f"- дата последнего документа: {stats_entry['last_doc_date']}\n\n"
        f"## Текущий состав команды (доступные слоты)\n\n"
        f"{', '.join(existing_specialists)}\n\n"
        f"## Референс структуры agents/*.md\n\n"
        f"```markdown\n{reference}\n```\n\n"
        f"Прими решение и верни JSON по схеме."
    )


def propose_new_specialist(
    slug: str,
    stats: dict,
    client: Optional[anthropic.Anthropic] = None,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """LLM-вызов: нужен ли отдельный специалист для теневой папки `slug`.

    Возвращает dict по схеме PROPOSE_SYSTEM_PROMPT. На диск ничего не пишет.

    Raises:
        ValueError: если slug не в stats или не является теневой папкой.
        json.JSONDecodeError: если LLM вернул не-JSON.
    """
    entry = stats.get(slug)
    if entry is None:
        raise ValueError(f"slug={slug!r} нет в stats")
    if entry["has_agent_md"]:
        raise ValueError(f"slug={slug!r} уже зарегистрирован в agents/")

    if client is None:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    existing = sorted(discover_specialists().keys())
    user_msg = _build_user_message(slug, entry, existing)

    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=[
            {"type": "text", "text": IDENTITY_RULES, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": PROPOSE_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.error("propose_new_specialist: не JSON. raw=%s", text[:500])
        raise


# ---------- Draft I/O для approve-flow A4 ----------

def draft_path(slug: str, agents_dir: Path = None) -> Path:
    return (agents_dir if agents_dir is not None else AGENTS_DIR) / f"{slug}{DRAFT_SUFFIX}"


def final_path(slug: str, agents_dir: Path = None) -> Path:
    return (agents_dir if agents_dir is not None else AGENTS_DIR) / f"{slug}.md"


def save_draft(slug: str, draft_md: str, agents_dir: Path = None) -> Path:
    """Пишет agents/<slug>.md.draft. Перезаписывает существующий, если есть."""
    path = draft_path(slug, agents_dir)
    path.write_text(draft_md, encoding="utf-8")
    return path


def read_draft(slug: str, agents_dir: Path = None) -> Optional[str]:
    path = draft_path(slug, agents_dir)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def approve_draft(slug: str, agents_dir: Path = None) -> Path:
    """Переименовывает .md.draft → .md атомарно.

    Raises:
        FileNotFoundError: если draft отсутствует.
        FileExistsError: если agents/<slug>.md уже существует (никаких silent merge).
    """
    src = draft_path(slug, agents_dir)
    dst = final_path(slug, agents_dir)
    if not src.exists():
        raise FileNotFoundError(f"draft {src.name} не найден")
    if dst.exists():
        raise FileExistsError(
            f"{dst.name} уже существует — approve отказан, чтобы не затереть"
        )
    src.rename(dst)
    return dst


def reject_draft(slug: str, agents_dir: Path = None) -> bool:
    """Удаляет .md.draft. Возвращает True если файл был и удалён."""
    path = draft_path(slug, agents_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


# ---------- Оркестратор A3 ----------

def audit_and_propose(
    min_docs: int = 3,
    client: Optional[anthropic.Anthropic] = None,
    specialists_dir: Path = None,
    agents_dir: Path = None,
    propose_fn=None,
) -> Optional[dict]:
    """Один цикл аудита: stats → shadow → LLM-предложение → сохранение draft.

    Если есть теневая папка с ≥ min_docs документов и для неё ещё нет draft'а,
    зовёт LLM (propose_fn по умолчанию = propose_new_specialist). Если LLM
    говорит needed=True — сохраняет draft на диск и возвращает dict с
    полями для уведомления в чат.

    Если ничего не нужно — возвращает None.

    Возвращаемый dict:
        {
            "slug": str,
            "name_ru": str,
            "reasoning": str,
            "doc_count": int,
            "last_doc_date": str | None,
            "draft_path": Path,
        }

    Параметры propose_fn / specialists_dir / agents_dir — для тестов
    (можно подсунуть мок LLM).
    """
    stats = collect_stats(specialists_dir, agents_dir)
    shadows = find_shadow_specialists(stats, min_docs=min_docs)
    if not shadows:
        log.info("audit_and_propose: теневых папок нет")
        return None

    slug = shadows[0]

    # Не дёргаем LLM повторно, если draft уже лежит на диске —
    # значит предыдущий audit уже отработал, ждём approve/reject.
    if draft_path(slug, agents_dir).exists():
        log.info("audit_and_propose: draft для %s уже существует, пропускаю", slug)
        return None

    fn = propose_fn if propose_fn is not None else propose_new_specialist
    proposal = fn(slug, stats, client=client) if client is not None else fn(slug, stats)

    if not proposal.get("needed"):
        log.info(
            "audit_and_propose: LLM решил, что %s не нужен. Reasoning: %s",
            slug, proposal.get("reasoning", "(пусто)"),
        )
        return None

    draft_md = proposal.get("draft_agent_md") or ""
    if not draft_md.strip():
        log.warning("audit_and_propose: needed=True, но draft_agent_md пуст для %s", slug)
        return None

    path = save_draft(slug, draft_md, agents_dir)
    return {
        "slug": proposal.get("slug", slug),
        "name_ru": proposal.get("name_ru", slug),
        "reasoning": proposal.get("reasoning", ""),
        "doc_count": proposal.get("doc_count", stats[slug]["total"]),
        "last_doc_date": proposal.get("last_doc_date", stats[slug]["last_doc_date"]),
        "draft_path": path,
    }


def format_proposal_message(proposal: dict) -> str:
    """Сообщение в группу: предложение нового специалиста + команды approve/reject."""
    return (
        f"🩺 *Главврач предлагает ввести нового специалиста: {proposal['name_ru']}*\n\n"
        f"Обоснование: {proposal['reasoning']}\n\n"
        f"Накоплено документов: {proposal['doc_count']}\n"
        f"Последний документ: {proposal['last_doc_date'] or '—'}\n\n"
        f"Команды:\n"
        f"• `/show_draft {proposal['slug']}` — посмотреть черновик agents/{proposal['slug']}.md\n"
        f"• `/approve_specialist {proposal['slug']}` — подключить (.draft → .md)\n"
        f"• `/reject_specialist {proposal['slug']}` — отклонить"
    )
