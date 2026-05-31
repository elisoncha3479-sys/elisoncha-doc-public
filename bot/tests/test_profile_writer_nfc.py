"""Регрессия: NFD/NFC дубли кириллических имён.

macOS APFS отдаёт строки в NFD («й» = «и» + U+0306, «ё» = «е» + U+0308),
Linux ext4 хранит как есть. После rsync VPS→mac появляются файлы-дубли,
которые git видит как разные. Фикс: имя файла per-doc всегда нормализуется
в NFC перед записью.

Тестовые строки специально содержат «й» и «ё» — единственные кириллические
буквы, которые реально декомпозируются в NFD.
"""

import unicodedata

import pytest

from profile_writer import _slugify_title

# «шейного» (й) и «приём» (ё) — оба декомпозируются в NFD
DECOMPOSABLE_TITLES = [
    "Рентген шейного отдела позвоночника",
    "Приём невролога ротационные пробы",
    "УЗДС сосудов шеи приём 2025",
]


@pytest.mark.parametrize("text", DECOMPOSABLE_TITLES)
def test_title_is_actually_decomposable(text):
    """Защита самого теста: убеждаемся, что строка реально меняется в NFD,
    иначе тест ничего не проверяет."""
    assert unicodedata.normalize("NFD", text) != unicodedata.normalize("NFC", text)


@pytest.mark.parametrize("text", DECOMPOSABLE_TITLES)
def test_slugify_normalizes_nfd_to_nfc(text):
    nfd_title = unicodedata.normalize("NFD", text)

    slug = _slugify_title(nfd_title)

    assert unicodedata.is_normalized("NFC", slug), (
        f"slug не в NFC: {[hex(ord(c)) for c in slug]}"
    )


@pytest.mark.parametrize("text", DECOMPOSABLE_TITLES)
def test_slugify_nfd_and_nfc_produce_identical_slug(text):
    """Один заголовок в NFD и NFC должен дать байт-в-байт одинаковый slug —
    иначе rsync VPS→mac снова наплодит дубли."""
    slug_from_nfc = _slugify_title(unicodedata.normalize("NFC", text))
    slug_from_nfd = _slugify_title(unicodedata.normalize("NFD", text))

    assert slug_from_nfc == slug_from_nfd
    assert unicodedata.is_normalized("NFC", slug_from_nfc)


def test_slugify_plain_ascii_unchanged():
    assert _slugify_title("Blood test 2025") == "Blood test 2025"
