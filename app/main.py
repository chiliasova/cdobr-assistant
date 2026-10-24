"""Веб-сервер помощника: API + статичный интерфейс.

Запуск: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import answer, config, llm, modules, routing, search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_):
    search.get_index()  # прогрев: разбор источников и векторный индекс
    if config.USE_LLM:
        import threading
        threading.Thread(target=llm.warmup, daemon=True).start()
    yield


app = FastAPI(title="Помощник по Инструкции ДОУ", lifespan=lifespan)
STATIC = config.ROOT / "static"


# ---- Журнал вопросов (для аналитики и обратной связи) -----------------------------

def db():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.execute("""create table if not exists log(
        id integer primary key, ts real, user text, question text, answer text,
        status text, refs text, rating integer)""")
    return con


def log_question(user, q, res) -> int:
    with db() as con:
        cur = con.execute("insert into log(ts,user,question,answer,status,refs) values(?,?,?,?,?,?)",
                          (time.time(), user, q, res["answer"], res["status"],
                           json.dumps([s["ref"] for s in res["sources"]], ensure_ascii=False)))
        return cur.lastrowid


# ---- Авторизация: подписанная cookie, две роли ------------------------------------

def _sign(user: str, role: str) -> str:
    payload = f"{user}:{role}"
    sig = hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def current_user(request: Request) -> dict:
    token = request.cookies.get("session", "")
    try:
        user, role, sig = token.split(":")
    except ValueError:
        raise HTTPException(401, "Нужно войти")
    if not hmac.compare_digest(_sign(user, role), token):
        raise HTTPException(401, "Нужно войти")
    return {"user": user, "role": role}


def admin(u=Depends(current_user)):
    if u["role"] != "doc_admin":
        raise HTTPException(403, "Только для сотрудников Департамента ДОУ")
    return u


class Login(BaseModel):
    login: str
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    rec = config.USERS.get(body.login)
    if not rec or not hmac.compare_digest(rec[0], body.password):
        raise HTTPException(401, "Неверный логин или пароль")
    response.set_cookie("session", _sign(body.login, rec[1]), httponly=True, samesite="lax")
    return {"user": body.login, "role": rec[1]}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(u=Depends(current_user)):
    return u


# ---- Вопросы и ответы ---------------------------------------------------------------

class Ask(BaseModel):
    question: str
    history: list[dict] = []


@app.post("/api/ask")
def ask(body: Ask, u=Depends(current_user)):
    res = answer.ask(body.question, body.history)
    res["log_id"] = log_question(u["user"], body.question, res)
    return res


@app.post("/api/ask/stream")
def ask_stream(body: Ask, u=Depends(current_user)):
    """SSE: сначала источники, потом токены модели, в конце проверенный ответ."""
    prep = answer.prepare(body.question, body.history)

    def gen():
        yield _sse("sources", {"sources": prep["hits"], "found": prep["found"]})
        text = None
        if prep["found"] and config.USE_LLM and not prep.get("direct"):
            try:
                parts = []
                for tok in llm.chat_stream(prep["messages"]):
                    parts.append(tok)
                    yield _sse("token", {"t": tok})
                text = "".join(parts).strip()
            except Exception as e:
                logging.warning("LLM недоступна: %s", e)
        res = answer.finalize(prep, text)
        res["log_id"] = log_question(u["user"], body.question, res)
        yield _sse("done", res)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class Feedback(BaseModel):
    log_id: int
    rating: int  # 1 — полезно, -1 — нет


@app.post("/api/feedback")
def feedback(body: Feedback, u=Depends(current_user)):
    with db() as con:
        con.execute("update log set rating=? where id=?", (body.rating, body.log_id))
    return {"ok": True}


@app.get("/api/source/{chunk_id:path}")
def source(chunk_id: str, u=Depends(current_user)):
    c = search.get_index().get(chunk_id)
    if not c:
        raise HTTPException(404)
    # соседние части того же пункта (длинные пункты разбиты на куски)
    base = chunk_id.split("#")[0]
    parts = [x.text for x in search.get_index().chunks if x.id.split("#")[0] == base]
    return {**c.to_dict(), "full_text": " ".join(parts)}


# ---- Модули и черновики -------------------------------------------------------------

@app.get("/api/modules")
def list_modules(u=Depends(current_user)):
    return modules.list_modules()


@app.get("/api/modules/{module_id}")
def module_slots(module_id: str, u=Depends(current_user)):
    try:
        slots = [s.__dict__ for s in modules.get_slots(module_id)]
    except KeyError:
        raise HTTPException(404)
    row = modules.MATRIX_ROW.get(module_id)
    return {"id": module_id, "slots": slots, "matrix": {"group": row[0], "row": row[1]} if row else None}


class Describe(BaseModel):
    description: str


@app.post("/api/modules/{module_id}/extract")
def module_extract(module_id: str, body: Describe, u=Depends(current_user)):
    slots = [s.__dict__ for s in modules.get_slots(module_id)]
    try:
        return {"values": answer.extract_slots(body.description, slots)}
    except Exception as e:
        raise HTTPException(503, f"Локальная модель недоступна: {e}")


class Fill(BaseModel):
    values: dict[str, str]


@app.post("/api/modules/{module_id}/fill")
def module_fill(module_id: str, body: Fill, u=Depends(current_user)):
    try:
        path, warnings = modules.fill(module_id, body.values)
    except KeyError:
        raise HTTPException(404)
    return {"file": path.name, "url": f"/api/drafts/{path.name}", "warnings": warnings,
            "check": modules.check_draft(path)}


@app.get("/api/modules/{module_id}/template")
def module_template(module_id: str, u=Depends(current_user)):
    """Пустой модуль — для тех, кто заполняет сам."""
    try:
        p = modules.module_path(module_id)
    except KeyError:
        raise HTTPException(404)
    return FileResponse(p, filename=p.name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/api/remarks-template")
def remarks_template(u=Depends(current_user)):
    p = modules.remarks_template()
    return FileResponse(p, filename=p.name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/api/drafts/{name}")
def draft(name: str, u=Depends(current_user)):
    p = (config.OUTPUT_DIR / name).resolve()
    if p.parent != config.OUTPUT_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


class RouteReq(BaseModel):
    group: str
    row: int
    flags: dict[str, bool] = {}
    modular: bool = True


@app.get("/api/matrix")
def matrix(u=Depends(current_user)):
    return [{"group": r["meta"]["group"], "row": r["meta"]["row"],
             "title": r["text"].split(". Юридическое")[0]} for r in routing.matrix_rows()]


@app.post("/api/route")
def route(body: RouteReq, u=Depends(current_user)):
    try:
        return routing.route(body.group, body.row, body.flags, body.modular)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/check")
async def check(file: UploadFile = File(...), u=Depends(current_user)):
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Нужен файл .docx")
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(await file.read())
    try:
        return modules.analyze_draft(Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# ---- Служебное ----------------------------------------------------------------------

@app.get("/api/status")
def status():
    return {**search.get_index().stats(), "llm": llm.available(), "model": config.LLM_MODEL}


@app.post("/api/admin/reindex")
def reindex(u=Depends(admin)):
    return search.get_index(rebuild=True).stats()


@app.get("/api/admin/log")
def admin_log(u=Depends(admin)):
    with db() as con:
        rows = con.execute("select id,ts,user,question,status,rating from log order by id desc limit 200").fetchall()
    return [dict(zip(["id", "ts", "user", "question", "status", "rating"], r)) for r in rows]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
