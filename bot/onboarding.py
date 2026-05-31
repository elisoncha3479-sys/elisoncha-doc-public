"""bot/onboarding.py — знакомство с новым человеком (формат «золотая
середина», только публичная версия).

Бот одним сообщением задаёт 10 вопросов, человек отвечает одним письмом,
один проход модели превращает ответ в профиль персоны:
  - идентичность → data/patient_persona.json (читает patient_identity);
  - психопрофиль + образ жизни → history/PATIENT_PERSONA.md (читают
    специалисты при разборе).

Маме это не нужно (у неё профиль есть) — фича для новых пользователей.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import patient_identity
from agents import AGENTS_DIR, parse_frontmatter

log = logging.getLogger(__name__)

_PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = _PROJECT / "data"
HISTORY_DIR = _PROJECT / "history"
SPECIALISTS_DIR = _PROJECT / "specialists"

# Лаборант подключается ВСЕГДА (правило роутинга главврача) — держим
# его в активном составе независимо от того, что насобирал онбординг.
ALWAYS_ACTIVE = ("lab-analyst",)

ONBOARDING_MODEL = os.environ.get("ONBOARDING_MODEL", "claude-sonnet-4-6")
# Подробный рассказ → длинный JSON персоны. 2000 токенов обрезали
# ответ модели на полуслове (Unterminated string) → откат в пусто
# (баг dogfooding 2026-05-17). Берём с большим запасом, настраиваемо.
ONBOARDING_MAX_TOKENS = int(os.environ.get("ONBOARDING_MAX_TOKENS", "8000"))

ONBOARDING_QUESTIONS = (
    "Привет! Прежде чем я подключу к вам команду врачей, расскажите "
    "немного о себе — это останется только у вас, нужно чтобы врачи "
    "говорили с вами на вашем языке.\n\n"
    "Отвечайте по пунктам, своими словами (что-то пропустить — не "
    "страшно). Можно писать НЕСКОЛЬКИМИ сообщениями, если длинно — "
    "я приму все части. Когда закончите, пришлите одним словом: "
    "ГОТОВО — тогда я всё соберу.\n\n"
    "1. Как вас зовут? (фамилия, имя, отчество — по нему я узнаю ваши "
    "документы)\n"
    "2. Сколько вам лет (или дата рождения), пол?\n"
    "3. Кем работаете или работали? Как обычно проходит ваш день?\n"
    "4. Как вы питаетесь? Есть слабости в еде?\n"
    "5. Сколько двигаетесь — зал, прогулки, или почти не выходите?\n"
    "6. Вредные привычки есть?\n"
    "7. ЗДОРОВЬЕ — самое важное, опишите подробно своими словами: "
    "хронические болезни и аллергии; что оперировано или наблюдается "
    "у врачей; что беспокоит прямо сейчас и чего опасаетесь. По "
    "этому я соберу вашу команду врачей — имена специалистов знать "
    "не нужно, просто расскажите про себя.\n"
    "8. Что принимаете сейчас (лекарства, витамины)?\n"
    "9. Как с вами лучше разговаривать — прямо и по делу, или мягко "
    "и осторожно? Что вас скорее поддержит, а что испугает?\n"
    "10. Вы заполняете для себя или ухаживаете за близким? Если за "
    "близким — расскажите всё выше про него, от его лица."
)

ONBOARDING_SYSTEM_PROMPT = """Ты — приёмный администратор медицинской системы. Человек прислал рассказ о себе в свободной форме (ответ на анкету знакомства).

Сделай из этого профиль персоны и подбери команду врачей. Верни СТРОГО JSON без markdown-обёртки:

{
  "full_name": "Фамилия Имя Отчество (если назвал; иначе пустая строка)",
  "address_name": "Имя Отчество для обращения вслух (или пусто)",
  "dob": "дата рождения как сказал, или пусто",
  "city": "город или пусто",
  "gender": "f или m (по имени/контексту; по умолчанию f)",
  "addressee": "self если человек пишет про себя; caregiver если он ухаживает за близким и описывает близкого",
  "persona_md": "markdown: ## Психологический профиль (характер, как разговаривать, что мотивирует, чего боится) и ## Рекомендации по образу жизни (работа/режим, питание и слабости, движение, привычки, известные диагнозы и аллергии, что принимает, что беспокоит). Только то, что человек сказал — не выдумывай. Тёплый человеческий язык.",
  "specialists": [
    {"slug": "латинский-слаг", "name_ru": "название в нижнем регистре", "domain": "зоны компетенции через запятую", "reason": "по какой проблеме человека"}
  ]
}

