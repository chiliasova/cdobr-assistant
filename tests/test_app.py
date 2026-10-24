import os

os.environ["USE_EMBEDDINGS"] = "0"

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ingest, main, modules, routing
from tests.fake_ollama import handler

_real = httpx.Client


@pytest.fixture(autouse=True)
def fake_ollama(monkeypatch, tmp_path):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _real(transport=transport).post(url, **kw))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _real(transport=transport).get(url, **kw))

    def stream(method, url, **kw):
        return _real(transport=transport).stream(method, url, **kw)
    monkeypatch.setattr(httpx, "stream", stream)
    monkeypatch.setattr(main.config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(main.config, "OUTPUT_DIR", tmp_path / "drafts")


@pytest.fixture
def client():
    c = TestClient(main.app)
    assert c.post("/api/login", json={"login": "employee", "password": "employee"}).status_code == 200
    return c


def test_instruction_points():
    chunks = ingest.load_all()
    points = {c.meta.get("point") for c in chunks if c.source == "instruction"}
    assert {"1.1", "6.7", "10.3", "12.16"} <= points
    assert "2.13" in points  # и не задублирован ссылкой «по пункту 2.13.» из 12.13
    assert sum(1 for c in chunks if c.id == "instr:2.13") == 1


def test_login_required():
    assert TestClient(main.app).post("/api/ask", json={"question": "x"}).status_code == 401


def test_answer_has_reference(client):
    r = client.post("/api/ask", json={"question": "Какой срок рассмотрения проекта приказа при согласовании?"}).json()
    assert r["status"] == "verified", r
    assert r["sources"][0]["ref"].startswith("п. 10")


def test_refusal_on_offtopic(client):
    r = client.post("/api/ask", json={"question": "рецепт борща"}).json()
    assert r["status"] == "no_answer"


def test_stream(client):
    with client.stream("POST", "/api/ask/stream", json={"question": "Что делать с документом с отметкой Срочно?"}) as r:
        body = "".join(r.iter_text())
    assert "event: sources" in body and "event: done" in body


def test_verify_catches_fake_numbers():
    hits = [{"ref": "п. 10.3", "text": "срок 10 рабочих дней", "title": ""}]
    from app import answer
    assert answer.verify("Срок 10 рабочих дней [п. 10.3]", hits) == []
    assert answer.verify("Срок 15 рабочих дней [п. 10.3]", hits)
    assert answer.verify("Срок 10 рабочих дней [п. 99.1]", hits)


def test_module_fill_and_check(client):
    mid = "Модуль_распоряжения_о_рабочей_группе"
    slots = client.get(f"/api/modules/{mid}").json()["slots"]
    assert len(slots) >= 10
    values = {s["id"]: ("15.12.2026" if s["kind"] == "date" else "тест") for s in slots}
    r = client.post(f"/api/modules/{mid}/fill", json={"values": values}).json()
    assert r["warnings"] == [] and r["check"] == []
    assert client.get(r["url"]).status_code == 200


def test_all_modules_have_slots():
    for m in modules.list_modules():
        assert modules.get_slots(m["id"]), m


def test_route_working_group():
    r = routing.route("Внутренние документы", 6, {"it_project": True})
    names = [s["name"] for s in r["steps"]]
    assert "Кадровое подразделение" in names
    assert "Департамент информационных технологий" in names
    assert "Юридическое подразделение" not in names
    r = routing.route("Внутренние документы", 6, {})
    assert "Департамент информационных технологий" not in [s["name"] for s in r["steps"]]


def test_bad_date_warns():
    _, warnings = modules.fill("Модуль_распоряжения_о_рабочей_группе", {"s7": "030101"})
    assert any("не распознана" in w for w in warnings)


def test_truncated_json_is_salvaged(monkeypatch):
    from app import llm
    cut = '{\n  "s1": "внедрению CRM-системы",\n  "s2": "подготовки к \\"внедрению\\"",\n  "s3": "внедр'
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"message": {"content": cut}, "done_reason": "length"}, request=httpx.Request("POST", url)))
    assert llm.chat_json([], {}) == {"s1": "внедрению CRM-системы", "s2": 'подготовки к "внедрению"'}


def test_extract_slots(client):
    mid = "Модуль_распоряжения_о_рабочей_группе"
    r = client.post(f"/api/modules/{mid}/extract", json={"description": "рабочая группа по CRM"}).json()
    assert r["values"] == {"s1": "внедрению CRM-системы", "s7": "15.12.2026"}


def test_template_and_remarks(client):
    r = client.get("/api/modules/Модуль_приказа_о_вводе_ИС/template")
    assert r.status_code == 200 and len(r.content) > 1000
    r = client.get("/api/remarks-template")
    assert r.status_code == 200


def test_check_detects_module_and_leftovers(client):
    src = modules.module_path("Модуль_приказа_о_вводе_ИС")
    r = client.post("/api/check", files={"file": ("p.docx", src.read_bytes())}).json()
    assert r["module"]["id"] == "Модуль_приказа_о_вводе_ИС"
    assert any("МОДУЛЬНЫЙ" in i["message"] for i in r["issues"])
    assert r["next_steps"]


def test_intents(client):
    r = client.post("/api/ask", json={"question": "Мне нужно подготовить приказ о вводе информационной системы в эксплуатацию. Что для этого нужно сделать?"}).json()
    assert {a["type"] for a in r["actions"]} == {"draft", "template"}
    assert "Департамент информационной безопасности" in r["answer"]
    r = client.post("/api/ask", json={"question": "Я подготовил приказ на основе модуля. Проверь его"}).json()
    assert r["actions"][0]["type"] == "check"
    r = client.post("/api/ask", json={"question": "Как оформить реестр замечаний к проекту документа?"}).json()
    assert any(a["type"] == "remarks" for a in r["actions"])


def test_clean_echo_and_short_refs():
    from app import answer
    hits = [{"ref": "п. 9.5.6"}, {"ref": "Методические рекомендации, п. 4.2"}]
    t = answer.clean("1. Визы ставят по порядку [п. 4.2].\n2. Пункты [п. 9.5.6].\n\nВопрос: что делать?", hits)
    assert "Вопрос" not in t
    assert "[Методические рекомендации, п. 4.2]" in t and "[п. 9.5.6]" in t


def test_cut_tail_removed():
    from app import answer
    t = answer._drop_cut_tail("1. Шаг один [п. 9.5.6].\n2. Шаг два [п. 4.2].\n3. Убедитесь, что приказ включает и Департ")
    assert t.endswith("[п. 4.2].")
    assert answer._drop_cut_tail("Срок три дня [п. 6.7]. Затем обра") == "Срок три дня [п. 6.7]."


def test_step_numbers_not_checked():
    from app import answer
    hits = [{"ref": "п. 6.7", "title": "Сроки", "text": "Срочный документ исполняется в течение трех рабочих дней."}]
    t = "1. Срочный документ исполняется в течение трех рабочих дней [п. 6.7].\n2. Срочный документ исполняется в течение трех рабочих дней [п. 6.7]."
    assert not any("число" in p for p in answer.verify(t, hits))
