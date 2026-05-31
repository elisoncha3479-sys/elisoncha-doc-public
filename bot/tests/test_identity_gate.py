"""Финальный охранник (после инцидента 013).

Проверяем по ФИО, НЕ по дате рождения: в инциденте дата в документе была,
но узкие regex'ы её не выцепили. Имя (фамилия + имя/отчество ИЛИ
фамилия + инициалы) надёжнее — оно есть почти на любом анализе.

evaluate_identity(ocr_text, patient_full_name) → статус:
  matched        — ФИО пациента подтверждено → разбираем молча
  suspect        — ФИО не подтверждено → спросить двумя кнопками
  not_configured — эталон ФИО не задан → разбираем молча

Ключевой кейс инцидента: родственник с той же фамилией, но другим
именем/отчеством/инициалами → ДОЛЖНО быть suspect, не matched.

Данные в тесте — вымышленные (никаких реальных людей).
"""

import unicodedata

import pytest

from identity_check import (
    STATUS_MATCHED,
    STATUS_NOT_CONFIGURED,
    STATUS_SUSPECT,
    evaluate_identity,
    should_vision_recheck_identity,
)

PATIENT = "Иванова Мария Петровна"


def test_not_configured_when_no_reference_name():
    v = evaluate_identity("любой текст", "")
    assert v.status == STATUS_NOT_CONFIGURED
    assert v.suspect is False
    assert v.configured is False


@pytest.mark.parametrize("text", [
    "Пациент: Иванова Мария Петровна, анализ крови",
    "ИВАНОВА МАРИЯ ПЕТРОВНА",
    "Био: Ивановой Марии Петровны, гемоглобин 130",  # родительный падеж
    "Иванова М.П. — общий анализ",
    "Иванова М. П., направление",
])
def test_matched_for_various_patient_name_forms(text):
    v = evaluate_identity(text, PATIENT)
    assert v.status == STATUS_MATCHED
    assert v.suspect is False


@pytest.mark.parametrize("text", [
    "Иванова Светлана Андреевна, осмотр травматолога",  # родственник, та же фамилия
    "Ивановой Светланы Андреевны, 14.06.1988",
    "Иванова С.А. заключение",
    "Сидоров Иван Иванович, ЭКГ",
    "анализ без какого-либо имени, гемоглобин 130",
    "",
])
def test_suspect_when_patient_name_not_confirmed(text):
    v = evaluate_identity(text, PATIENT)
    assert v.status == STATUS_SUSPECT
    assert v.suspect is True
    assert v.configured is True


def test_nfd_macos_form_still_matches():
    # macOS отдаёт «ё» в NFD — охранник нормализует в NFC.
    nfd = unicodedata.normalize("NFD", "Иванова Мария Петровна")
    assert evaluate_identity(nfd, PATIENT).status == STATUS_MATCHED


def test_surname_alone_is_not_enough():
    # Та же фамилия, но без имени/отчества/совпавших инициалов — подозрение.
    v = evaluate_identity("Иванова, направление в лабораторию", PATIENT)
    assert v.status == STATUS_SUSPECT


# ---- B: перепроверка ФИО зрением для сжатых фото ----

def test_vision_recheck_only_when_suspect_and_image():
    suspect = evaluate_identity("анализ без имени", PATIENT)        # suspect
    matched = evaluate_identity("Иванова Мария Петровна", PATIENT)  # matched
    not_cfg = evaluate_identity("что угодно", "")                   # not_configured

    assert should_vision_recheck_identity(suspect, has_image=True) is True
    assert should_vision_recheck_identity(suspect, has_image=False) is False
    assert should_vision_recheck_identity(matched, has_image=True) is False
    assert should_vision_recheck_identity(not_cfg, has_image=True) is False
