#!/usr/bin/env python3
"""Регрессия: адаптивные вопросы являются полноценным интервью для аудита файла."""
import json
import os
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REALITY = ROOT / "experts" / "wz_data_reality_check.py"
SESSION = ROOT / "experts" / "wz_session.py"
WIZARD = ROOT / "ui" / "wizard.html"
SERVER = ROOT / "ui" / "server.py"


def load_expert(path, name):
    source = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                       if not line.startswith("$extens") and not line.startswith("include("))
    ns = {}
    exec(compile(source, str(path), "exec"), ns)
    return ns[name]


def main():
    reality = load_expert(REALITY, "wz_data_reality_check")
    session_fn = load_expert(SESSION, "wz_session")
    old_home = os.environ.get("HOME")
    old_requests = sys.modules.get("requests")
    captured = {}
    try:
        with tempfile.TemporaryDirectory(prefix="wz_adaptive_check_") as td:
            os.environ["HOME"] = td
            root = Path(td) / "extella_wizard" / "sessions"
            root.mkdir(parents=True)
            sid = "wz_adaptive"
            session = {
                "session_id": sid,
                "questionnaire_task": "Найти просроченные поверки и посчитать нагрузку поверителей",
                "answers": {
                    "main_operational_problem": {
                        "question": "Что именно нужно контролировать?",
                        "answer": "Просроченные поверки и остатки этикеток по организациям",
                    },
                    "desired_dashboard": {
                        "question": "Какой результат нужен?",
                        "answer": "Количество просрочек, нагрузка поверителей и остатки по филиалам",
                    },
                    "registry_origin": {
                        "question": "Где находятся данные?",
                        "answer": "Ежемесячная выгрузка Excel из реестра средств измерений",
                    },
                },
                "data_check": {"verdict": "no", "context_version": 1},
            }
            (root / (sid + ".json")).write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
            fdir = root / (sid + "_files")
            fdir.mkdir()
            import openpyxl
            wb = openpyxl.Workbook(); ws = wb.active
            ws.append(["Действителен до", "Поверитель", "Организация", "Остаток этикеток"])
            ws.append(["2026-01-31", "А. К.", "Филиал 1", 12])
            wb.save(fdir / "registry.xlsx")

            result_json = json.dumps({
                "verdict": "yes",
                "present": ["Действителен до", "Поверитель", "Организация", "Остаток этикеток"],
                "missing": [
                    {"need": "Описание процесса клиента", "why": "старое обязательное поле"},
                    {"need": "Код филиала", "why": "нужен для заявленной группировки"},
                ],
                "computable_metrics": [{"metric": "Просроченные поверки", "why": "есть срок"}],
                "blocked_metrics": [{"metric": "Стоимость работ", "why": "стоимости в задаче и файле нет"}],
                "client_message": "Файл содержит данные для заявленного контроля.",
            }, ensure_ascii=False)

            class Response:
                def json(self):
                    return {"status": "completed", "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": result_json}]}]}

            def post(_url, **kwargs):
                captured["prompt"] = (kwargs.get("json") or {}).get("input", "")
                return Response()

            sys.modules["requests"] = types.SimpleNamespace(post=post)
            out = reality(session_id=sid, api_token="token", agent_id="agent_qwen")
            dc = out["data_check"]
            prompt = captured["prompt"]
            assert "Найти просроченные поверки" in prompt
            assert "main_operational_problem" in prompt and "desired_dashboard" in prompt
            assert dc["verdict"] == "yes" and dc["context_version"] == 2
            assert dc["missing"] == [{"need": "Код филиала", "why": "нужен для заявленной группировки",
                                      "in_file": False}]
            assert dc["computable_metrics"] == ["Просроченные поверки — есть срок"]
            assert dc["blocked_metrics"] == ["Стоимость работ — стоимости в задаче и файле нет"]
            assert "[object Object]" not in json.dumps(dc, ensure_ascii=False)

            # Любое изменение интервью делает прежний аудит неактуальным.
            saved = session_fn(action="save_answers", session_id=sid, base_dir=str(root),
                               payload_json=json.dumps({"new_domain_answer": {
                                   "question": "Как учитывать исключения?", "answer": "Отдельной строкой"}},
                                   ensure_ascii=False))
            assert saved["status"] == "success"
            fresh = json.loads((root / (sid + ".json")).read_text(encoding="utf-8"))
            assert "data_check" not in fresh

        html = WIZARD.read_text(encoding="utf-8")
        server = SERVER.read_text(encoding="utf-8")
        assert "function _currentDataCheck()" in html and "context_version||0" in html
        assert "_dcText(x)" in html and "!_currentDataCheck()" in html
        assert 's.pop("data_check", None)' in server and 'x.pop("data_check", None)' in server
        print("адаптивное интервью → аудит читает все ответы; старый вердикт сбрасывается ✓")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = old_requests


if __name__ == "__main__":
    main()
