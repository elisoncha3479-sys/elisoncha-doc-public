"""Identity gate — проверка, что документ принадлежит пациенту, ПО ФИО.

Мотивация (инцидент 013): документ родственника с той же фамилией, что
у пациента, был ошибочно принят как документ пациента. Дата рождения в
документе была, но узкие regex'ы её не выцепляли — ловить по ДР
ненадёжно. Имя есть почти на любом анализе.

Логика:
  - эталон ФИО не задан → not_configured (разбираем молча);
  - в тексте уверенно найдено ФИО пациента (фамилия + имя/отчество ИЛИ
    фамилия + совпавшие инициалы) → matched (разбираем молча);
  - иначе → suspect (НЕ угадываем — спрашиваем двумя кнопками).

Одной фамилии недостаточно: у родственника может быть та же фамилия.
Требуется ещё совпадение имени/отчества или инициалов И.О.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

STATUS_MATCHED = "matched"
STATUS_SUSPECT = "suspect"
STATUS_NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class IdentityVerdict:
    status: str

    @property
    def suspect(self) -> bool:
        return self.status == STATUS_SUSPECT

    @property
    def configured(self) -> bool:
        return self.status != STATUS_NOT_CONFIGURED


def should_vision_recheck_identity(verdict: "IdentityVerdict", has_image: bool) -> bool:
    """Стоит ли перепроверить ФИО зрением модели (B, 2026-05-16).

    Сжатое «фото» в Telegram → OCR теряет имя пациента → ложный suspect.
    Если есть картинка и охранник засомневался — даём ещё один заход
    через зрение, прежде чем дёргать пациента кнопками. Если ФИО уже
    подтверждено или картинки нет — не нужно."""
    return bool(verdict.suspect and has_image)


def _norm(s: str) -> str:
    """NFC + casefold: macOS отдаёт кириллицу в NFD, текст бывает в любом
    регистре."""
    return unicodedata.normalize("NFC", s or "").casefold()


def _stem(token: str) -> str:
    """Грубый стем для русских падежей: отбрасываем последнюю букву у
    длинных токенов (Иванова→иванов, Марии→мари, Петровны→петровн)."""
    t = _norm(token)
    return t[:-1] if len(t) > 4 else t


def evaluate_identity(ocr_text: str, patient_full_name: str) -> IdentityVerdict:
    """Гейт ДО анализа и доставки. Возвращает различимый статус."""
    if not patient_full_name or not patient_full_name.strip():
        return IdentityVerdict(STATUS_NOT_CONFIGURED)

    text = _norm(ocr_text)
    if not text.strip():
        return IdentityVerdict(STATUS_SUSPECT)

    parts = patient_full_name.split()
    surname = parts[0] if parts else ""
    given = parts[1] if len(parts) > 1 else ""
    patronymic = parts[2] if len(parts) > 2 else ""

    if not surname:
        return IdentityVerdict(STATUS_NOT_CONFIGURED)

    surname_found = _stem(surname) in text
    given_found = bool(given) and _stem(given) in text
    patronymic_found = bool(patronymic) and _stem(patronymic) in text

    initials_found = False
    if given and patronymic:
        gi = re.escape(_norm(given[0]))
        pi = re.escape(_norm(patronymic[0]))
        # «Л.С.», «Л. С.», «Л.С», порядок имя-отчество
        initials_found = re.search(rf"{gi}\s*\.\s*{pi}\s*\.?", text) is not None

    confident = surname_found and (
        given_found or patronymic_found or initials_found
    )
    return IdentityVerdict(STATUS_MATCHED if confident else STATUS_SUSPECT)
