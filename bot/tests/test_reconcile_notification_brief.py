"""Уведомление сверки — короткое, тёплым языком, ТОЛЬКО про изменения
(владелец, 2026-05-16).

Раньше в чат падала длинная техническая «портянка» (правило «5 элементов
на каждый пункт», потолок 3500, max_tokens 4000, 2 сообщения). Затем —
короткая выжимка, но писателю всё ещё скармливали полный свежий отчёт,
и он переписывал всю схему. Теперь писатель получает ТОЛЬКО сводку
изменений; полная схема остаётся файлом в архиве.
"""

import inspect
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from reconcile import (
    DIFF_SYSTEM_PROMPT,
    NOTIFICATION_SYSTEM_PROMPT,
    _format_group_notification,
)


def test_prompt_enforces_brevity():
    p = NOTIFICATION_SYSTEM_PROMPT
    assert "КОРОТК" in p
    assert "1200 символов" in p
    assert "одно сообщение" in p


def test_old_long_format_rules_removed():
    p = NOTIFICATION_SYSTEM_PROMPT
    # рудименты «портянки» не должны остаться
    assert "ПЯТЬ ЭЛЕМЕНТОВ" not in p
    assert "3500" not in p
    assert "25 строк" not in p


def test_prompt_keeps_safety_voice():
    p = NOTIFICATION_SYSTEM_PROMPT.lower()
    # сохранили: к какому именно специалисту, без страшилок, голос как PDF
    assert "специалист" in p
    assert "не волнуйтесь" in p  # в списке запрещённого
    assert "имени-отчеству" in p  # тёплое обращение к пациенту, на «вы»
    assert "на «вы»" in p


def test_writer_gets_only_change_summary_not_full_report():
    """Главный фикс: писателю даётся только сводка изменений, полная схема —
    в архив, в промт не попадает и придумывать её нельзя."""
    p = NOTIFICATION_SYSTEM_PROMPT.lower()
    assert "сводк" in p and "изменени" in p
    assert "архив" in p
    # явный запрет додумывать схему сверх присланных изменений
    assert "не видишь" in p or "не должна" in p


def test_format_group_notification_signature_is_diff_only():
    """Сигнатура сузилась до одного аргумента — полный отчёт больше
    не прокидывается в писатель."""
    params = list(inspect.signature(_format_group_notification).parameters)
    assert params == ["diff_summary"]
    assert "new_report_text" not in params
    assert "prev_report_text" not in params


def test_diff_prompt_is_change_scoped_and_actionable():
    """Шаг сравнения теперь отдаёт обогащённую сводку изменений
    (что · почему · что делать + специалист · срочность) и сохраняет
    sentinel «изменений нет»."""
    d = DIFF_SYSTEM_PROMPT
    assert "NO_CHANGES" in d
    assert "ЧТО ДЕЛАТЬ" in d
    assert "ПОЧЕМУ ВАЖНО" in d
    assert "СРОЧНОСТЬ" in d
    assert "специалист" in d.lower()
    # по-прежнему только про изменения, не пересказ всей схемы
    assert "ТОЛЬКО ПО ИЗМЕНЕНИЯМ" in d