Про specialists: по описанным проблемным зонам подбери профильных врачей. Если подходящий есть в БИБЛИОТЕКЕ (список ниже) — используй его slug и name_ru ОТТУДА дословно. Если нужного врача в библиотеке нет — предложи нового: краткий латинский slug (строчные буквы и дефисы), русское название в нижнем регистре, domain. Системные роли (главврач, лаборант, проверяльщик OCR) не перечисляй — они подключаются сами.

Не теряй информацию: всё существенное из рассказа должно попасть в persona_md. Если чего-то не сказал — просто не пиши про это."""


# ---------- состояние диалога (in-memory, на время процесса) ----------

_pending: set[int] = set()
# Буфер частей ответа: длинный рассказ не влезает в одно сообщение
# Telegram (~4096), поэтому принимаем НЕСКОЛЬКО сообщений и склеиваем
# по слову-маркеру (баг dogfooding 2026-05-17).
_answer_parts: dict[int, list[str]] = {}

# Слова/команды завершения онбординга (без регистра, без пробелов)
FINALIZE_MARKERS = {"готово", "/готово", "done", "/done", "/finish"}


def start_onboarding(user_id: int) -> None:
    _pending.add(user_id)
    _answer_parts.pop(user_id, None)


def is_awaiting(user_id: int) -> bool:
    return user_id in _pending


def finish_onboarding(user_id: int) -> None:
    _pending.discard(user_id)
    _answer_parts.pop(user_id, None)


def is_finalize(text: str) -> bool:
    return (text or "").strip().lower() in FINALIZE_MARKERS


def add_answer_part(user_id: int, text: str) -> int:
    """Добавляет часть ответа в буфер. Возвращает число частей."""
    parts = _answer_parts.setdefault(user_id, [])
    if text and text.strip():
        parts.append(text.strip())
    return len(parts)


def pop_answers(user_id: int) -> str:
    """Склеивает все части в один текст и очищает буфер."""
    parts = _answer_parts.pop(user_id, [])
    return "\n\n".join(parts).strip()


# ---------- разбор ответа в персону ----------

def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # убираем первую строку ```… и закрывающий ```
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


def build_persona(client, answer_text: str) -> dict:
    """Один проход модели: свободный ответ → профиль персоны.

    При любой проблеме разбора слова человека НЕ теряются — уходят в
    persona_md, идентичность пустая (бот просто без имени, но контекст
    у врачей есть).
    """
    base = {
        "full_name": "",
        "address_name": "",
        "dob": "",
        "city": "",
        "gender": "f",
        "addressee": "self",
        "persona_md": answer_text.strip(),
        "specialists": [],
    }
    try:
        lib = library_specialists()
        lib_lines = "\n".join(f"- {slug} ({name})"
                              for slug, name in sorted(lib.items()))
        system = (ONBOARDING_SYSTEM_PROMPT
                  + "\n\nБИБЛИОТЕКА АРХЕТИПОВ (используй slug/name_ru "
                    "отсюда дословно, если врач подходит):\n" + lib_lines)
        resp = client.messages.create(
            model=ONBOARDING_MODEL,
            max_tokens=ONBOARDING_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": answer_text.strip()}],
        )
        raw = _strip_fences(resp.content[0].text)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("ожидался JSON-объект")
    except Exception as e:  # noqa: BLE001 — любой сбой → безопасный fallback
        log.warning("build_persona: не разобрала ответ модели (%s) — "
                    "сохраняю слова как есть", e)
        return base

    out = dict(base)
    for k in ("full_name", "address_name", "dob", "city", "gender"):
        v = data.get(k, "")
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    pm = data.get("persona_md", "")
    if isinstance(pm, str) and pm.strip():
        out["persona_md"] = pm.strip()
    if out["gender"].lower() not in ("f", "m"):
        out["gender"] = "f"

    addr = str(data.get("addressee", "")).strip().lower()
    out["addressee"] = addr if addr in ("self", "caregiver") else "self"

    specs = data.get("specialists", [])
    if isinstance(specs, list):
        out["specialists"] = [s for s in specs if isinstance(s, dict)]
    return out


# ---------- библиотека архетипов и сборка состава ----------

def library_specialists(agents_dir: Path | None = None) -> dict:
    """ВСЕ архетипы-специалисты (role=specialist), БЕЗ фильтра активного
    состава: {slug: name_ru}. Это библиотека, из которой онбординг
    подбирает подмножество под конкретного человека."""
    directory = Path(agents_dir) if agents_dir else AGENTS_DIR
    out: dict[str, str] = {}
    for path in sorted(directory.glob("*.md")):
        try:
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if (fm.get("role") == "specialist"
                and fm.get("slug") and fm.get("name_ru")):
            out[fm["slug"]] = fm["name_ru"]
    return out


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _sanitize_slug(raw: str) -> str:
    s = (raw or "").strip().lower().replace(" ", "-")
    s = _SLUG_RE.sub("", s)
    return s.strip("-")


def _skeleton(slug: str, name_ru: str, domain: str) -> str:
    """Строгий общий скелет роли специалиста (без личных данных).
    Структура и «Контракт оркестрации» — как у всех архетипов, чтобы
    реестр и оркестрация работали без правки кода."""
    title = name_ru[:1].upper() + name_ru[1:] if name_ru else slug
    return f"""---
