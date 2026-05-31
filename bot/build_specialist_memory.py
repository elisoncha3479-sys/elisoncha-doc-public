"""
Построение системы памяти специалистов.
Читает все OCR-тексты из inbox, анализирует через Claude,
раскидывает по папкам специалистов и генерирует profile.md для каждого.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv()

import patient_identity  # noqa: E402  (нужен sys.path выше)
from agents import discover_specialists  # noqa: E402  (нужен sys.path выше)

_claude = None


def _get_claude() -> "anthropic.Anthropic":
    """Ленивая инициализация клиента: импорт модуля не должен падать без
    ключа (нужно для тестов/реестра в чистом окружении)."""
    global _claude
    if _claude is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY не задан — пропишите ключ в .env"
            )
        _claude = anthropic.Anthropic(api_key=key)
    return _claude


# Папка входящих документов пациента (по умолчанию нейтральная).
INBOX = Path(__file__).parent.parent / "inbox" / os.environ.get(
    "PATIENT_INBOX_DIR", "PATIENT"
)
SPECIALISTS_DIR = Path(__file__).parent.parent / "specialists"
HISTORY_PATH = Path(__file__).parent.parent / "history" / "MEDICAL_HISTORY.md"

# name_ru → slug из динамического реестра agents/*.md (backlog #2): новый
# approve'нутый специалист подхватывается без правки Python.
SPECIALIST_MAP = discover_specialists()

ROUTING_PROMPT = (
    "Определи, к каким медицинским специалистам относится этот документ.\n\n"
    # Список специалистов — из реестра, чтобы новый approve'нутый
    # специалист сразу попадал в роутинг (backlog #2).
    f"Специалисты: {', '.join(SPECIALIST_MAP.keys())}.\n\n"
    """Правила:
- Один документ может относиться к НЕСКОЛЬКИМ специалистам
- Анализ крови (ОАК, биохимия, липидограмма) → лаборант + все, чьи показатели затронуты
- ЭКГ, ЭхоКГ, давление → кардиолог
- Креатинин, мочевина, моча → нефролог
- Холестерин, ЛПНП → кардиолог
- Глюкоза, щитовидная, вес → эндокринолог
- УЗИ живота, ЖКТ → гастроэнтеролог
- Стопа, колено, суставы → ортопед
- Лимфоциты, лейкоформула, ХЛЛ → гематолог
- Сосуды головы, головокружение → невролог

Также определи дату документа (если есть) в формате YYYY-MM-DD.
И краткое описание (1 строка) — что это за документ.

Верни ТОЛЬКО JSON:
{
  "specialists": ["кардиолог", "лаборант"],
  "date": "2026-03-16",
  "description": "Общий анализ крови + биохимия"
}"""
)


def find_ocr_texts():
    """Находит все .txt сайдкары в inbox."""
    results = []
    for txt_path in sorted(INBOX.rglob("*.txt")):
        if txt_path.name == "_batch_report.txt":
            continue
        if txt_path.name.startswith("."):
            continue
        text = txt_path.read_text(encoding="utf-8")
        # Пропускаем метаданные OCR
        lines = text.split("\n")
        content_lines = [l for l in lines if not l.startswith("# ")]
        content = "\n".join(content_lines).strip()
        if len(content) < 20:
            continue

        # Имя оригинального файла
        orig_name = txt_path.name
        for suffix in [".JPG.txt", ".jpg.txt", ".PNG.txt", ".png.txt",
                       ".HEIC.txt", ".heic.txt", ".pdf.txt", ".docx.txt"]:
            if orig_name.endswith(suffix):
                orig_name = orig_name[:-4]  # убираем .txt
                break

        results.append({
            "path": txt_path,
            "orig_name": orig_name,
            "content": content[:3000],  # первые 3000 символов
        })
    return results


def route_document(content: str) -> dict:
    """Определяет специалистов и дату через Claude."""
    response = _get_claude().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=ROUTING_PROMPT,
        messages=[{"role": "user", "content": f"Документ:\n\n{content[:2000]}"}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"specialists": ["uncategorized"], "date": "unknown", "description": "Не удалось определить"}


def save_to_specialist(spec_name: str, date: str, description: str, content: str, orig_name: str):
    """Сохраняет запись в папку специалиста."""
    folder_name = SPECIALIST_MAP.get(spec_name, "uncategorized")
    folder = SPECIALISTS_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    safe_date = date.replace("/", "-") if date else "unknown"
    filename = f"{safe_date}_{orig_name}.md"
    # Убираем недопустимые символы
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

    filepath = folder / filename
    if filepath.exists():
        return  # уже обработано

    md_content = f"""# {description}

