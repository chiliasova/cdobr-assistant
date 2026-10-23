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
