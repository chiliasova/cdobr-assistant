"""Заглушка Ollama для тестов: отвечает пересказом первого фрагмента со ссылкой."""
import json
import re

import httpx


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen3:4b-instruct-2507-q4_K_M"}]})
    body = json.loads(request.content)
    if request.url.path == "/api/embed":
        raise httpx.ConnectError("нет эмбеддингов в тестах")
    if request.url.path == "/api/chat":
        user = body["messages"][-1]["content"]
        if body.get("format"):
            ids = list(body["format"]["properties"])
            return httpx.Response(200, json={"message": {"content": json.dumps({ids[0]: "внедрению CRM-системы"})}})
        m = re.search(r"\[([^\]]+)\] \([^)]*\)\n(.{0,120})", user, re.S)
        text = f"{m.group(2).strip()} [{m.group(1)}]" if m else "НЕТ ОТВЕТА"
        if body.get("stream"):
            lines = [json.dumps({"message": {"content": w + " "}, "done": False}) for w in text.split(" ")]
            lines.append(json.dumps({"message": {"content": ""}, "done": True}))
            return httpx.Response(200, content="\n".join(lines).encode())
        return httpx.Response(200, json={"message": {"content": text}})
    return httpx.Response(404)
