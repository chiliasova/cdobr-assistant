"""Ответ на вопрос: поиск → (порог отказа) → пересказ локальной LLM → проверка.

Модель только пересказывает найденные фрагменты. После генерации проверяем:
каждая ссылка [п. X] должна быть среди найденных фрагментов, каждое число из
ответа должно встречаться в этих фрагментах. Если проверка не прошла —
показываем дословные пункты вместо пересказа.
"""
from __future__ import annotations

import re

from . import config, llm, search

SYSTEM = """Ты — помощник сотрудника по Инструкции о документообороте Организации.
Отвечай ТОЛЬКО по фрагментам ниже. Ничего не придумывай.
Правила:
- Пиши просто и коротко: 2–5 предложений, без вступлений.
- После каждого утверждения ставь ссылку на источник в квадратных скобках точно как в заголовке фрагмента, например [п. 10.3].
- Сроки, числа и названия подразделений переписывай точно как во фрагменте.
- Если во фрагментах нет ответа на вопрос, ответь ровно: НЕТ ОТВЕТА
- Если из фрагментов понятно, кто отвечает за вопрос, в конце добавь строку «Куда обратиться: …»."""

REFUSAL = ("В Инструкции и связанных документах я не нашёл ответа на этот вопрос. "
           "Чтобы не ввести вас в заблуждение, не буду отвечать наугад. "
           "Уточните вопрос или обратитесь в Департамент ДОУ (подразделение ДОУ вашего РО).")

DRAFT_RE = re.compile(r"подготов|состав(ь|ить)|сформир|созда(й|ть) (проект|черновик|приказ|распоряж)|"
                      r"черновик|шаблон|модул|оформи(ть)? (приказ|распоряж)", re.I)
REF_RE = re.compile(r"\[([^\[\]]{2,120})\]")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _context(hits: list[dict]) -> str:
    return "\n\n".join(f"[{h['ref']}] ({h['title']})\n{h['text']}" for h in hits)


def _norm_ref(r: str) -> str:
    return re.sub(r"\s+", " ", r.replace("пункт", "п.").replace("п.п.", "п.")).strip(" .").lower()


def verify(answer: str, hits: list[dict]) -> list[str]:
    """Список проблем; пустой — ответ подтверждён источниками."""
    problems = []
    known = {_norm_ref(h["ref"]) for h in hits}
    refs = REF_RE.findall(answer)
    if not refs:
        problems.append("в ответе нет ссылок на пункты")
    for r in refs:
        for part in re.split(r";\s*", r):
            if _norm_ref(part) not in known:
                problems.append(f"ссылка [{part}] не из найденных фрагментов")
    body = REF_RE.sub(" ", answer)
    src = " ".join(h["text"] + " " + h["ref"] for h in hits)
    src_nums = set(NUM_RE.findall(src))
    for n in NUM_RE.findall(body):
        if n not in src_nums:
            problems.append(f"число {n} не найдено в источниках")
    return problems


def _module_hint(question: str, hits: list[dict]) -> dict | None:
    if not DRAFT_RE.search(question):
        return None
    ix = search.get_index()
    mods = ix.search(question, k=1, sources={"module"})
    if mods and mods[0]["relevance"] >= 0.3:
        return {"module_id": mods[0]["meta"]["file"].removesuffix(".docx"), "title": mods[0]["ref"]}
    return None


def _route_hint(hits: list[dict]) -> dict | None:
    """Если среди первых фрагментов есть строка Матрицы — предложим посчитать маршрут."""
    for h in hits[:3]:
        if h["source"] == "matrix" and h["meta"].get("row"):
            return {"group": h["meta"]["group"], "row": h["meta"]["row"]}
    return None


def _query(question: str, history: list[dict]) -> str:
    # Короткий уточняющий вопрос («а для распоряжения?») ищем вместе с предыдущим
    prev = [h["content"] for h in history if h.get("role") == "user"][-1:]
    return (prev[0] + " " + question) if prev and len(question) < 60 else question