slug: {slug}
name_ru: {name_ru}
role: specialist
domain: {domain}
---

# {title}

## Роль

Профильный специалист-агент по направлению: {domain}. Конкретные диагнозы, цифры и анамнез человека берёт из его профиля и истории — в этой роли их нет.

## Зона ответственности

- Профильные состояния и показатели в рамках: {domain}
- Связь находок своей области с сопутствующей патологией — отметить и эскалировать смежным специалистам

## Источники фактуры о пациенте

- `history/MEDICAL_HISTORY.md` — единый свод диагнозов, лекарств, хронологии
- `history/PATIENT_PERSONA.md` — профиль человека (если есть): что важно учитывать в общении и образе жизни
- `specialists/{slug}/profile.md` — профильный свод и ближайшие задачи
- `inbox/` — первичные документы по направлению

Любые конкретные стадии, цифры, дозы и сроки — оттуда. В этой инструкции их быть не должно.

## Принципы работы

- Перед любой рекомендацией — прочитать `history/MEDICAL_HISTORY.md` полностью и `specialists/{slug}/profile.md`.
- Учитывать сопутствующую патологию и принимаемую терапию при интерпретации.
- Не назначать и не отменять препараты без явного запроса; формулировать как «обсудить с лечащим врачом X».
- При выявлении красного флага — явно помечать его и эскалировать главврачу-агенту.

## Тревожные симптомы (срочная переоценка)

- Острое или быстро прогрессирующее ухудшение в профильной области
- Признаки, требующие неотложной очной помощи

## Контракт оркестрации

**Вход от chief:**

- задача: формулировка вопроса/проблемы по моей области
- ссылка на свой `specialists/{slug}/profile.md`
- по необходимости — документы из `inbox/` или строки из `MEDICAL_HISTORY.md`

**Выход chief'у (стандартная структура):**

- `summary` — короткая оценка ситуации (1–3 предложения)
- `findings[]` — что я увидел, с цитированием источника
- `flags[]` — красные (срочно) и жёлтые (плановое внимание) флаги
- `profile_updates[]` — что дописать/изменить в `profile.md` (через `commands/refresh-profile`)
- `escalations[]` — конфликты или вопросы за пределами компетенции

**Запрещено:**

- Прямая коммуникация с пациентом — любое сообщение пациенту формирует только chief
- Запись в `MEDICAL_HISTORY.md` — это делает chief
- Самостоятельное назначение/отмена препаратов — только «обсудить с лечащим врачом X»
- Чтение чужих профилей без явного запроса от chief

**Обновление profile.md:**

- Только через `commands/refresh-profile`, без прямой записи руками

**Конфликты и недостаток данных:**

- При расхождении с другим специалистом — `escalations[]` `conflict`, chief разрешает
- При недостатке данных — `escalations[]` `data_request` с конкретным запросом

## Формат ответа

