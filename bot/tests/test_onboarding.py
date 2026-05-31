"""Онбординг нового пользователя — формат «золотая середина».

Бот одним сообщением задаёт 10 вопросов, человек отвечает одним письмом,
один проход модели → профиль персоны. Только в публичном репо.
Данные в тесте — вымышленные.
"""

import json

import onboarding
import patient_identity


# ---------- текст вопросов ----------

def test_questions_message_covers_all_ten_topics():
    msg = onboarding.ONBOARDING_QUESTIONS
    for n in range(1, 11):
        assert f"{n}." in msg, f"нет пункта {n}"
    low = msg.lower()
    for kw in ("зовут", "лет", "работа", "пита", "двига",
               "привыч", "аллерг", "принима", "беспоко", "разговар"):
        assert kw in low, f"вопрос не покрывает: {kw}"


# ---------- разбор ответа в персону ----------

class _FakeClient:
    def __init__(self, text):
        self._text = text

    class _Msg:
        def __init__(self, text):
            self.content = [type("C", (), {"text": text})()]

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kw):
                return _FakeClient._Msg(outer._text)
        return _M()


def test_build_persona_parses_model_json():
    payload = json.dumps({
        "full_name": "Иванова Мария Петровна",
        "address_name": "Мария Петровна",
        "dob": "1949-03-12",
        "city": "Самара",
        "gender": "f",
        "persona_md": "## Психологический профиль\nСпокойная.\n",
    }, ensure_ascii=False)
    p = onboarding.build_persona(_FakeClient(payload), "ответ письмом")
    assert p["full_name"] == "Иванова Мария Петровна"
    assert p["address_name"] == "Мария Петровна"
    assert "Психологический профиль" in p["persona_md"]


def test_build_persona_survives_garbage_model_output():
    p = onboarding.build_persona(_FakeClient("не json вообще"), "мой ответ")
    # ничего не теряем: слова человека уходят в persona_md
    assert p["full_name"] == ""
    assert "мой ответ" in p["persona_md"]


def test_build_persona_strips_code_fences():
    fenced = "```json\n{\"full_name\": \"Тест Тест Тест\", \"persona_md\": \"x\"}\n```"
    p = onboarding.build_persona(_FakeClient(fenced), "txt")
    assert p["full_name"] == "Тест Тест Тест"


# ---------- сохранение ----------

def test_save_persona_writes_json_and_markdown(tmp_path):
    persona = {
        "full_name": "Иванова Мария Петровна",
        "address_name": "Мария Петровна",
        "dob": "1949-03-12",
        "city": "Город",
        "gender": "f",
        "persona_md": "## Психологический профиль\nтекст\n",
    }
    data_dir = tmp_path / "data"
    hist_dir = tmp_path / "history"
    jp, mp = onboarding.save_persona(persona, data_dir, hist_dir)
    assert jp.exists() and mp.exists()
    loaded = json.loads(jp.read_text(encoding="utf-8"))
    assert loaded["full_name"] == "Иванова Мария Петровна"
    assert "Психологический профиль" in mp.read_text(encoding="utf-8")


# ---------- состояние диалога ----------

def test_onboarding_state_lifecycle():
    uid = 123456
    onboarding.finish_onboarding(uid)            # чистый старт
    assert onboarding.is_awaiting(uid) is False
    onboarding.start_onboarding(uid)
    assert onboarding.is_awaiting(uid) is True
    onboarding.finish_onboarding(uid)
    assert onboarding.is_awaiting(uid) is False


# ---------- patient_identity: fallback на файл персоны ----------

