"""Разбор источников базы знаний на фрагменты с точными ссылками.

Инструкция режется по пунктам («10.15.»), Матрица — по строкам таблицы,
Методические рекомендации — по пунктам, модули — по одному фрагменту
с названием и примечаниями по заполнению.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import docx

from . import config

MAX_CHUNK = 900  # символов; длинные пункты делим, ссылка остаётся та же


@dataclass
class Chunk:
    id: str            # уникальный id фрагмента
    source: str        # instruction | matrix | methodology | module
    ref: str           # как ссылаться в ответе: «п. 10.15», «Матрица, строка 6»
    title: str         # глава / раздел, для показа пользователю
    text: str
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


POINT_RE = re.compile(r"^(\d+(?:\.\d+)+)\.\s*(.*)$")
CHAPTER_RE = re.compile(r"^Глава\s+(\d+)\.\s*(.*)$")


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHUNK:
        return [text]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.;:])\s+", text):
        if cur and len(cur) + len(sent) > MAX_CHUNK:
            parts.append(cur.strip())
            cur = ""
        cur += sent + " "
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_instruction(path: Path) -> list[Chunk]:
    lines = [l.rstrip() for l in path.read_text(encoding="utf-8").splitlines()]
    chunks: list[Chunk] = []
    chapter_no, chapter_title = 0, ""
    cur_id, cur_lines = None, []
    seen: set[str] = set()
    title_lines = 0          # строк заголовка главы после «Глава N.»
    preamble: list[str] = []  # текст между заголовком и первым пунктом (PDF местами перемешан)
    appendix: list[str] | None = None

    def flush():
        nonlocal cur_id, cur_lines
        if cur_id:
            text = re.sub(r"\s+", " ", " ".join(cur_lines)).strip()
            for i, part in enumerate(_split_long(text)):
                chunks.append(Chunk(
                    id=f"instr:{cur_id}" + (f"#{i}" if i else ""),
                    source="instruction", ref=f"п. {cur_id}",
                    title=f"Глава {chapter_no}. {chapter_title}",
                    text=part, meta={"point": cur_id, "chapter": chapter_no},
                ))
        cur_id, cur_lines = None, []

    for line in lines:
        if not line.strip():
            continue
        if appendix is not None:
            appendix.append(line)
            continue
        if line.startswith("Приложение №"):
            flush()
            appendix = [line]
            continue
        m = CHAPTER_RE.match(line)
        if m:
            flush()
            chapter_no, chapter_title = int(m.group(1)), m.group(2).strip()
            title_lines, preamble = 0, []
            continue
        m = POINT_RE.match(line)
        if m:
            num = m.group(1)
            # «по пункту 2.13.» в начале строки внутри пункта другой главы — не новый пункт.
            # Порядок не проверяем: в PDF встречается 4.1.1 перед 4.1.
            if chapter_no and int(num.split(".")[0]) == chapter_no and num not in seen:
                flush()
                seen.add(num)
                cur_id, cur_lines = num, preamble + [m.group(2)]
                preamble = []
                continue
        if chapter_no and cur_id is None:
            # одна строка — продолжение заголовка главы, остальное — к первому пункту
            if title_lines == 0 and not line.startswith("•"):
                chapter_title += " " + line.strip()
                title_lines = 1
            else:
                preamble.append(line)
            continue
        if cur_id:
            cur_lines.append(line)
    flush()
    if appendix:
        text = re.sub(r"\s+", " ", " ".join(appendix[3:]))
        chunks.append(Chunk(
            id="instr:app1", source="instruction", ref="Приложение № 1",
            title="Приложение № 1. Таблица замечаний", text=text,
            meta={"point": "Приложение 1"},
        ))
    return chunks


def _md_plain(s: str) -> str:
    s = re.sub(r"\\([\[\]_.\-*])", r"\1", s)
    s = s.replace("**", "").replace("> ", "")
    return re.sub(r"\s+", " ", s).strip()


def parse_matrix(path: Path) -> list[Chunk]:
    """Строки грид-таблицы pandoc: ячейки разделены «|», строки — «+---»."""
    text = path.read_text(encoding="utf-8")
    rows, cur = [], None
    for line in text.splitlines():
        if line.startswith("+"):
            if cur and any(c.strip() for c in cur):
                rows.append([_md_plain(c) for c in cur])
            cur = None
        elif line.startswith("|"):
            cells = line.strip().strip("|").split("|")
            if cur is None:
                cur = [""] * len(cells)
            for i, c in enumerate(cells[: len(cur)]):
                cur[i] += " " + c
    header = rows[0][:5]
    chunks, group = [], ""
    for row in rows[1:]:
        name = row[0]
        if name.isupper():
            group = "Внутренние документы" if "ВНУТР" in name else "Внешние документы"
            continue
        m = re.match(r"(\d+)\.\s*", name)
        n = m.group(1) if m else "?"
        parts = [f"{header[i]}: {row[i] or '—'}" for i in range(1, 5)]
        chunks.append(Chunk(
            id=f"matrix:{group[:4]}:{n}", source="matrix",
            ref=f"Матрица визирования, {group.lower()}, строка {n}",
            title="Матрица обязательного визирования",
            text=f"{name}. " + "; ".join(parts),
            meta={"group": group, "row": int(n) if n.isdigit() else 0,
                  "cells": dict(zip(header[1:], row[1:5]))},
        ))
    # Общие правила применения Матрицы (нумерованные абзацы над таблицей)
    for m in re.finditer(r"^(\d)\\?\.\s+(.+)$", text.split("+")[0], re.M):
        chunks.append(Chunk(
            id=f"matrix:rule:{m.group(1)}", source="matrix",
            ref=f"Матрица визирования, п. {m.group(1)}",
            title="Матрица обязательного визирования", text=_md_plain(m.group(2)),
        ))
    return chunks


def parse_methodology(path: Path) -> list[Chunk]:
    chunks, section, cur_id, buf = [], "", None, []

    def flush():
        if cur_id and buf:
            chunks.append(Chunk(
                id=f"method:{cur_id}", source="methodology",
                ref=f"Методические рекомендации, п. {cur_id}", title=section,
                text=_md_plain(" ".join(buf)),
            ))

    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^\*\*(\d+)\.\s+(.+?)\*\*$", s)
        if m and not re.match(r"^\*\*\d+\.\*\*", s):
            flush(); cur_id, buf = None, []
            section = f"{m.group(1)}. {m.group(2)}"
            continue
        m = re.match(r"^(\d+\.\d+)\.\s+(.*)$", s)
        if m:
            flush()
            cur_id, buf = m.group(1), [m.group(2)]
        elif cur_id:
            buf.append(s)
    flush()
    return chunks


def module_title(path: Path) -> str:
    return path.stem.replace("Модуль_", "Модуль ").replace("_", " ")


def parse_module(path: Path) -> Chunk:
    d = docx.Document(str(path))
    paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    heading = next((p for p in paras if p.startswith("О ") or p.startswith("Об ")), "")
    try:
        notes = paras[paras.index(next(p for p in paras if p.startswith("Примечания по заполнению"))):]
    except StopIteration:
        notes = []
    kind = "Приказ" if "приказа" in path.stem else "Распоряжение"
    text = f"{module_title(path)}. {kind} «{heading}». " + " ".join(notes)
    return Chunk(
        id=f"module:{path.stem}", source="module", ref=module_title(path),
        title="Модульные документы (шаблоны)", text=re.sub(r"\s+", " ", text),
        meta={"file": path.name, "kind": kind},
    )


def load_all() -> list[Chunk]:
    s = config.SOURCES_DIR
    chunks = parse_instruction(s / "instruction.txt")
    chunks += parse_matrix(s / "matrix.md")
    chunks += parse_methodology(s / "methodology.md")
    chunks += [parse_module(p) for p in sorted(config.MODULES_DIR.glob("*.docx"))]
    return chunks


def sources_fingerprint() -> str:
    """Хеш всех источников: меняется файл — индекс пересобирается (версионность)."""
    h = hashlib.sha256()
    for p in sorted(config.SOURCES_DIR.rglob("*")):
        if p.is_file():
            h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()[:16]
