"""
Пакетная обработка медицинских документов — извлечение текста из всех файлов в inbox/МАМА/.
Запуск: python batch_process.py
"""

import sys
from pathlib import Path
from datetime import datetime

# Добавляем путь к модулям бота
sys.path.insert(0, str(Path(__file__).parent))

from ocr import extract_text, save_text_sidecar, ExtractionResult

INBOX = Path(__file__).parent.parent / "inbox" / "МАМА"
SUPPORTED = {".jpg", ".jpeg", ".png", ".heic", ".pdf", ".docx"}


def discover_files(root: Path) -> list:
    """Находит все поддерживаемые файлы, пропуская уже обработанные."""
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        # Пропускаем если уже есть .txt сайдкар
        sidecar = Path(str(path) + ".txt")
        if sidecar.exists():
            continue
        files.append(path)
    return files


def process_file(path: Path) -> ExtractionResult:
    """Обрабатывает один файл и сохраняет .txt."""
    result = extract_text(str(path))
    if result.text.strip():
        save_text_sidecar(str(path), result)
    return result


def main():
    if not INBOX.exists():
        print(f"Папка не найдена: {INBOX}")
        return

    all_supported = list(INBOX.rglob("*"))
    all_supported = [p for p in all_supported if p.is_file() and p.suffix.lower() in SUPPORTED]
    total_all = len(all_supported)

    files = discover_files(INBOX)
    skipped = total_all - len(files)

    print(f"═══ Elisoncha Doc: пакетная обработка ═══")
    print(f"Папка: {INBOX}")
    print(f"Найдено файлов: {total_all}")
    print(f"Уже обработано: {skipped}")
    print(f"К обработке: {len(files)}")
    print()

    if not files:
        print("Все файлы уже обработаны!")
        return

    # Сортируем: DOCX → PDF → остальное (быстрые сначала)
    priority = {".docx": 0, ".pdf": 1}
    files.sort(key=lambda p: (priority.get(p.suffix.lower(), 2), p.name))

    results = {"ok": 0, "fail": 0, "by_type": {}}
    errors = []

    for i, path in enumerate(files, 1):
        ext = path.suffix.lower()
        rel = path.relative_to(INBOX)

        result = process_file(path)

        if result.error:
            status = f"ОШИБКА: {result.error}"
            results["fail"] += 1
            errors.append((rel, result.error))
        else:
            status = f"OK ({result.confidence}%, {len(result.text)} символов)"
            results["ok"] += 1

        # Статистика по типам
        if ext not in results["by_type"]:
            results["by_type"][ext] = {"count": 0, "total_conf": 0.0}
        results["by_type"][ext]["count"] += 1
        results["by_type"][ext]["total_conf"] += result.confidence

        print(f"  [{i:>3}/{len(files)}] {rel} ... {status}")

    # Отчёт
    print()
    print(f"═══ Результат ═══")
    print(f"  Успешно: {results['ok']}")
    print(f"  Ошибки:  {results['fail']}")
    print()
    print(f"  По типам:")
    for ext, data in sorted(results["by_type"].items()):
        avg = data["total_conf"] / data["count"] if data["count"] else 0
        print(f"    {ext:>6}: {data['count']} файлов, средняя уверенность {avg:.1f}%")

    if errors:
        print()
        print(f"  Ошибки:")
        for rel, err in errors:
            print(f"    {rel}: {err}")

    # Сохраняем отчёт
    report_path = INBOX / "_batch_report.txt"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    report_lines = [
        f"Batch OCR Report — {now}",
        f"Обработано: {results['ok']} OK, {results['fail']} ошибок",
        "",
    ]
    for ext, data in sorted(results["by_type"].items()):
        avg = data["total_conf"] / data["count"] if data["count"] else 0
        report_lines.append(f"{ext}: {data['count']} files, avg confidence {avg:.1f}%")
    if errors:
        report_lines.append("")
        report_lines.append("Errors:")
        for rel, err in errors:
            report_lines.append(f"  {rel}: {err}")
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\n  Отчёт сохранён: {report_path}")


if __name__ == "__main__":
    main()
