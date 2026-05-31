"""Динамический «лекарственный» список (согласовано с владельцем 2026-05-16).

Раньше PHARMACO_ACTIVE_SPECS был хардкодом из 5 специалистов — невролог
и ортопед в сверку лекарств не попадали, и каждый новый специалист
пришлось бы вписывать руками (легко забыть → дыра в безопасности).

Теперь: по умолчанию ВСЕ специалисты из динамического реестра —
лекарственные; исключение — явный короткий список не-назначающих
(`lab-analyst`). Любой новый approve'нутый специалист входит сам.
"""

import sys
import textwrap
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from reconcile import NON_PRESCRIBING_SPECS, pharmaco_active_specs


def _write_agent(directory: Path, slug: str, name_ru: str, role: str = "specialist") -> None:
    (directory / f"{slug}.md").write_text(
        textwrap.dedent(f"""\
            ---
            slug: {slug}
            name_ru: {name_ru}
            role: {role}
            ---

            # {name_ru.capitalize()}
            заглушка
        """),
        encoding="utf-8",
    )


def test_neurologist_and_orthopedist_now_included(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "neurologist", "невролог")
    _write_agent(tmp_path, "orthopedist", "ортопед")
    _write_agent(tmp_path, "lab-analyst", "лаборант")

    pharm = pharmaco_active_specs(tmp_path)

    assert "neurologist" in pharm
    assert "orthopedist" in pharm
    assert "cardiologist" in pharm


def test_lab_analyst_excluded_as_non_prescribing(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "lab-analyst", "лаборант")

    pharm = pharmaco_active_specs(tmp_path)

    assert "lab-analyst" not in pharm
    assert "lab-analyst" in NON_PRESCRIBING_SPECS


def test_brand_new_specialist_auto_included(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "pulmonologist", "пульмонолог")  # новый, его никто не вписывал

    pharm = pharmaco_active_specs(tmp_path)

    assert "pulmonologist" in pharm


def test_meta_roles_not_in_pharm(tmp_path):
    _write_agent(tmp_path, "cardiologist", "кардиолог")
    _write_agent(tmp_path, "chief", "главврач", role="meta")

    pharm = pharmaco_active_specs(tmp_path)

    assert pharm == {"cardiologist"}


def test_default_uses_real_registry_and_excludes_lab_analyst():
    pharm = pharmaco_active_specs()
    assert isinstance(pharm, set) and pharm
    assert "lab-analyst" not in pharm
    # на реальном реестре невролог и ортопед теперь покрыты
    assert "neurologist" in pharm
    assert "orthopedist" in pharm


def test_therapist_and_dietitian_non_prescribing_dental_prescribes():
    """РПП-терапевт и диетолог не назначают лекарства → вне сверки;
    стоматолог-гнатолог назначает (антибиотики и пр.) → в сверке."""
    assert "eating-disorder-therapist" in NON_PRESCRIBING_SPECS
    assert "dietitian" in NON_PRESCRIBING_SPECS
    pharm = pharmaco_active_specs()
    assert "eating-disorder-therapist" not in pharm
    assert "dietitian" not in pharm
    assert "dental-gnathologist" in pharm
