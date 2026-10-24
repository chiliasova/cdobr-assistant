"""Ответ на вопрос: поиск → (порог отказа) → пересказ локальной LLM → проверка.

Модель только пересказывает найденные фрагменты. После генерации проверяем:
каждая ссылка [п. X] должна быть среди найденных фрагментов, каждое число из
ответа должно встречаться в этих фрагментах. Если проверка не прошла —
показываем дословные пункты вместо пересказа.
"""
from __future__ import annotations

import logging
import re
import time

from . import config, llm, modules, routing, search

log = logging.getLogger(__name__)

SYSTEM = """Ты — помощник сотрудника по Инструкции о документообороте Организации.
Отвечай ТОЛЬКО по фрагментам ниже. Ничего не придумывай.
Правила:
- Фрагменты отсортированы по близости к вопросу: первый — самый подходящий.
- Начни с главного: срок, действие или ответственный. Без вступлений.
- Пиши просто и коротко: 2–4 предложения.
- После каждого предложения ставь ссылку на фрагмент, из которого оно взято, в квадратных скобках точно как в заголовке фрагмента, например [п. 10.3]. Не ссылайся на фрагмент, в котором этого нет.
- Сроки, числа, условия («для документов от органов власти» и т. п.) и названия подразделений переписывай точно как во фрагменте.
- Если во фрагментах нет ответа на вопрос, ответь ровно: НЕТ ОТВЕТА"""

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
    problems += _support_problems(answer, hits)
    body = REF_RE.sub(" ", answer)
    src = " ".join(h["text"] + " " + h["ref"] for h in hits)
    src_nums = set(NUM_RE.findall(src))
    for n in NUM_RE.findall(body):
        if n not in src_nums:
            problems.append(f"число {n} не найдено в источниках")
    return problems


def _support_problems(answer: str, hits: list[dict]) -> list[str]:
    """Предложение со ссылкой должно по словам совпадать с пунктом, на который ссылается."""
    by_ref = {_norm_ref(h["ref"]): set(search.tokenize(h["title"] + " " + h["text"])) for h in hits}
    problems = []
    for sent in re.split(r"(?<=[.!?])\s+", answer):
        refs = [_norm_ref(p) for r in REF_RE.findall(sent) for p in re.split(r";\s*", r)]
        refs = [r for r in refs if r in by_ref]
        words = [w for w in search.tokenize(REF_RE.sub(" ", sent)) if not w.isdigit() and len(w) > 2]
        if not refs or len(words) < 3:
            continue
        src = set().union(*(by_ref[r] for r in refs))
        share = sum(w in src for w in words) / len(words)
        if share < 0.45:
            problems.append(f"предложение не подтверждается {', '.join(refs)}: «{sent[:60]}…»")
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


def _draft_answer(module: dict) -> str:
    """Просьба подготовить документ: отвечаем без LLM — шаблоном и маршрутом из Матрицы."""
    text = (f"Для этого есть модуль «{module['title']}». Нажмите «Подготовить черновик»: опишите ситуацию "
            "своими словами, я разложу её по полям шаблона, а вы проверите их перед выгрузкой в .docx. "
            "Проект, строго соответствующий модулю, не направляется на повторное полное согласование "
            "[Методические рекомендации, п. 1.3].")
    row = modules.MATRIX_ROW.get(module["module_id"])
    if row:
        r = routing.route(*row)
        names = [s["name"] for s in r["steps"] if s["stage"] == "визирование"]
        text += f" Обязательные визы: {', '.join(names)} [{r['ref']}]."
        if r["questions"]:
            text += (" Может понадобиться ещё виза по условию Матрицы («" + "», «".join(q["text"] for q in r["questions"])
                     + "»): отметьте это на вкладке «Маршрут визирования».")
    return text