**Дата:** {date}
**Источник:** {orig_name}
**Специалист:** {spec_name}

---

{content}
"""
    filepath.write_text(md_content, encoding="utf-8")


def generate_profile(spec_name: str):
    """Генерирует profile.md — свод для специалиста через Claude."""
    folder_name = SPECIALIST_MAP.get(spec_name, spec_name)
    folder = SPECIALISTS_DIR / folder_name

    entries = sorted(folder.glob("*.md"))
    entries = [e for e in entries if e.name != "profile.md"]

    if not entries:
        return

    # Собираем все записи
    all_text = ""
    for entry in entries:
        content = entry.read_text(encoding="utf-8")
        all_text += f"\n\n{'='*60}\n{content}"

    # Загружаем промпт агента если есть
    agent_path = Path(__file__).parent.parent / "agents" / f"{folder_name}.md"
    agent_prompt = agent_path.read_text(encoding="utf-8") if agent_path.exists() else ""

    # Загружаем историю
    history = HISTORY_PATH.read_text(encoding="utf-8") if HISTORY_PATH.exists() else ""

    _pname = patient_identity.full_name()
    _patient_ref = f"по пациенту {_pname}" if _pname else f"по {patient_identity.patient_noun()}"

    profile_prompt = f"""Ты — {spec_name} в системе «Elisoncha Doc».

{agent_prompt}

Составь СВОД {_patient_ref} на основе всех документов ниже.
Также учти общую историю болезни.

Свод должен быть структурирован:

1. **Мой профиль пациентки** — 3-5 предложений, суть по ТВОЕЙ области
2. **Хронология** — таблица: дата | исследование | ключевые находки | тренд
3. **Текущее состояние** — что актуально сейчас
4. **Лекарства по моей области** — что принимает, что влияет
5. **На что обратить внимание** — красные флаги, ожидающие действия
6. **Рекомендации** — что делать дальше

Пиши как врач для себя — профессионально, с цифрами, но понятно.
Это твоя рабочая записка, которую ты обновляешь при каждом новом документе.

ОБЩАЯ ИСТОРИЯ БОЛЕЗНИ:
{history[:4000]}

ДОКУМЕНТЫ ПО МОЕЙ ОБЛАСТИ:
{all_text[:6000]}
"""

    response = _get_claude().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": profile_prompt}],
    )

    profile_text = response.content[0].text
    profile_path = folder / "profile.md"
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    header = f"""# {spec_name.upper()} — Свод по пациенту

**Пациент:** {_pname or "см. history/MEDICAL_HISTORY.md"}
**Последнее обновление:** {now}
**Документов в базе:** {len(entries)}

---

"""
    profile_path.write_text(header + profile_text, encoding="utf-8")
    print(f"  Profile: {profile_path}")


def main():
    print("═══ Построение системы памяти специалистов ═══\n")

    # 1. Находим все OCR-тексты
    texts = find_ocr_texts()
    print(f"Найдено документов с текстом: {len(texts)}\n")

    # 2. Маршрутизируем каждый
    print("Маршрутизация документов...\n")
    routing_results = []

    for i, doc in enumerate(texts, 1):
        routing = route_document(doc["content"])
        routing_results.append({**doc, **routing})

        specs = ", ".join(routing.get("specialists", ["?"]))
        date = routing.get("date", "?")
        desc = routing.get("description", "?")
        print(f"  [{i:>3}/{len(texts)}] {doc['orig_name']}")
        print(f"           → {specs} | {date} | {desc}")

    # 3. Раскидываем по папкам
    print(f"\nРаскладываем по специалистам...\n")
    spec_counts = {}

    for doc in routing_results:
        specialists = doc.get("specialists", ["uncategorized"])
        date = doc.get("date", "unknown")
        description = doc.get("description", doc["orig_name"])

        for spec in specialists:
            save_to_specialist(spec, date, description, doc["content"], doc["orig_name"])
            spec_counts[spec] = spec_counts.get(spec, 0) + 1

    for spec, count in sorted(spec_counts.items(), key=lambda x: -x[1]):
        print(f"  {spec}: {count} документов")

    # 4. Генерируем profile.md для каждого
    print(f"\nГенерация профилей...\n")
    all_specs = set()
    for doc in routing_results:
        all_specs.update(doc.get("specialists", []))

    for spec in sorted(all_specs):
        if spec == "uncategorized":
            continue
        print(f"  Генерирую: {spec}...")
        try:
            generate_profile(spec)
        except Exception as e:
            print(f"  ОШИБКА: {spec} — {e}")

    print(f"\n═══ Готово! ═══")
    print(f"Папка: {SPECIALISTS_DIR}")


if __name__ == "__main__":
    main()
