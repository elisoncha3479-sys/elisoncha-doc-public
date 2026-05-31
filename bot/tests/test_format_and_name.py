"""Регрессии инцидента 2026-05-31:
1. Формат файла: .odt читается; неподдержанный/бинарный формат НЕ выдаёт
   ложный «успешный» OCR (и потому не уйдёт слепо в vision → 400).
2. Имя в чате: реальное имя пациента передаётся в промпт ответа; при
   отсутствии имени модель получает прямой запрет выдумывать (повод —
   галлюцинация «Тамара Ивановна»).
"""
import sys
import zipfile
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import ocr
import patient_message as pm


# ---------- фейковый Anthropic-клиент (как в test_patient_message) ----------

class _Resp:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]


class FakeClient:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = []

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kw):
            self._outer.calls.append(kw)
            return _Resp(self._outer._outputs.pop(0))

    @property
    def messages(self):
        return FakeClient._Messages(self)


# ---------- 1. формат .odt ----------

def _make_odt(path: Path, paragraphs) -> None:
    """Минимальный валидный .odt: zip с content.xml в ODF-разметке."""
    ns_office = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    ns_text = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    content = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-content xmlns:office="{ns_office}" '
        f'xmlns:text="{ns_text}">'
        f"<office:body><office:text>{body}"
        f"</office:text></office:body></office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.xml", content)


def test_odt_text_is_extracted(tmp_path):
    f = tmp_path / "analiz.odt"
    _make_odt(f, ["Гемоглобин 130 г/л", "Лейкоциты 6.2"])
    res = ocr.extract_text(str(f))
    assert "Гемоглобин 130 г/л" in res.text
    assert "Лейкоциты 6.2" in res.text
    assert res.method == "odt-xml"


def test_odt_passes_quality_gate(tmp_path):
    f = tmp_path / "analiz.odt"
    _make_odt(f, ["Заключение: норма"])
    res = ocr.extract_text(str(f))
    assert ocr.assess_quality(res) is True


def test_unsupported_format_is_not_falsely_ok(tmp_path):
    """Неизвестный формат не должен выглядеть как валидный текст —
    иначе main.py не уведёт его в vision, но и тут подстрахуемся."""
    f = tmp_path / "странный.rtf"
    f.write_bytes(b"{\\rtf1 random binary-ish content}")
    res = ocr.extract_text(str(f))
    assert res.method in ("none", "error")
    assert ocr.assess_quality(res) is False


# ---------- 2. имя в чат-ответе ----------

def _system_text(client) -> str:
    return client.calls[0]["system"][0]["text"]


def test_patient_reply_injects_known_name(monkeypatch):
    monkeypatch.setattr(pm.patient_identity, "address_name", lambda: "Мария")
    c = FakeClient(["ответ"])
    pm.generate_patient_reply(c, "как дела?")
    sys_text = _system_text(c)
    assert "Мария" in sys_text
    assert "выдумыв" in sys_text.lower()  # запрет домысливать отчество/фамилию


def test_patient_reply_forbids_inventing_name_when_unknown(monkeypatch):
    monkeypatch.setattr(pm.patient_identity, "address_name", lambda: "")
    c = FakeClient(["ответ"])
    pm.generate_patient_reply(c, "здравствуйте")
    sys_text = _system_text(c).lower()
    assert "не придумывай" in sys_text or "без имени" in sys_text
