"""Вопрос сотрудника — к словам Инструкции: опечатки, разговорные слова, раскладка.

Пишут «соглосование», «сз», «ghbrfp». BM25 по леммам такие слова не находит,
поэтому до поиска правим их по словарю, собранному из самих источников.
"""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache

from . import search

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")

# разговорные слова и сокращения -> как в Инструкции
SLANG = {
    "сз": "служебная записка", "служебка": "служебная записка", "служебку": "служебную записку",
    "служебки": "служебной записки", "служебке": "служебной записке",
    "сопроводиловка": "сопроводительное письмо", "сопроводиловки": "сопроводительного письма",
    "сопроводиловку": "сопроводительное письмо", "сопровод": "сопроводительное письмо",
    "эцп": "электронная подпись", "эп": "электронная подпись",
    "док": "документ", "дока": "документа", "доком": "документом", "доке": "документе", "доку": "документу",
    "доки": "документы", "доков": "документов", "документик": "документ",
    "согласовалка": "согласование", "согласовку": "согласование", "согласовка": "согласование",
    "визирнуть": "завизировать", "чо": "что", "че": "что", "щас": "сейчас", "плз": "", "пж": "",
    "исх": "исходящий", "вх": "входящий", "входяшка": "входящий документ", "входящку": "входящий документ",
    "рег": "регистрационный", "регномер": "регистрационный номер",
    "сэд": "ИИС ДО", "иисдо": "ИИС ДО",
}
LAT = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
CYR = "йцукенгшщзхъфывапролджэячсмитьбю"
LAYOUT = str.maketrans(LAT, CYR)


def _vocab() -> tuple[Counter, dict]:
    return _build(search.get_index().version)  # после «Переиндексировать» словарь собирается заново


@lru_cache(maxsize=2)
def _build(version: str) -> tuple[Counter, dict]:
    words = Counter()
    for c in search.get_index().chunks:
        words.update(w.lower().replace("ё", "е") for w in WORD_RE.findall(c.title + " " + c.text))
    # леммы тоже кандидаты: «зарегестрировать» -> «зарегистрировать», хотя в тексте только «регистрируется»
    for w in list(words):
        words[search.lemma(w)] += 0
    by_len: dict[int, list[str]] = {}
    for w in words:
        if len(w) >= 3:
            by_len.setdefault(len(w), []).append(w)
    return words, by_len


def _dist(a: str, b: str, limit: int) -> int:
    """Расстояние Дамерау — Левенштейна с ранним выходом за limit."""
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _fix(word: str) -> str | None:
    words, by_len = _vocab()
    w = word.lower().replace("ё", "е")
    if w in words:
        return None
    if re.fullmatch(r"[a-z\[\];',.]+", w):
        ru = w.translate(LAYOUT)
        # «ghbrfp» -> «приказ»; английские слова и латиницу вроде «ИС» не трогаем
        return ru if ru in words or search.lemma(ru) in _lemmas() else None
    if not re.fullmatch(r"[а-я]+", w) or len(w) < 4:
        return None
    if search._morph().word_is_known(w):
        return None  # настоящее слово («отправить», «Ростове») не подгоняем под Инструкцию
    limit = 1 if len(w) <= 6 else 2
    best, best_key = None, None
    for n in range(len(w) - limit, len(w) + limit + 1):
        for cand in by_len.get(n, ()):
            d = _dist(w, cand, limit)
            if d <= limit:
                # меньше правок, пропущенная двойная буква («групе»), общее начало слова, чаще в тексте
                key = (d, re.sub(r"(.)\1", r"\1", cand) != w, cand[:2] != w[:2], -words[cand])
                if best_key is None or key < best_key:
                    best, best_key = cand, key
    return best


def _lemmas() -> frozenset:
    return _lemma_set(search.get_index().version)


@lru_cache(maxsize=2)
def _lemma_set(version: str) -> frozenset:
    words, _ = _vocab()
    return frozenset(search.lemma(w) for w in words)


def normalize(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Возвращает исправленный вопрос и список замен (было, стало)."""
    fixes: list[tuple[str, str]] = []

    def repl(m: re.Match) -> str:
        w = m.group(0)
        low = w.lower()
        if low in SLANG:
            new = SLANG[low]
        else:
            new = _fix(w)
            if not new:
                return w
        if w[:1].isupper() and new:
            new = new[:1].upper() + new[1:]
        fixes.append((w, new))
        return new

    out = re.sub(r"[A-Za-zА-Яа-яЁё\[\];',.]+(?<![.,;'])", repl, text)
    return re.sub(r"\s{2,}", " ", out).strip(), fixes
