#!/usr/bin/env python3
"""Регрессия: длинный/медленный мозг не захватывает и не блокирует Wizard."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "ui" / "wizard.html").read_text(encoding="utf-8")


def main():
    assert "max-height:calc(100vh - 40px)" in HTML
    assert ".mod-content{min-height:0;overflow-y:auto" in HTML
    assert "max-height:32vh;min-height:0;overflow-y:auto;overflow-wrap:anywhere" in HTML
    assert 'class="mod-close"' in HTML and "if(event.target===this)modCancel()" in HTML
    assert "if(el('modBack').classList.contains('open')) modCancel()" in HTML
    assert "let _brainOpenSeq=0" in HTML
    assert "seq!==_brainOpenSeq || !el(\"modBack\").classList.contains(\"open\")" in HTML
    assert "запоздавший сетевой ответ не имеет права" in HTML
    assert "text.length>320" in HTML and "развернуть" in HTML
    assert "Мозг агента временно недоступен" in HTML and "Повторить загрузку" in HTML
    print("мозг агента: bounded modal, scroll, close/escape и stale-response guard ✓")


if __name__ == "__main__":
    main()
