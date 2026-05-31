"""Сообщение мамы в чат → разбор главврачом → память специалистов
(владелец, 2026-05-16, путь А — без гейта подтверждения).

Контракт:
- классификация намерения: clinical / question / both / social;
- social («спасибо») НЕ пишет в память и НЕ зовёт мультиагент;
- clinical → run_multi_agent → write_perdoc (с меткой провенанса
  «со слов мамы») → refresh профилей → reconcile-hook;
- сообщение НЕ от пациентки (владелец/оператор) в клинику не идёт;
- ответ маме генерируется тёплым голосом, без назначений лечения.
"""
import asyncio
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import pytest

import patient_message as pm


# ---------- фейковый Anthropic-клиент ----------

class _Resp:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]


class FakeClient:
    """messages.create возвращает заранее заданный текст по очереди."""
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


# ---------- классификация намерения ----------

def test_classify_returns_known_intent():
    c = FakeClient(['{"intent": "clinical"}'])
    out = pm.classify_patient_message(c, "уролог отменил Престариум")
    assert out["intent"] == "clinical"


def test_classify_unknown_falls_back_to_question():
    c = FakeClient(["мусор не json"])
    out = pm.classify_patient_message(c, "что-то непонятное")
    assert out["intent"] in {"clinical", "question", "both", "social"}


# ---------- провенанс ----------

def test_provenance_marker_is_patient_reported():
    assert "со слов" in pm.CHAT_PROVENANCE_SOURCE.lower()
    assert "чат" in pm.CHAT_PROVENANCE_SOURCE.lower()


# ---------- автор: только пациентка ----------

class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, uid):
        self.from_user = _User(uid)


def test_is_patient_author_strict_when_env_set(monkeypatch):
    monkeypatch.setenv("PATIENT_TG_USER_ID", "111")
    assert pm.is_patient_author(_Msg(111)) is True
    assert pm.is_patient_author(_Msg(999)) is False


def test_is_patient_author_default_true_when_env_unset(monkeypatch):
    monkeypatch.delenv("PATIENT_TG_USER_ID", raising=False)
    assert pm.is_patient_author(_Msg(123)) is True


# ---------- оркестрация ----------

@pytest.fixture
def spy(monkeypatch):
    """Подменяет тяжёлые зависимости, копит вызовы."""
    state = {
        "multi_agent": 0,
        "perdoc_args": None,
        "refresh_args": None,
        "reconcile_args": None,
    }

    async def fake_multi_agent(client, document_text, caption="", extra_context=""):
        state["multi_agent"] += 1
        return {
            "_routing": {"specialists": ["нефролог"], "document_type": "сообщение"},
            "_opinions": {"нефролог": "Отметить отмену препарата, проконтролировать."},
        }

    def fake_write_perdoc(analysis, raw_ocr_text="", source_filename="—"):
        state["perdoc_args"] = {
            "source": source_filename,
            "raw": raw_ocr_text,
        }
        return [Path("/x/specialists/nephrologist/2026-05-16_msg.md")]

    async def fake_refresh(dirs):
        state["refresh_args"] = dirs
        return [type("R", (), {"path": Path("/x/specialists/nephrologist/profile.md"),
                               "meds_changed": True})()]

    async def fake_reconcile(refreshed_dirs, app, meds_changed_dirs=None):
        state["reconcile_args"] = {
            "refreshed": refreshed_dirs,
            "meds_changed": meds_changed_dirs,
        }
        return None

    monkeypatch.setattr(pm, "run_multi_agent", fake_multi_agent)
    monkeypatch.setattr(pm, "write_perdoc_files", fake_write_perdoc)
    monkeypatch.setattr(pm, "refresh_profiles_for_specialists", fake_refresh)
    monkeypatch.setattr(pm, "maybe_run_reconcile_after_refresh", fake_reconcile)
    monkeypatch.setattr(
        pm, "generate_patient_reply",
        lambda client, text, brief="": "Мария Петровна, отметили, спасибо.",
    )
    return state


def test_social_message_does_not_touch_memory(spy):
    c = FakeClient(['{"intent": "social"}'])
    res = asyncio.run(pm.handle_patient_message(c, app=None,
                                                text="спасибо вам большое!", ts="t1"))
    assert spy["multi_agent"] == 0
    assert spy["perdoc_args"] is None
    assert res["intent"] == "social"
    assert res["reply"]


def test_clinical_message_writes_memory_with_provenance(spy):
    c = FakeClient(['{"intent": "clinical"}'])
    res = asyncio.run(pm.handle_patient_message(
        c, app=None, text="уролог отменил Престариум, теперь Лозап 50", ts="t2"))
    assert spy["multi_agent"] == 1
    assert spy["perdoc_args"] is not None
    assert "со слов" in spy["perdoc_args"]["source"].lower()
    # raw сохраняет исходный текст пациентки
    assert "Престариум" in spy["perdoc_args"]["raw"]
    # профили обновлены и reconcile-hook вызван
    assert spy["refresh_args"] == ["nephrologist"]
    assert spy["reconcile_args"]["meds_changed"] == {"nephrologist"}
    assert res["intent"] == "clinical"
    assert res["reply"]


def test_question_message_replies_without_memory(spy):
    c = FakeClient(['{"intent": "question"}'])
    res = asyncio.run(pm.handle_patient_message(
        c, app=None, text="а что такое креатинин?", ts="t3"))
    assert spy["multi_agent"] == 0
    assert spy["perdoc_args"] is None
    assert res["intent"] == "question"
    assert res["reply"]