- Простым языком, на «Вы», без сюсюканья
- Структура: что вижу → что это значит → что делать → когда это важно/срочно
- Числовые значения и даты — только если уже зафиксированы в истории/профиле и релевантны вопросу
"""


def generate_specialist_file(slug: str, name_ru: str, domain: str,
                             agents_dir: Path) -> Path:
    """Создаёт agents/<slug>.md по строгому скелету, если файла ещё нет."""
    agents_dir = Path(agents_dir)
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{slug}.md"
    if not path.exists():
        path.write_text(_skeleton(slug, name_ru, domain), encoding="utf-8")
        log.info("онбординг: сгенерирован новый архетип %s", slug)
    return path


def _ensure_skeleton_folder(slug: str, specialists_dir: Path) -> None:
    folder = Path(specialists_dir) / slug
    folder.mkdir(parents=True, exist_ok=True)
    keep = folder / ".gitkeep"
    if not keep.exists():
        keep.write_text("", encoding="utf-8")


def resolve_roster(parsed: dict, agents_dir: Path | None = None,
                   specialists_dir: Path | None = None) -> list:
    """Из разбора онбординга → список активных slug.

    Известный из библиотеки — берём как есть. Незнакомый, но с
    name_ru+domain — генерируем по скелету (решение «сразу, без
    черновика»). Каркас папки-памяти создаём по чеклисту скилла.
    Лаборант всегда активен.
    """
    agents_dir = Path(agents_dir) if agents_dir else AGENTS_DIR
    specialists_dir = (Path(specialists_dir) if specialists_dir
                       else SPECIALISTS_DIR)
    lib = library_specialists(agents_dir)
    name_to_slug = {name: slug for slug, name in lib.items()}

    roster: list[str] = []
    for item in parsed.get("specialists", []):
        if not isinstance(item, dict):
            continue
        slug = _sanitize_slug(item.get("slug", ""))
        name_ru = str(item.get("name_ru", "")).strip().lower()
        domain = str(item.get("domain", "")).strip()

        if slug and slug in lib:
            chosen = slug
        elif name_ru and name_ru in name_to_slug:
            chosen = name_to_slug[name_ru]
        elif slug and name_ru and domain:
            generate_specialist_file(slug, name_ru, domain, agents_dir)
            chosen = slug
        else:
            log.warning("онбординг: пропущен невалидный специалист %r", item)
            continue

        if chosen not in roster:
            roster.append(chosen)
            _ensure_skeleton_folder(chosen, specialists_dir)

    for slug in ALWAYS_ACTIVE:
        if slug not in roster:
            roster.append(slug)
            _ensure_skeleton_folder(slug, specialists_dir)
    return roster


def write_active_roster(slugs: list, data_dir: Path | None = None) -> Path:
    """Пишет data/active_specialists.json (отсортированный уникальный)."""
    data_dir = Path(data_dir) if data_dir else DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "active_specialists.json"
    path.write_text(
        json.dumps(sorted(set(slugs)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


# ---------- сохранение ----------

def save_persona(persona: dict, data_dir: Path | None = None,
                 history_dir: Path | None = None) -> tuple[Path, Path]:
    """Пишет идентичность в data/patient_persona.json и
    человекочитаемый профиль в history/PATIENT_PERSONA.md.
    Возвращает (json_path, md_path)."""
    data_dir = Path(data_dir) if data_dir else DATA_DIR
    history_dir = Path(history_dir) if history_dir else HISTORY_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)

    addressee = persona.get("addressee", "self")
    if addressee not in ("self", "caregiver"):
        addressee = "self"
    identity = {
        "full_name": persona.get("full_name", ""),
        "address_name": persona.get("address_name", ""),
        "dob": persona.get("dob", ""),
        "city": persona.get("city", ""),
        "gender": persona.get("gender", "f"),
        "addressee": addressee,
    }
    json_path = data_dir / "patient_persona.json"
    json_path.write_text(
        json.dumps(identity, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    addr_note = ("сам пациент (один адресат, пишем человеку напрямую)"
                 if addressee == "self"
                 else "ухаживающий за пациентом (два адресата: пациент "
                      "и ухаживающий-координатор)")
    md_path = history_dir / "PATIENT_PERSONA.md"
    md_path.write_text(
        "# Профиль персоны (из онбординга)\n\n"
        "_Заполнено при знакомстве. Читают все специалисты при разборе._\n\n"
        f"**Модель адресата:** {addr_note}\n\n"
        + persona.get("persona_md", "").strip() + "\n",
        encoding="utf-8",
    )

    # сразу подхватываем — без рестарта
    patient_identity._invalidate_cache()
    return json_path, md_path
