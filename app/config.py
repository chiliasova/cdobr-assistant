"""Настройки приложения. Всё можно переопределить переменными окружения."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CDOBR_DATA_DIR", ROOT / "data"))
SOURCES_DIR = DATA_DIR / "sources"
MODULES_DIR = SOURCES_DIR / "modules"
INDEX_DIR = DATA_DIR / "index"
OUTPUT_DIR = DATA_DIR / "drafts"
DB_PATH = DATA_DIR / "app.db"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
# 0 — отключить LLM и отвечать только цитатами (полезно для отладки без Ollama)
USE_LLM = os.getenv("USE_LLM", "1") != "0"
USE_EMBEDDINGS = os.getenv("USE_EMBEDDINGS", "1") != "0"

# Сколько фрагментов отдаём модели и показываем пользователю
TOP_K = int(os.getenv("TOP_K", "5"))
# Ниже этого порога честно говорим «в Инструкции этого нет»
MIN_RELEVANCE = float(os.getenv("MIN_RELEVANCE", "0.35"))

# Демо-пользователи: логин -> (пароль, роль). Для прототипа достаточно.
USERS = {
    "employee": ("employee", "employee"),
    "doc": ("doc", "doc_admin"),
}
SECRET_KEY = os.getenv("CDOBR_SECRET", "change-me-on-demo")
