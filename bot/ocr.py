"""
OCR-модуль «Elisoncha Doc» — извлечение текста из медицинских документов.
Поддерживает: JPG, PNG, HEIC, PDF, DOCX.
"""

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Проверяем наличие Tesseract при импорте
if not shutil.which("tesseract"):
    log.warning("Tesseract не найден! OCR фото работать не будет. Установите: brew install tesseract tesseract-lang")


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    confidence: float
    method: str
    error: Optional[str] = None


def extract_text(file_path: str) -> ExtractionResult:
    """Извлекает текст из файла. Выбирает метод по расширению."""
    path = Path(file_path)
    ext = path.suffix.lower()

    handlers = {
        ".jpg": _extract_from_image,
        ".jpeg": _extract_from_image,
        ".png": _extract_from_image,
        ".heic": _extract_from_heic,
        ".pdf": _extract_from_pdf,
        ".docx": _extract_from_docx,
        ".odt": _extract_from_odt,
    }

    handler = handlers.get(ext)
    if not handler:
        return ExtractionResult(
            text="", confidence=0.0, method="none",
            error=f"Неподдерживаемый формат: {ext}"
        )

    try:
        return handler(str(path))
    except Exception as e:
        log.error("Ошибка OCR для %s: %s", path.name, e)
        return ExtractionResult(
            text="", confidence=0.0, method="error",
            error=str(e)
        )


def _extract_from_image(file_path: str) -> ExtractionResult:
    """OCR через Tesseract для фотографий."""
    import pytesseract
    from PIL import Image, ImageFilter

    img = Image.open(file_path)

    # Ограничение разрешения перед OCR. На больших iPhone-HEIC (24+ МП)
    # tesseract уходит в многоминутный OCR без выигрыша в качестве. Для
    # медицинских документов ~2 МП с большим запасом достаточно — текст
    # читается, OCR стабильно укладывается в 30–40 секунд. Ограничиваем
    # по общему числу пикселей (а не по длинной стороне) — так
    # предсказуемо работаем и на квадратных (4:3), и на вытянутых
    # (2:1) картинках. Маленькие картинки не трогаем.
    MAX_OCR_PIXELS = 2_000_000
    w, h = img.size
    if w * h > MAX_OCR_PIXELS:
        scale = (MAX_OCR_PIXELS / (w * h)) ** 0.5
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Предобработка: серый + лёгкая резкость (улучшает OCR на мед. бланках)
    img = img.convert("L")
    img = img.filter(ImageFilter.SHARPEN)

    # Получаем текст + уверенность по словам
    data = pytesseract.image_to_data(img, lang="rus", output_type=pytesseract.Output.DICT)

    # Средняя уверенность (исключаем -1 = не распознано)
    confidences = [c for c in data["conf"] if c != -1 and c > 0]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    text = pytesseract.image_to_string(img, lang="rus").strip()

    return ExtractionResult(
        text=text,
        confidence=round(avg_conf, 1),
        method="tesseract",
    )


def _extract_from_heic(file_path: str) -> ExtractionResult:
    """HEIC → Pillow → Tesseract. Fallback на sips (macOS)."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        return _extract_from_image(file_path)
    except ImportError:
        import subprocess
        import tempfile
        # Fallback: sips конвертирует HEIC → JPEG
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        subprocess.run(
            ["sips", "-s", "format", "jpeg", file_path, "--out", tmp.name],
            capture_output=True,
        )
        result = _extract_from_image(tmp.name)
        Path(tmp.name).unlink(missing_ok=True)
        return ExtractionResult(
            text=result.text,
            confidence=result.confidence,
            method="tesseract+sips",
            error=result.error,
        )


def _extract_from_pdf(file_path: str) -> ExtractionResult:
    """PDF: сначала текстовый слой, потом Tesseract для сканов."""
    import pdfplumber

    texts = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                texts.append(page_text)

    full_text = "\n\n".join(texts).strip()

    # Если текстовый слой есть и достаточно содержательный
    if len(full_text) > 50:
        return ExtractionResult(
            text=full_text,
            confidence=95.0,
            method="pdfplumber",
        )

    # Скан-PDF: рендерим страницы как изображения и OCR
    log.info("PDF без текстового слоя, пробуем OCR: %s", Path(file_path).name)
    try:
        import pytesseract
        from PIL import Image

        ocr_texts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                pil_img = page.to_image(resolution=300).original
                page_text = pytesseract.image_to_string(pil_img, lang="rus")
                if page_text.strip():
                    ocr_texts.append(page_text.strip())

        ocr_full = "\n\n".join(ocr_texts)
        return ExtractionResult(
            text=ocr_full,
            confidence=55.0,
            method="pdfplumber+tesseract",
        )
    except Exception as e:
        return ExtractionResult(
            text=full_text,
            confidence=10.0,
            method="pdfplumber",
            error=f"OCR fallback failed: {e}",
        )


def _extract_from_docx(file_path: str) -> ExtractionResult:
    """DOCX: прямое извлечение текста + таблицы."""
    import docx

    doc = docx.Document(file_path)

    # Параграфы
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]

    # Таблицы
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                paragraphs.append(" | ".join(cells))

    full_text = "\n".join(paragraphs).strip()

    return ExtractionResult(
        text=full_text,
        confidence=99.0,
        method="python-docx",
    )


def _extract_from_odt(file_path: str) -> ExtractionResult:
    """ODT: текстовый документ OpenDocument. Это zip-архив с content.xml —
    достаём текст из абзацев и заголовков без сторонних зависимостей
    (stdlib zipfile + ElementTree). Покрывает выгрузки из Google Docs /
    LibreOffice / OpenOffice, которые присылают как .odt."""
    import zipfile
    import xml.etree.ElementTree as ET

    TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"

    with zipfile.ZipFile(file_path) as z:
        with z.open("content.xml") as f:
            root = ET.parse(f).getroot()

    # Абзацы (text:p) и заголовки (text:h); itertext() собирает вложенные
    # span'ы внутри абзаца в одну строку.
    blocks = []
    for elem in root.iter():
        if elem.tag in (TEXT_NS + "p", TEXT_NS + "h"):
            chunk = "".join(elem.itertext()).strip()
            if chunk:
                blocks.append(chunk)

    full_text = "\n".join(blocks).strip()

    return ExtractionResult(
        text=full_text,
        confidence=99.0,
        method="odt-xml",
    )


def assess_quality(result: ExtractionResult) -> bool:
    """Достаточно ли хорош OCR, чтобы не гонять Vision API?"""
    if result.method in ("python-docx", "pdfplumber", "odt-xml"):
        return True
    return result.confidence >= 60.0 and len(result.text.strip()) >= 30


def save_text_sidecar(file_path: str, result: ExtractionResult) -> str:
    """Сохраняет текст рядом с оригиналом как .txt файл."""
    txt_path = Path(file_path).with_suffix(Path(file_path).suffix + ".txt")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    content = (
        f"# OCR: {result.method} | confidence: {result.confidence}% | {now}\n"
        f"# source: {Path(file_path).name}\n\n"
        f"{result.text}"
    )

    txt_path.write_text(content, encoding="utf-8")
    return str(txt_path)
