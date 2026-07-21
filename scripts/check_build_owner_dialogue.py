#!/usr/bin/env python3
"""Регрессия шва need_human → inline answer → Task Contract → новая стройка."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
AGENTIC = (ROOT / "ui" / "wz_agentic.py").read_text(encoding="utf-8")
BUILD = (ROOT / "ui" / "wz_build.py").read_text(encoding="utf-8")
SERVER = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
WIZARD = (ROOT / "ui" / "wizard.html").read_text(encoding="utf-8")


def main():
    assert 'prog["status"] = "waiting_for_owner"' in BUILD
    assert 's["waiting_build"]' in BUILD and '_wait_owner(question, "source_model"' in BUILD
    assert 'self.path == "/x/build_answer"' in SERVER
    assert "apply_owner_clarification(s, question, answer, previous_build_id)" in SERVER
    assert '"source": "builder_checkpoint"' in AGENTIC
    assert 'jpost("/x/build_answer"' in WIZARD
    assert "Сохранить ответ и продолжить" in WIZARD
    assert 'S.buildDone=true; S.buildId=null; S.waitingBuild=null' in WIZARD
    assert 'el("buildBtn").textContent="↻ Повторить сборку"' in WIZARD
    print("стройка: technical identity автоматична; business ambiguity ждёт inline-ответ и продолжает сессию ✓")


if __name__ == "__main__":
    main()
