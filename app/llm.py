"""Клиент локальной Ollama. Наружу запросы не уходят."""
from __future__ import annotations

import json

import httpx

from . import config

TIMEOUT = httpx.Timeout(300.0, connect=3.0)


def embed(texts: list[str]) -> list[list[float]]:
    r = httpx.post(f"{config.OLLAMA_URL}/api/embed",
                   json={"model": config.EMBED_MODEL, "input": texts}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["embeddings"]


def _payload(messages, fmt=None, max_tokens=400):
    p = {"model": config.LLM_MODEL, "messages": messages, "stream": False, "think": False,
         "options": {"temperature": 0.1, "num_predict": max_tokens, "num_ctx": 4096}}
    if fmt:
        p["format"] = fmt
    return p


def chat(messages: list[dict], max_tokens: int = 400) -> str:
    r = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=_payload(messages, max_tokens=max_tokens),
                   timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def chat_stream(messages: list[dict], max_tokens: int = 400):
    p = _payload(messages, max_tokens=max_tokens) | {"stream": True}
    with httpx.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=p, timeout=TIMEOUT) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                part = json.loads(line)
                yield part.get("message", {}).get("content", "")
                if part.get("done"):
                    break


def chat_json(messages: list[dict], schema: dict, max_tokens: int = 600) -> dict:
    r = httpx.post(f"{config.OLLAMA_URL}/api/chat",
                   json=_payload(messages, fmt=schema, max_tokens=max_tokens), timeout=TIMEOUT)
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def available() -> bool:
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        names = {m["name"] for m in r.json().get("models", [])}
        return config.LLM_MODEL in names or any(n.startswith(config.LLM_MODEL) for n in names)
    except Exception:
        return False