def prepare(question: str, history: list[dict] | None = None) -> dict:
    history = history or []
    hits = search.get_index().search(_query(question, history), k=config.TOP_K + 2)
    best = hits[0]["relevance"] if hits else 0
    found = bool(best >= config.MIN_RELEVANCE)
    module = _module_hint(question, hits)
    direct = _draft_answer(module) if module and found else None
    # Шаблоны модулей сбивают модель на обычных вопросах — в контекст их не берём
    hits = [h for h in hits if h["source"] != "module"][:config.TOP_K] if not module else hits[:config.TOP_K]
    return {"hits": hits, "found": found, "module": module, "direct": direct,
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
        return {"answer": REFUSAL, "status": "no_answer", "sources": [], "module": None, "problems": []}
    if prep.get("direct"):
        return {"answer": prep["direct"], "status": "verified", "sources": hits, "module": prep["module"],
                "problems": []}
    if text is None:
        return {"answer": extractive(hits), "status": "extractive", "sources": hits,
                "module": prep["module"], "problems": ["LLM недоступна — показаны дословные пункты"]}
    if "НЕТ ОТВЕТА" in text.upper():
        return {"answer": REFUSAL, "status": "no_answer", "sources": [], "module": None, "problems": []}
    problems = verify(text, hits)
    status = "verified" if not problems else "unverified"
    answer = text
    if problems:
        answer = text + "\n\n⚠️ Часть ответа не удалось сверить с источником. Дословно:\n\n" + extractive(hits, 1).split("\n\n", 1)[1]
    return {"answer": answer, "status": status, "sources": hits, "module": prep["module"], "problems": problems}


def ask(question: str, history: list[dict] | None = None) -> dict:
    prep = prepare(question, history)
    text = None
    if prep["found"] and config.USE_LLM and not prep.get("direct"):
        try:
            text = llm.chat(prep["messages"])
        except Exception:
            text = None
    return finalize(prep, text)


# ---- Заполнение модуля по описанию ситуации ----------------------------------------

EMPTY = {"", "-", "—", "нет", "пусто", "не указано", "нет данных"}
LINE_RE = re.compile(r"^\s*[-*]?\s*(s\d+)\s*[:=|—–-]\s*(.*?)\s*$", re.M)


def extract_slots(description: str, slots: list[dict]) -> dict[str, str]:
    """LLM раскладывает свободное описание по полям модуля. Пустые поля не выдумывает.

    Формат ответа — строки «s3: значение», а не JSON: маленькая модель в режиме
    строгого JSON на Windows зацикливалась и обрывала ответ.
    """
    fields = "\n".join(
        f"{s['id']} ({s['label']}): «{s['context'][-50:]} ___ {s.get('after', '')}»"
        + (f"; варианты: {' / '.join(s['options'])}" if s.get("options") else "")
        + ("; дата ДД.ММ.ГГГГ" if s["kind"] == "date" else "")
        + ("; здесь действие, не дата" if s["kind"] == "text" and "Срок" in s["label"] else "")
        for s in slots)
    msgs = [
        {"role": "system", "content": "Ты заполняешь поля шаблона документа по описанию пользователя. "
         "Бери сведения только из описания, ничего не придумывай. "
         "Значение должно грамматически продолжать текст перед ___ (нужный падеж). "
         "Ответ — по одной строке на поле в формате «s1: значение». В значении пиши только то, "
         "что встанет на место пропуска, без текста перед ним. Если сведений нет — «s1: -». "
         "Пример: для поля «s5 (Контроль): «возложить на ___ .»» ответ "
         "«s5: начальника Управления делами Петрова П. П.». Больше ничего не пиши."},
        {"role": "user", "content": f"Поля:\n{fields}\n\nОписание:\n{description}"},
    ]
    t = time.time()
    text = llm.chat(msgs, max_tokens=60 * len(slots) + 100)
    log.info("Поля модуля заполнены за %.1f с", time.time() - t)
    by_id = {s["id"]: s for s in slots}
    out = {}
    for sid, val in LINE_RE.findall(text):
        if sid not in by_id or sid in out:
            continue
        slot = by_id[sid]
        val = _strip_echo(val, slot["context"], slot.get("after", ""), slot["kind"])
        # текстовое поле, в которое попала одна дата, — пусть заполнит человек
        if slot["kind"] != "date" and re.fullmatch(r"[\d.]+", val):
            continue
        if val.lower().strip(".") not in EMPTY:
            out[sid] = val
    # Одинаковые значения в полях с разными подписями — модель «съехала» на соседнее поле.
    # Заголовок и п. 1 по методичке совпадают, их не трогаем; поле «Срок и действие» —
    # самое путаное, из него и убираем дубль.
    for sid, val in list(out.items()):
        twins = [o for o, v in out.items() if v == val and o != sid]
        if twins and "Срок" in by_id[sid]["label"] and all("Срок" not in by_id[t]["label"] for t in twins):
            del out[sid]
    return out


def _strip_echo(val: str, context: str, after: str = "", kind: str = "text") -> str:
    """Модель иногда повторяет текст вокруг пропуска («…возложить на директора…», «… и представить») — убираем его."""
    val = val.strip(" «»\"'")
    for piece in (context, context[-50:], context.split(":")[-1], context.split("…")[-1]):
        piece = piece.strip(" …")
        if len(piece) >= 3 and piece in val:
            val = val[val.rindex(piece) + len(piece):]
    for piece in (after, after.split("…")[0], after[:15]):
        piece = piece.strip(" …,.:;")
        if len(piece) >= 3 and piece in val:
            val = val[:val.index(piece)]
    if kind == "date":
        m = re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", val)
        val = m.group() if m else val
    val = re.sub(r"_{2,}|…", " ", val)
    return re.sub(r"\s+", " ", val).strip(" :,«»\"'")
