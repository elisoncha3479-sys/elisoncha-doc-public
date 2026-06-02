"""Backlog #4: reconcile-hook не должен стрелять, если раздел 4 «Лекарства»
у фарм-активного специалиста фактически не изменился после refresh.

Раньше maybe_run_reconcile_after_refresh запускал сверку при ЛЮБОМ обновлении
фарм-активного специалиста (cardiologist, nephrologist, ...). После, напр.,
УЗДС-документа в кардиологический profile.md раздел 4 не меняется, но сверка
всё равно слала уведомление пациенту/владельцу.
"""

import asyncio
import textwrap

import pytest

from meds_section import extract_meds_section, meds_section_changed
import reconcile


PROFILE_TMPL = textwrap.dedent("""\
    # Профиль: кардиолог
    > Последнее обновление: 2026-05-15

    ## 1. ДИАГНОЗЫ
    - ИБС

    ## 4. ЛЕКАРСТВА ПО МОЕЙ ОБЛАСТИ
    {meds}

    ## 5. ДИНАМИКА
    - стабильно
""")


def _profile(meds: str) -> str:
    return PROFILE_TMPL.format(meds=meds)


# ---------- extract_meds_section ----------

def test_extract_meds_section_grabs_section_4():
    text = _profile("- Бисопролол 5 мг утром\n- Аторвастатин 20 мг")
    sec = extract_meds_section(text)
    assert "Бисопролол" in sec and "Аторвастатин" in sec
    assert "ДИНАМИКА" not in sec and "ДИАГНОЗЫ" not in sec


# ---------- meds_section_changed ----------

def test_identical_meds_not_changed():
    a = _profile("- Бисопролол 5 мг")
    b = _profile("- Бисопролол 5 мг")
    assert meds_section_changed(a, b) is False


def test_whitespace_only_diff_not_changed():
    a = _profile("- Бисопролол 5 мг\n- Аторвастатин 20 мг")
    b = _profile("-  Бисопролол 5 мг\n\n-   Аторвастатин 20 мг   ")
    assert meds_section_changed(a, b) is False


def test_real_dose_change_is_changed():
    a = _profile("- Бисопролол 5 мг")
    b = _profile("- Бисопролол 2.5 мг")
    assert meds_section_changed(a, b) is True


def test_added_drug_is_changed():
    a = _profile("- Бисопролол 5 мг")
    b = _profile("- Бисопролол 5 мг\n- Аторвастатин 20 мг")
    assert meds_section_changed(a, b) is True


def test_both_missing_section_not_changed():
    """Новый specialist без раздела 4 в обоих версиях — не повод для сверки."""
    no_meds = "# Профиль\n## 1. ДИАГНОЗЫ\n- что-то\n"
    assert meds_section_changed(no_meds, no_meds) is False


# ---------- maybe_run_reconcile_after_refresh: smart-фильтр ----------

@pytest.fixture
def captured_calls(monkeypatch):
    calls = []

    async def fake_run(app, reason="manual"):
        calls.append(reason)
        return None

    monkeypatch.setattr(reconcile, "run_reconcile_and_notify", fake_run)
    return calls


def test_hook_skips_when_meds_unchanged(captured_calls):
    res = asyncio.run(
        reconcile.maybe_run_reconcile_after_refresh(
            ["cardiologist"], app=None, meds_changed_dirs=set()
        )
    )
    assert res is None
    assert captured_calls == [], "сверка не должна запускаться без изменения лекарств"


def test_hook_fires_when_meds_changed(captured_calls):
    asyncio.run(
        reconcile.maybe_run_reconcile_after_refresh(
            ["cardiologist"], app=None, meds_changed_dirs={"cardiologist"}
        )
    )
    assert len(captured_calls) == 1
    assert "cardiologist" in captured_calls[0]


def test_hook_skips_non_prescribing_specialist(captured_calls):
    # Динамический список (2026-05-16): не-назначающий — только лаборант.
    asyncio.run(
        reconcile.maybe_run_reconcile_after_refresh(
            ["lab-analyst"], app=None, meds_changed_dirs={"lab-analyst"}
        )
    )
    assert captured_calls == [], "лаборант не назначает — сверка не нужна"


def test_hook_now_fires_for_neurologist_and_orthopedist(captured_calls):
    # Раньше пропускались (дыра); теперь в сверке (согласовано 2026-05-16).
    asyncio.run(
        reconcile.maybe_run_reconcile_after_refresh(
            ["neurologist"], app=None, meds_changed_dirs={"neurologist"}
        )
    )
    asyncio.run(
        reconcile.maybe_run_reconcile_after_refresh(
            ["orthopedist"], app=None, meds_changed_dirs={"orthopedist"}
        )
    )
    assert len(captured_calls) == 2
    assert "neurologist" in captured_calls[0]
    assert "orthopedist" in captured_calls[1]


def test_hook_backward_compatible_without_filter(captured_calls):
    """Без meds_changed_dirs (None) — старое поведение: стреляет на любой
    refresh фарм-активного (для обратной совместимости / monthly)."""
    asyncio.run(
        reconcile.maybe_run_reconcile_after_refresh(["cardiologist"], app=None)
    )
    assert len(captured_calls) == 1
