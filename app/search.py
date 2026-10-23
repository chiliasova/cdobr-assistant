"""Гибридный поиск: BM25 по леммам + векторы bge-m3, объединение через RRF.

Если Ollama с bge-m3 недоступна, поиск работает только на BM25.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from functools import lru_cache

import numpy as np

from . import config, ingest, llm

log = logging.getLogger(__name__)

WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.I)
STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только
ее мне было вот от меня еще нет о из ему теперь когда даже ну ли если уже или ни быть был него до вас
нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем
была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем
ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два
об другой хоть после над больше тот через эти нас про всего них какая много разве три эту моя впрочем
хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между это
который настоящий также либо иной соответствующий мочь нужно какой""".split())


@lru_cache(maxsize=1)
def _morph():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=100_000)
def lemma(word: str) -> str:
    return _morph().parse(word)[0].normal_form


def tokenize(text: str) -> list[str]:
    out = []
    for w in WORD_RE.findall(text.lower().replace("ё", "е")):
        if w.isdigit():
            out.append(w)
            continue
        l = lemma(w)
        if l not in STOP and len(l) > 1:
            out.append(l)
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = np.array([len(d) for d in docs], dtype=float)
        self.avg = self.len.mean()
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, q: list[str]) -> np.ndarray:
        s = np.zeros(len(self.tf))
        for t in set(q):
            idf = self.idf.get(t)
            if not idf:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(t)
                if f:
                    s[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        return s


class Index:
    def __init__(self):
        self.version = ingest.sources_fingerprint()
        self.chunks = ingest.load_all()
        # В индекс добавляем заголовок главы — помогает на общих вопросах
        self.bm25 = BM25([tokenize(c.title + " " + c.ref + " " + c.text) for c in self.chunks])
        self.vectors = self._load_vectors()

    def _load_vectors(self):
        if not config.USE_EMBEDDINGS:
            return None
        path = config.INDEX_DIR / f"vectors-{self.version}.npy"
        if path.exists():
            return np.load(path)
        try:
            texts = [f"{c.ref}. {c.text}" for c in self.chunks]
            vecs = []
            for i in range(0, len(texts), 32):
                vecs += llm.embed(texts[i:i + 32])
            arr = np.array(vecs, dtype=np.float32)
            arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9
            config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
            np.save(path, arr)
            log.info("Векторный индекс построен: %s фрагментов", len(arr))
            return arr
        except Exception as e:  # Ollama не запущена — живём на BM25
            log.warning("Векторный поиск отключён: %s", e)
            return None

    def search(self, query: str, k: int = config.TOP_K, sources: set[str] | None = None) -> list[dict]:
        bm = self.bm25.scores(tokenize(query))
        dense = None
        if self.vectors is not None:
            try:
                qv = np.array(llm.embed([query])[0], dtype=np.float32)
                qv /= np.linalg.norm(qv) + 1e-9
                dense = self.vectors @ qv
            except Exception as e:
                log.warning("Эмбеддинг запроса не получен: %s", e)

        allowed = [i for i, c in enumerate(self.chunks) if not sources or c.source in sources]
        # RRF: складываем обратные ранги из двух списков
        rrf = Counter()
        for scores in (bm, dense):
            if scores is None:
                continue
            order = sorted(allowed, key=lambda i: -scores[i])
            for r, i in enumerate(order[:50]):
                if scores[i] > 0:
                    rrf[i] += 1 / (60 + r)
        top = [i for i, _ in rrf.most_common(k)]

        # Оценка уверенности 0..1 для показа и для порога отказа
        bm_max = bm.max() or 1
        res = []
        for i in top:
            parts = [min(bm[i] / max(bm_max, 8.0), 1.0)]
            if dense is not None:
                # у bge-m3 косинус ~0.35 — «мимо», ~0.7 — «точно»
                parts.append(float(np.clip((dense[i] - 0.35) / 0.35, 0, 1)))
            rel = max(parts) if dense is None else 0.4 * parts[0] + 0.6 * parts[1]
            c = self.chunks[i]
            res.append({**c.to_dict(), "relevance": round(float(rel), 2)})
        res.sort(key=lambda r: -r["relevance"])
        return res

    def get(self, chunk_id: str):
        return next((c for c in self.chunks if c.id == chunk_id), None)

    def stats(self) -> dict:
        return {"version": self.version, "chunks": len(self.chunks),
                "by_source": dict(Counter(c.source for c in self.chunks)),
                "vectors": self.vectors is not None}


_index: Index | None = None


def get_index(rebuild: bool = False) -> Index:
    global _index
    if _index is None or rebuild or _index.version != ingest.sources_fingerprint():
        _index = Index()
    return _index
