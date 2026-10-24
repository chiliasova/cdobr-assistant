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
- Пиши просто и коротко: 2–4 предложения. Если спрашивают «как сделать» или «что нужно сделать» — дай нумерованные шаги (не больше 6), по одной ссылке на шаг.
- Если вопрос из нескольких частей и на какую-то часть во фрагментах ответа нет, так и напиши, назвав эту часть: «Как составить сопроводительное письмо, в Инструкции не регламентировано» — и не давай своих советов по этой части.
- Не переноси правило для одного вида документа на другой: если во фрагменте не назван тот вид документа, о котором спрашивают, — это не ответ на вопрос.
- Если ответ зависит от вида документа (приказ, распоряжение, положение, письмо), а вид не назван — дай общий порядок и в конце попроси уточнить вид документа.
- После каждого предложения ставь ссылку на фрагмент, из которого оно взято, в квадратных скобках точно как в заголовке фрагмента, например [п. 10.3]. Не ссылайся на фрагмент, в котором этого нет.
- Сроки, числа, условия («для документов от органов власти» и т. п.) и названия подразделений переписывай точно как во фрагменте. Если срок действует только для части документов (например, «поступающие от органов государственной власти»), назови это условие в том же предложении.
- Не добавляй общие положения, которые не отвечают на вопрос.
- Если фрагменты описывают общий порядок, который применим к вопросу (например, согласование любого проекта документа), — отвечай по ним, это и есть ответ.
- Если во фрагментах нет ответа на вопрос, ответь ровно: НЕТ ОТВЕТА"""

REFUSAL = ("В Инструкции и связанных документах я не нашёл ответа на этот вопрос. "
           "Чтобы не ввести вас в заблуждение, не буду отвечать наугад. "
           "Уточните вопрос или обратитесь в Департамент ДОУ (подразделение ДОУ вашего РО).")

DRAFT_RE = re.compile(r"подготов|состав(ь|ить)|сформир|созда(й|ть) (проект|черновик|приказ|распоряж)|"
                      r"черновик|шаблон|модул|оформи(ть)? (приказ|распоряж)", re.I)
REF_RE = re.compile(r"\[([^\[\]]{2,120})\]")
# конец предложения, но не «п. 3.2» и не «г. Москва»
SENT_RE = re.compile(r"(?<=[.!?])(?<![\s\[(]п\.)(?<![\s\[(]г\.)\s+")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _context(hits: list[dict]) -> str:
    # длинные пункты подрезаем: на CPU каждый лишний абзац — секунды ожидания
    return "\n\n".join(f"[{h['ref']}] ({h['title']})\n{_clip(h['text'], 900)}" for h in hits)


def clean(text: str, hits: list[dict]) -> str:
    """Убираем повтор вопроса в конце и приводим сокращённые ссылки к заголовкам фрагментов."""
    text = re.sub(r"^\s*Ответ\s*:\s*", "", text)
    m = re.search(r"^\s*(Вопрос|Фрагменты)\s*:", text, re.M)
    if m:
        text = text[:m.start()]
    known = {_norm_ref(h["ref"]): h["ref"] for h in hits}
    # модель пишет [п. 4.2] вместо [Методические рекомендации, п. 4.2]
    alias = {}
    for h in hits:
        short = _norm_ref(h["ref"].split(", ")[-1])
        if short not in known:
            alias.setdefault(short, h["ref"])

    def fix(m):
        parts = [p.strip() for p in re.split(r";\s*", m.group(1))]
        return "[" + "; ".join(alias.get(_norm_ref(p), p) if _norm_ref(p) not in known else p for p in parts) + "]"
    text = REF_RE.sub(fix, text).strip()
    # «…в Инструкции не регламентировано [п. 3.2]» — ссылка на пункт, где этого нет, только путает
    text = "\n".join(" ".join(REF_RE.sub("", x).replace(" .", ".").strip() if "не регламентирован" in x.lower() else x
                               for x in SENT_RE.split(line)) for line in text.split("\n"))
    return _drop_cut_tail(text)


def _drop_cut_tail(text: str) -> str:
    """Ответ оборвался на лимите токенов — убираем недописанный последний шаг или предложение."""
    if not text or re.search(r"[.!?»)\]]\s*$", text):
        return text
    lines = text.rstrip().split("\n")
    if len(lines) > 1 and re.match(r"\s*\d+[.)]\s", lines[-1]):
        return "\n".join(lines[:-1]).rstrip()
    cut = max(text.rfind(". "), text.rfind("]."), text.rfind("]"))
    return text[:cut + 2].rstrip() if cut > 0 else text


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
    body = re.sub(r"^\s*\d+[.)]\s", " ", REF_RE.sub(" ", answer), flags=re.M)  # номера шагов списка — не факты
    src = " ".join(h["text"] + " " + h["ref"] for h in hits)
    src_nums = set(NUM_RE.findall(src))
    for n in NUM_RE.findall(body):
        if n not in src_nums:
            problems.append(f"число {n} не найдено в источниках")
    return problems


COND_RE = re.compile(r"(?:поступающ|направляем|адресуем|подготовленн|исходящ|входящ)\w*\s+(?:от|в|из|для|без)\s+[^,.;—«]+", re.I)


def condition_problems(answer: str, hits: list[dict]) -> list[str]:
    """Число из пункта пересказано без условия, при котором оно действует.

    П. 6.7: «Документы, поступающие от органов государственной власти с отметкой «Срочно», — 3 рабочих дня».
    Ответ «Документы с отметкой «Срочно» — 3 рабочих дня» теряет условие и вводит в заблуждение.
    """
    problems = []
    src_sents = [x for h in hits for x in SENT_RE.split(h["text"])]
    for sent in SENT_RE.split(answer):
        body = REF_RE.sub(" ", sent)
        words = set(search.tokenize(body))
        for n in set(NUM_RE.findall(body)):
            for ss in src_sents:
                if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", ss):
                    continue
                for cond in COND_RE.findall(ss):
                    need = [w for w in search.tokenize(cond) if len(w) > 3]
                    if need and sum(w in words for w in need) / len(need) < 0.6:
                        problems.append(f"условие «{cond.strip()}» потеряно")
    return sorted(set(problems))


def _support_problems(answer: str, hits: list[dict]) -> list[str]:
    """Предложение со ссылкой должно по словам совпадать с пунктом, на который ссылается."""
    by_ref: dict[str, set] = {}
    for h in hits:  # длинный пункт разбит на части с одной ссылкой — объединяем
        by_ref.setdefault(_norm_ref(h["ref"]), set()).update(search.tokenize(h["title"] + " " + h["text"]))
    problems = []
    for sent in SENT_RE.split(answer):
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


def _bigrams(text: str) -> set[tuple[str, str]]:
    t = search.tokenize(text)
    return set(zip(t, t[1:]))


DOC_NOUN_RE = re.compile(r"письмо|записка|приказ|распоряжение|положение|регламент|порядок|правило|протокол|"
                         r"соглашение|договор|акт|справка|разъяснение|рекомендация|постановление|телеграмма|доверенность")


def _drop_foreign(text: str, hits: list[dict], question: str) -> str:
    """Убираем предложения, где требование к одному виду документа перенесено на другой.

    Если в предложении есть термин из вопроса («сопроводительное письмо»), а в пункте,
    на который оно ссылается, этого термина нет — модель подставила чужое правило.
    """
    if not question:
        return text
    q = _bigrams(question)
    by_ref: dict[str, set] = {}
    for h in hits:
        by_ref.setdefault(_norm_ref(h["ref"]), set()).update(_bigrams(h["title"] + " " + h["text"]))
    kept, dropped = [], False
    for line in text.split("\n"):
        out = []
        for sent in SENT_RE.split(line):
            refs = [_norm_ref(p) for r in REF_RE.findall(sent) for p in re.split(r";\s*", r)]
            src = set().union(*(by_ref[r] for r in refs if r in by_ref)) if refs else set()
            # смотрим только на названия видов документов («сопроводительное письмо», «служебная записка»)
            terms = {b for b in _bigrams(REF_RE.sub(" ", sent)) & q if DOC_NOUN_RE.match(b[1])}
            if refs and terms and not (terms & src):
                dropped = True
                continue
            out.append(sent)
        if out or not line.strip():
            kept.append(" ".join(out))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if dropped and "не регламентирован" not in text.lower():
        text += "\n\nПо остальной части вопроса в Инструкции порядок не регламентирован."
    return text


def _module_hint(question: str, hits: list[dict]) -> dict | None:
    # модули есть только для приказов и распоряжений: «как составить письмо» — не про модуль
    if not DRAFT_RE.search(question) or not re.search(r"приказ|распоряж|модул|шаблон", question, re.I):
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
    # Склеиваем только явные уточнения: «а для …», «а если …», «и …», «то же для …» — иначе
    # «какая погода в Ростове?» после вопроса о сроках искалась бы вместе с ним
    prev = [h["content"] for h in history if h.get("role") == "user"][-1:]
    follow = re.match(r"\s*(а|и|но|то же|тоже|а что|а как|а если|а для|для|в случае|если)\b", question, re.I)
    return (prev[0] + " " + question) if prev and len(question) < 60 and follow else question


CHECK_RE = re.compile(r"провер(ь|ьте|ить|ка)\b.*(приказ|распоряж|проект|документ|черновик)|"
                      r"(приказ|распоряж|проект|документ).*провер(ь|ьте|ить)", re.I)
REMARKS_RE = re.compile(r"замечани.*(реестр|таблиц|собрат|оформ|свод)|(реестр|таблиц|свод).*замечани", re.I)

NEXT_STEPS_LIST = ("Что дальше:\n"
                   "1. Сверьте проект с модулем и завизируйте его как ответственный исполнитель [Методические рекомендации, п. 4.1].\n"
                   "2. Визирование по порядку: исполнитель и руководитель подразделения, лингвистическая экспертиза, "
                   "визы по Матрице, Департамент ДОУ [Методические рекомендации, п. 4.2].\n"
                   "3. Подписание в ИИС ДО [Методические рекомендации, п. 5.1].\n"
                   "4. Регистрация, затем рассылка и контроль [Методические рекомендации, п. 5.2].")


APPROVAL_RE = re.compile(r"согласов|визир", re.I)
DOC_KINDS = {"приказ": "приказ по основной деятельности", "распоряж": "распоряжение", "соглашени": "соглашение",
             "методическ": "методические рекомендации", "положени": "положение", "регламент": "регламент",
             "порядк": "порядок", "порядок": "порядок", "правил": "правила", "протокол": "протокол",
             "информационн": "информационное письмо", "разъяснени": "официальное разъяснение",
             "модул": "проект модуля", "концепц": "аналитический документ или концепция", "письм": "письмо"}


DOC_RE = re.compile("|".join(DOC_KINDS), re.I)


def _terms() -> dict[str, str]:
    """Сроки рассмотрения из п. 10.3: «положение» → «10 рабочих дней»."""
    c = next((c for c in search.get_index().chunks if c.ref == "п. 10.3"), None)
    return dict(re.findall(r"•\s*([^—•]+?)\s+—\s+(\d+ рабоч\w+ дн\w+)", c.text)) if c else {}


REMARKS_ANSWER = (
    "Замечания всех подразделений сводятся в таблицу замечаний по форме Приложения № 1 [Приложение № 1]:\n"
    "1. Графы: номер замечания; автор и подразделение; структурная единица или фрагмент ПД; содержание замечания; "
    "предложенная редакция; позиция ответственного исполнителя; результат — принято, частично принято, отклонено; "
    "обоснование результата; признак неурегулированного замечания [Приложение № 1].\n"
    "2. Замечания фиксируются в ИИС ДО либо отдельным файлом, результат рассмотрения оформляется визой «Согласовано», "
    "«Согласовано с замечаниями» или «Не согласовано» [п. 10.8].\n"
    "3. Замечания вне компетенции подразделения или без предложения по существу исполнитель вправе не учитывать, "
    "отразив причину в таблице [п. 10.4].\n"
    "4. При повторном рассмотрении таблица обязательна: без неё ПД могут вернуть без рассмотрения или поставить визу "
    "«Не согласовано» [п. 10.5].\n"
    "5. По неурегулированным замечаниям исполнитель до подписания сообщает подписанту их суть, позиции и последствия; "
    "подписант решает, провести ли согласительное совещание, доработать ПД или подписать при разногласиях [п. 10.9]. "
    "Итоги совещания отражаются в обновлённой таблице замечаний [п. 10.10].\n\n"
    "Пустую таблицу по форме Приложения № 1 можно скачать кнопкой ниже.")

APPROVAL_STEPS = (
    "1. Ответственный исполнитель оформляет ПД, согласует его внутри своего подразделения, определяет заинтересованные "
    "подразделения с учетом Матрицы обязательного визирования и одновременно направляет ПД во все заинтересованные "
    "подразделения с отметкой «Первичное рассмотрение» [п. 10.2].\n"
    "2. Каждое подразделение рассматривает ПД только в пределах компетенции [п. 10.4]; руководитель подразделения "
    "персонально отвечает за организацию рассмотрения и соблюдение срока [п. 10.14].\n"
    "3. При повторном рассмотрении таблица замечаний обязательна [п. 10.5]; существенные разногласия рассматриваются "
    "на согласительном совещании [п. 10.10].\n"
    "4. Визирование — строго по порядку: исполнитель, работники и руководитель ответственного подразделения, "
    "заинтересованные подразделения, подразделения обязательного визирования по Матрице, юридическое подразделение, "
    "Департамент ДОУ, координирующее должностное лицо [п. 10.6].\n"
    "5. Затем лингвистическая экспертиза, подписание и регистрация [п. 10.2].")


def _approval_suffix(question: str) -> str:
    terms = _terms()
    if not terms:
        return ""
    q = question.lower()
    kind = next((v for k, v in DOC_KINDS.items() if k in q and v in terms), None)
    if kind:
        text = (f"Срок: каждое подразделение рассматривает {kind.split(' или ')[0]} не более {terms[kind]} "
                "со дня поступления в ИИС ДО — и при первичном, и при повторном рассмотрении [п. 10.3].")
    else:
        common = [f"{k} — {v}" for k, v in terms.items()
                  if k in ("положение", "регламент", "приказ по основной деятельности", "методические рекомендации",
                           "соглашение", "распоряжение")]
        text = ("Сроки зависят от вида документа. Каждое подразделение рассматривает проект не дольше: "
                + "; ".join(common) + " [п. 10.3]. Уточните вид документа (положение, регламент, порядок, приказ) — "
                "назову точный срок.")
    text += (" Подписание — как правило, не более трёх рабочих дней после направления подписанту [п. 10.15]. "
             "Какие подразделения визируют обязательно, покажет вкладка «Маршрут визирования» по Матрице [п. 10.11].")
    return text


def _draft_suffix(module: dict) -> str:
    """Добавка к ответу о подготовке документа: модуль и визы по Матрице (без LLM)."""
    text = (f"Для такого документа есть модуль «{module['title']}». Проект, строго соответствующий модулю, "
            "не направляется на повторное полное согласование [Методические рекомендации, п. 1.3].")
    row = modules.MATRIX_ROW.get(module["module_id"])
    if row:
        r = routing.route(*row)
        names = [s["name"] for s in r["steps"] if s["stage"] == "визирование"]
        text += f" Обязательные визы: {', '.join(names)} [{r['ref']}]."
        if r["questions"]:
            text += (" Может понадобиться ещё виза по условию Матрицы («" + "», «".join(q["text"] for q in r["questions"])
                     + "») — отметьте это на вкладке «Маршрут визирования».")
    text += (" Как удобнее: скачать пустой шаблон и заполнить самостоятельно или подготовить проект со мной — "
             "я задам вопросы по каждому полю?")
    return text


def prepare(question: str, history: list[dict] | None = None) -> dict:
    history = history or []
    hits = search.get_index().search(_query(question, history), k=config.TOP_K + 2)
    best = hits[0]["relevance"] if hits else 0
    found = bool(best >= config.MIN_RELEVANCE)
    module = _module_hint(question, hits)
    direct, suffix, actions = None, "", []
    if CHECK_RE.search(question):
        # «Проверь мой проект» — без LLM: просим файл и сразу говорим, что дальше
        found = True
        direct = ("Загрузите проект в формате .docx кнопкой ниже. Проверю незаполненные поля, оставшиеся элементы шаблона "
                  "(линии, сноски, гриф «УТВЕРЖДАЮ», пометку «МОДУЛЬНЫЙ ДОКУМЕНТ»), сохранность служебных зон и "
                  "совпадение постоянных формулировок с модулем [Методические рекомендации, п. 3.3].\n\n" + NEXT_STEPS_LIST)
        actions.append({"type": "check", "label": "Загрузить проект на проверку"})
    elif module and found:
        suffix = _draft_suffix(module)
        actions += [{"type": "draft", "label": "Подготовить проект со мной", "module_id": module["module_id"]},
                    {"type": "template", "label": "Скачать пустой шаблон", "module_id": module["module_id"]}]
    elif (APPROVAL_RE.search(_query(question, history)) and not module and not _route_hint(hits)
          and re.search(r"\bкак\b|порядо?к|организ|срок|а для|а если", question, re.I)):
        # Общий порядок согласования — из главы 10 без пересказа моделью: так быстрее (нет минуты ожидания)
        # и без риска перепутать сроки. «А для положения?» после такого вопроса — тот же сценарий.
        found = True
        terms = _approval_suffix(question if DOC_RE.search(question) else _query(question, history))
        # спросили о конкретном виде документа — срок первым, общий порядок после
        if DOC_RE.search(question) and _query(question, history) != question:
            direct = terms  # уточнение «а для положения?» — общий порядок уже был в прошлом ответе
        elif DOC_RE.search(question):
            direct = terms + "\n\n" + APPROVAL_STEPS
        else:
            direct = APPROVAL_STEPS + "\n\n" + terms
    if REMARKS_RE.search(question) and not direct:
        # реестр замечаний — по пунктам главы 10 и Приложению № 1 без пересказа моделью
        found = True
        direct = REMARKS_ANSWER
        actions.append({"type": "remarks", "label": "Скачать таблицу замечаний (Прил. № 1)"})
    # Для типовых задач добираем фрагменты, без которых ответ неполный
    ix = search.get_index()
    extra = []
    if module and found:
        extra = ix.search(question + " порядок подготовки проекта на основе модуля карточка визирование",
                          k=3, sources={"methodology"})
    if APPROVAL_RE.search(_query(question, history)) and not module and not direct:
        # общий порядок согласования: кто готовит, последовательность виз, Матрица, сроки
        want = ("п. 10.2", "п. 10.6", "п. 10.11", "п. 10.14", "п. 10.5")
        extra = [{**c.to_dict(), "relevance": 1.0} for r in want for c in ix.chunks if c.ref == r]
    if REMARKS_RE.search(question):
        extra = ix.search("таблица замечаний по приложению 1 систематизирует замечания повторное рассмотрение",
                          k=3, sources={"instruction"})
    seen = set()
    merged = []
    for h in extra + hits:
        if h["id"] not in seen and h["source"] != "module":  # шаблоны сбивают модель, про модуль скажет suffix
            seen.add(h["id"])
            merged.append(h)
    route = _route_hint(merged[:config.TOP_K]) or (_route_from_module(module) if module else None)
    if module and found:
        # визы по Матрице уже перечислены в suffix — строку таблицы модель только пересказывает с обрывом
        merged = [h for h in merged if h["source"] != "matrix"]
    hits = merged[:config.TOP_K]
    fallback = APPROVAL_STEPS if APPROVAL_RE.search(_query(question, history)) and not module and not direct else None
    return {"hits": hits, "found": found, "module": module, "direct": direct, "question": question, "fallback": fallback, "suffix": suffix.strip(),
            "actions": actions, "route": route,
            "messages": [{"role": "system", "content": SYSTEM},
                         *[{"role": h["role"], "content": h["content"]} for h in history[-4:]],
                         {"role": "user", "content": f"Фрагменты:\n\n{_context(hits)}\n\nВопрос: {question}"}]}


def _route_from_module(module: dict) -> dict | None:
    row = modules.MATRIX_ROW.get(module["module_id"])
    return {"group": row[0], "row": row[1]} if row else None


def _clip(t: str, n: int = 450) -> str:
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " …"


def extractive(hits: list[dict], n: int = 2) -> str:
    """Запасной ответ без LLM: дословные пункты (шаблоны модулей не цитируем)."""
    hits = [h for h in hits if h["source"] != "module"] or hits
    parts = [f"{_clip(h['text'])} [{h['ref']}]" for h in hits[:n]]
    return "Вот что сказано в документах:\n\n" + "\n\n".join(parts)


def finalize(prep: dict, text: str | None) -> dict:
    res = _finalize(prep, text)
    ok = res["status"] != "no_answer"
    res["route"] = prep["route"] if ok and not prep.get("direct") else None  # вид документа ещё неизвестен
    res["actions"] = prep["actions"] if ok else []
    if ok and prep.get("suffix") and not prep.get("direct"):
        res["answer"] += "\n\n" + prep["suffix"]
    if ok:
        # пункты из добавки и готового ответа тоже делаем кликабельными
        have = {s["ref"] for s in res["sources"]}
        for r in REF_RE.findall(res["answer"]):
            for part in re.split(r";\s*", r):
                c = next((c for c in search.get_index().chunks if c.ref == part.strip()), None)
                if c and c.ref not in have:
                    have.add(c.ref)
                    res["sources"] = res["sources"] + [{**c.to_dict(), "relevance": None}]
    return res


def _finalize(prep: dict, text: str | None) -> dict:
    hits = prep["hits"]
    if not prep["found"]:
        return {"answer": REFUSAL, "status": "no_answer", "sources": [], "module": None, "problems": []}
    if prep.get("direct"):
        return {"answer": prep["direct"], "status": "verified", "sources": [], "module": prep["module"],
                "problems": []}
    if text is None:
        return {"answer": extractive(hits), "status": "extractive", "sources": hits,
                "module": prep["module"], "problems": ["LLM недоступна — показаны дословные пункты"]}
    text = clean(text, hits)
    if "НЕТ ОТВЕТА" in text.upper() or not text:
        if prep.get("fallback"):
            # маленькая модель иногда отказывается, хотя пункты найдены, — даём порядок по ним дословно
            return {"answer": prep["fallback"], "status": "verified", "sources": hits, "module": prep["module"],
                    "problems": []}
        if prep.get("suffix") or (hits and hits[0]["relevance"] >= 0.75):
            # пункт явно по теме, а модель отказалась — показываем его дословно, а не отказ
            return {"answer": extractive(hits), "status": "extractive", "sources": hits, "module": prep["module"],
                    "problems": []}
        return {"answer": REFUSAL, "status": "no_answer", "sources": [], "module": None, "problems": []}
    text = _drop_foreign(text, hits, prep.get("question", ""))
    lost = condition_problems(text, hits)
    if lost:
        # срок без условия хуже, чем отсутствие пересказа: показываем пункты дословно
        conds = [re.search(r"«(.+)»", p).group(1) for p in lost]
        cited = [h for h in hits if any(c in h["text"] for c in conds)] or hits
        return {"answer": "Срок зависит от условий, поэтому привожу пункт дословно:\n\n"
                + "\n\n".join(f"{h['text']} [{h['ref']}]" for h in cited[:1]),
                "status": "extractive", "sources": hits, "module": prep["module"], "problems": lost}
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
            text = llm.chat(prep["messages"], max_tokens=500)
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
         "Значение должно грамматически продолжать текст перед ___ и согласовываться с текстом после "
         "(нужный падеж: «___ обеспечить» — кому? «Департаменту ИТ»). Для полей с вариантами выбери ровно один вариант. "
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
        if slot.get("options"):
            # выбор из вариантов шаблона: принимаем только один из них
            val = next((o for o in slot["options"] if o.lower() in val.lower()), "")
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
    val = re.sub(r"\s+", " ", val).strip(" :,«»\"'")
    if after.lstrip()[:1] in (")", "»"):
        val = val.rstrip(")»")  # «ЭАД)» перед «»)» шаблона
    # кавычки внутри значения должны быть парными: «… архива «Электронный архив»
    if val.count("«") > val.count("»"):
        val += "»"
    elif val.count("»") > val.count("«"):
        val = val.replace("»", "", val.count("»") - val.count("«"))
    if val and after.lstrip().startswith("»") and not context.rstrip().endswith("«"):
        val = "«" + val  # в шаблоне «(далее - ___»)» открывающая кавычка за пользователем
    return val