def prepare(question: str, history: list[dict] | None = None) -> dict:
    history = history or []
    hits = search.get_index().search(_query(question, history), k=config.TOP_K)
    best = hits[0]["relevance"] if hits else 0
    found = bool(best >= config.MIN_RELEVANCE)
    return {"hits": hits, "found": found, "module": _module_hint(question, hits),
            "route": _route_hint(hits),
            "messages": [{"role": "system", "content": SYSTEM},
                         *[{"role": h["role"], "content": h["content"]} for h in history[-4:]],
                         {"role": "user", "content": f"Фрагменты:\n\n{_context(hits)}\n\nВопрос: {question}"}]}


def _clip(t: str, n: int = 450) -> str:
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " …"


def extractive(hits: list[dict], n: int = 2) -> str:
    """Запасной ответ без LLM: дословные пункты (шаблоны модулей не цитируем)."""
    hits = [h for h in hits if h["source"] != "module"] or hits
    parts = [f"{_clip(h['text'])} [{h['ref']}]" for h in hits[:n]]
    return "Вот что сказано в документах:\n\n" + "\n\n".join(parts)


def finalize(prep: dict, text: str | None) -> dict:
    res = _finalize(prep, text)
    res["route"] = prep["route"] if res["status"] != "no_answer" else None
    return res


def _finalize(prep: dict, text: str | None) -> dict:
    hits = prep["hits"]
    if not prep["found"]:
        return {"answer": REFUSAL, "status": "no_answer", "sources": hits[:3], "module": prep["module"],
                "problems": []}
    if text is None:
        return {"answer": extractive(hits), "status": "extractive", "sources": hits,
                "module": prep["module"], "problems": ["LLM недоступна — показаны дословные пункты"]}
    if "НЕТ ОТВЕТА" in text.upper():
        return {"answer": REFUSAL, "status": "no_answer", "sources": hits[:3], "module": prep["module"],
                "problems": []}
    problems = verify(text, hits)
    status = "verified" if not problems else "unverified"
    answer = text
    if problems:
        answer = text + "\n\n⚠️ Часть ответа не удалось сверить с источником. Дословно:\n\n" + extractive(hits, 1).split("\n\n", 1)[1]
    return {"answer": answer, "status": status, "sources": hits, "module": prep["module"], "problems": problems}


def ask(question: str, history: list[dict] | None = None) -> dict:
    prep = prepare(question, history)
    text = None
    if prep["found"] and config.USE_LLM:
        try:
            text = llm.chat(prep["messages"])
        except Exception:
            text = None
    return finalize(prep, text)


# ---- Заполнение модуля по описанию ситуации ----------------------------------------

def extract_slots(description: str, slots: list[dict]) -> dict[str, str]:
    """LLM раскладывает свободное описание по полям модуля. Пустые поля не выдумывает."""
    fields = "\n".join(
        f"- {s['id']}: {s['label']}. {s['hint']} (контекст: «{s['context']} …»)"
        + (f" Варианты: {', '.join(s['options'])}." if s.get("options") else "")
        + (" Формат: ДД.ММ.ГГГГ." if s["kind"] == "date" else "")
        for s in slots)
    schema = {"type": "object", "properties": {s["id"]: {"type": "string"} for s in slots}}
    msgs = [
        {"role": "system", "content": "Ты заполняешь поля шаблона документа по описанию пользователя. "
         "Бери сведения только из описания. Если сведения для поля нет — верни пустую строку. "
         "Пиши значения так, чтобы они грамматически продолжали контекст (в нужном падеже). "
         "Отвечай только JSON."},
        {"role": "user", "content": f"Поля:\n{fields}\n\nОписание:\n{description}"},
    ]
    data = llm.chat_json(msgs, schema, max_tokens=800)
    return {k: str(v).strip() for k, v in data.items() if k in schema["properties"] and str(v).strip()}
