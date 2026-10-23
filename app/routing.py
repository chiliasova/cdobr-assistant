"""Маршрут визирования по Матрице обязательного визирования.

Правила читаются из самой Матрицы (data/sources/matrix.md): обновили Матрицу —
обновился маршрут. Условия «если …» превращаются в вопросы пользователю.
"""
from __future__ import annotations

import re

from . import search

# Ключевые слова условий -> флаг, который спрашиваем у пользователя
CONDITIONS = {
    "it_access": ("доступ", "Затрагивается доступ к информационным системам?"),
    "it_project": ("ИТ-проект", "Объединение создаётся для ИТ-проекта или вопросов информационных систем?"),
}
DEPT_RE = re.compile(r"Департамент[а-я]*\s+[а-яё\s]+?(?=\s+визирует|\s*$|\s+Департамент)", re.I)


def matrix_rows() -> list[dict]:
    ix = search.get_index()
    return [c.to_dict() for c in ix.chunks if c.source == "matrix" and c.meta.get("row")]


def _parse_other(cell: str) -> list[dict]:
    if not cell or cell.strip() in ("Нет", "—"):
        return []
    cond = None
    for key, (kw, _) in CONDITIONS.items():
        if "если" in cell and kw.lower() in cell.lower():
            cond = key
    depts = [d.strip() for d in DEPT_RE.findall(cell)] or [cell.strip()]
    reason = cell.split("если", 1)[1].strip(" .") if "если" in cell else ""
    return [{"name": d, "condition": cond, "reason": reason} for d in depts]


def route(group: str, row: int, flags: dict | None = None, modular: bool = True) -> dict:
    flags = flags or {}
    chunk = next((r for r in matrix_rows() if r["meta"]["group"] == group and r["meta"]["row"] == row), None)
    if chunk is None:
        raise KeyError(f"{group}, строка {row}")
    cells = chunk["meta"]["cells"]
    ref = chunk["ref"]
    steps = []
    if modular:
        steps.append({"name": "Ответственный исполнитель, участники подготовки и руководитель подразделения-исполнителя",
                      "ref": "Методические рекомендации, п. 4.2", "stage": "подготовка"})
        steps.append({"name": "Лингвистическая экспертиза (корректор Департамента ДОУ)",
                      "ref": "Методические рекомендации, п. 4.2", "stage": "экспертиза"})
    questions = []
    for col, val in cells.items():
        if col == "Прочие департаменты":
            for d in _parse_other(val):
                if d["condition"]:
                    questions.append({"flag": d["condition"], "text": CONDITIONS[d["condition"]][1]})
                    if not flags.get(d["condition"]):
                        continue
                steps.append({"name": d["name"], "ref": ref, "stage": "визирование",
                              "reason": d["reason"] or "обязательно по Матрице"})
        elif val.strip().startswith("Да"):
            name = "Департамент ДОУ" if col == "Департамент ДОУ" else col
            if modular and name == "Департамент ДОУ":
                continue  # добавим последним — по п. 4.2 методички
            steps.append({"name": name, "ref": ref, "stage": "визирование", "reason": "обязательно по Матрице"})
    if any(r for r in cells.values() if r.startswith("Да") and "ДОУ" in r) or cells.get("Департамент ДОУ", "").startswith("Да"):
        if modular:
            steps.append({"name": "Департамент ДОУ (проверка применения модуля, оформления и маршрута)",
                          "ref": "Методические рекомендации, п. 4.2; " + ref, "stage": "визирование"})
    notes = ["Если проект затрагивает компетенцию другого подразделения, оно включается в маршрут "
             "независимо от отметок в Матрице [Матрица визирования, п. 3]."]
    if modular:
        notes.append("Проект, строго соответствующий модулю, не направляется на повторное полное согласование; "
                     "если изменены постоянные формулировки — полное согласование по главе 10 Инструкции "
                     "[Методические рекомендации, п. 1.3].")
    return {"matrix_row": chunk["text"].split(". Юридическое")[0], "ref": ref, "steps": steps,
            "questions": questions, "notes": notes}