def test_patient_identity_falls_back_to_persona_file(tmp_path, monkeypatch):
    for var in ("PATIENT_FULL_NAME", "PATIENT_ADDRESS_NAME",
                "PATIENT_DOB", "PATIENT_CITY", "PATIENT_GENDER"):
        monkeypatch.delenv(var, raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "patient_persona.json").write_text(json.dumps({
        "full_name": "Петров Пётр Петрович",
        "address_name": "Пётр Петрович",
        "dob": "1950-01-01",
        "city": "Тверь",
        "gender": "m",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(patient_identity, "PERSONA_JSON",
                         data_dir / "patient_persona.json")
    patient_identity._invalidate_cache()
    assert patient_identity.full_name() == "Петров Пётр Петрович"
    assert patient_identity.address_name() == "Пётр Петрович"
    assert patient_identity.city() == "Тверь"
    assert patient_identity.is_female() is False


# ---------- расширенный онбординг: состав врачей + адресат ----------

def test_questions_include_addressee_and_health_zones():
    """После дедупликации (баг dogfooding: вопросы 7/9/12 дублировались)
    — ровно 10 пунктов, здоровье одним блоком, адресат — пункт 10."""
    q = onboarding.ONBOARDING_QUESTIONS
    low = q.lower()
    assert "10." in q
    assert "11." not in q and "12." not in q          # дублей больше нет
    assert ("ухажива" in low) or ("для себя" in low)   # модель адресата
    # объединённый блок здоровья
    assert "здоровье" in low
    assert ("хронические" in low) and ("беспокоит" in low)


def test_library_specialists_lists_all_archetypes_unfiltered(tmp_path):
    """Библиотека = ВСЕ архетипы, без фильтра активного состава."""
    lib = onboarding.library_specialists()
    assert "hematologist" in lib
    assert "dental-gnathologist" in lib
    assert "eating-disorder-therapist" in lib
    assert "dietitian" in lib


def test_resolve_roster_keeps_known_and_generates_unknown(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # мини-библиотека
    for slug, name in (("hematologist", "гематолог"),
                        ("lab-analyst", "лаборант")):
        (agents_dir / f"{slug}.md").write_text(
            f"---\nslug: {slug}\nname_ru: {name}\nrole: specialist\n"
            f"domain: тест\n---\n# {name}\n", encoding="utf-8")
    specialists_dir = tmp_path / "specialists"
    parsed = {"specialists": [
        {"slug": "hematologist", "name_ru": "гематолог", "domain": "кровь"},
        {"slug": "pulmonologist", "name_ru": "пульмонолог",
         "domain": "лёгкие, бронхи, астма"},  # нет в библиотеке → генерим
    ]}
    slugs = onboarding.resolve_roster(parsed, agents_dir, specialists_dir)

    assert "hematologist" in slugs
    assert "pulmonologist" in slugs
    assert "lab-analyst" in slugs  # лаборант всегда активен
    gen = agents_dir / "pulmonologist.md"
    assert gen.exists()
    fm, _ = __import__("agents").parse_frontmatter(gen.read_text(encoding="utf-8"))
    assert fm["slug"] == "pulmonologist"
    assert fm["role"] == "specialist"
    assert fm["name_ru"] == "пульмонолог"
    # каркас папки-памяти создан (чеклист скилла)
    assert (specialists_dir / "pulmonologist" / ".gitkeep").exists()


def test_resolve_roster_sanitizes_slug_and_skips_garbage(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "lab-analyst.md").write_text(
        "---\nslug: lab-analyst\nname_ru: лаборант\nrole: specialist\n"
        "domain: x\n---\n# л\n", encoding="utf-8")
    specialists_dir = tmp_path / "specialists"
    parsed = {"specialists": [
        {"slug": "Кардио Лог!!", "name_ru": "кардиолог", "domain": "сердце"},
        {"name_ru": "", "domain": ""},  # мусор → пропуск
    ]}
    slugs = onboarding.resolve_roster(parsed, agents_dir, specialists_dir)
    assert "lab-analyst" in slugs
    # slug санитизирован к латинице-дефисам
    assert any(s and all(c.islower() or c == "-" or c.isdigit()
                         for c in s) for s in slugs)


def test_write_active_roster_writes_sorted_unique(tmp_path):
    p = onboarding.write_active_roster(
        ["hematologist", "dietitian", "hematologist"], tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == sorted(set(["hematologist", "dietitian"]))


def test_multimessage_answer_buffer():
    """Длинный ответ приходит частями, склеивается по маркеру ГОТОВО."""
    uid = 9988776
    onboarding.finish_onboarding(uid)
    onboarding.start_onboarding(uid)
    assert onboarding.add_answer_part(uid, "часть один") == 1
    assert onboarding.add_answer_part(uid, "часть два") == 2
    assert onboarding.is_finalize("ГОТОВО") is True
    assert onboarding.is_finalize("/done") is True
    assert onboarding.is_finalize("обычный текст") is False
    combined = onboarding.pop_answers(uid)
    assert "часть один" in combined and "часть два" in combined
    assert onboarding.pop_answers(uid) == ""          # буфер очищен
    onboarding.finish_onboarding(uid)


def test_start_onboarding_clears_stale_buffer():
    uid = 5566778
    onboarding.start_onboarding(uid)
    onboarding.add_answer_part(uid, "старое")
    onboarding.start_onboarding(uid)                  # новый заход
    assert onboarding.pop_answers(uid) == ""
    onboarding.finish_onboarding(uid)


def test_onboarding_max_tokens_generous():
    """2000 токенов обрезали длинный JSON персоны (баг dogfooding)."""
    assert onboarding.ONBOARDING_MAX_TOKENS >= 6000


def test_build_persona_extracts_addressee():
    payload = json.dumps({
        "full_name": "Иванова Мария Петровна",
        "addressee": "caregiver",
        "persona_md": "## Профиль\nтекст",
        "specialists": [{"slug": "hematologist", "name_ru": "гематолог",
                         "domain": "кровь"}],
    }, ensure_ascii=False)
    p = onboarding.build_persona(_FakeClient(payload), "ответ")
    assert p["addressee"] == "caregiver"
    assert p["specialists"][0]["slug"] == "hematologist"


def test_build_persona_addressee_defaults_self_on_garbage():
    p = onboarding.build_persona(_FakeClient("мусор"), "ответ письмом")
    assert p["addressee"] == "self"          # безопасный дефолт
    assert p["specialists"] == []            # состав не трогаем
    assert "ответ письмом" in p["persona_md"]


def test_save_persona_persists_addressee(tmp_path):
    persona = {
        "full_name": "Тест Тест Тест", "address_name": "Тест",
        "dob": "", "city": "", "gender": "f", "addressee": "self",
        "persona_md": "## Профиль\nтекст",
    }
    jp, mp = onboarding.save_persona(persona, tmp_path / "d", tmp_path / "h")
    loaded = json.loads(jp.read_text(encoding="utf-8"))
    assert loaded["addressee"] == "self"


def test_env_still_wins_over_persona_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PATIENT_ADDRESS_NAME", "Из Окружения")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "patient_persona.json").write_text(
        json.dumps({"address_name": "Из Файла"}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(patient_identity, "PERSONA_JSON",
                         data_dir / "patient_persona.json")
    patient_identity._invalidate_cache()
    assert patient_identity.address_name() == "Из Окружения"
