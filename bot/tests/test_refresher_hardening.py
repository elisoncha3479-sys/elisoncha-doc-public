"""Хардненинг profile_refresher (находки при приёмке кардио-свода 2026-05-15):

1. Рефрешер затирал profile.md без бэкапа — плохая регенерация в бою
   была бы невосстановима. Теперь перед перезаписью кладётся profile.md.bak.
2. Дату «Последнее обновление» вписывал LLM → выдумывал время
   (2026-05-13 вместо 2026-05-15). Эту дату читает reconcile как сигнал
   свежести → был реальный баг, не косметика. Теперь дата проставляется
   программно.
"""

from pathlib import Path

import profile_refresher
from profile_refresher import _backup_existing, _stamp_timestamp


NOW = "2026-05-15 22:26"


# ---------- _stamp_timestamp ----------

def test_stamp_replaces_wrong_header_date():
    text = (
        "# КАРДИОЛОГИЯ — Свод\n"
        "**Последнее обновление:** 2026-05-13 23:30\n"
        "**Документов в базе:** 36\n\n## 1. ...\n"
    )
    out = _stamp_timestamp(text, NOW)
    assert f"**Последнее обновление:** {NOW}" in out
    assert "2026-05-13 23:30" not in out


def test_stamp_replaces_llm_footer_with_single_canonical():
    text = (
        "# Свод\n**Последнее обновление:** x\n\n## 1. ...\n\n"
        "_Обновлено автоматически: 2026-05-13 23:30_\n"
    )
    out = _stamp_timestamp(text, NOW)
    assert out.count("_Обновлено автоматически:") == 1
    assert f"_Обновлено автоматически: {NOW}_" in out
    assert "2026-05-13" not in out


def test_stamp_appends_footer_when_missing():
    text = "# Свод\n**Последнее обновление:** y\n\n## 1. тело\n"
    out = _stamp_timestamp(text, NOW)
    assert out.rstrip().endswith(f"_Обновлено автоматически: {NOW}_")


def test_stamp_adds_header_if_absent():
    text = "# Свод без шапки\n\n## 1. тело\n"
    out = _stamp_timestamp(text, NOW)
    assert f"**Последнее обновление:** {NOW}" in out


# ---------- _backup_existing ----------

def test_backup_creates_bak_with_previous_content(tmp_path: Path):
    p = tmp_path / "profile.md"
    p.write_text("СТАРОЕ содержимое", encoding="utf-8")
    bak = _backup_existing(p)
    assert bak is not None and bak.exists()
    assert bak.read_text(encoding="utf-8") == "СТАРОЕ содержимое"
    assert bak.name == "profile.md.bak"


def test_backup_noop_when_file_absent(tmp_path: Path):
    p = tmp_path / "profile.md"
    assert _backup_existing(p) is None  # не падает, бэкапить нечего


def test_backup_overwrites_previous_bak(tmp_path: Path):
    p = tmp_path / "profile.md"
    p.write_text("v1", encoding="utf-8")
    _backup_existing(p)
    p.write_text("v2", encoding="utf-8")
    bak = _backup_existing(p)
    assert bak.read_text(encoding="utf-8") == "v2"  # последний-известный-хороший


# ---------- REFRESH_INSTRUCTION больше не просит LLM ставить дату ----------

def test_refresh_instruction_no_longer_asks_llm_for_date():
    instr = profile_refresher.REFRESH_INSTRUCTION
    assert "добавь строку" not in instr
    assert "_Обновлено автоматически: YYYY-MM-DD" not in instr
