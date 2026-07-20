#!/usr/bin/env python3
"""Регрессия стройки: runtime-обвязка не должна ломать обязательный конвейер данных."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "ui" / "wz_build.py"


def load_classifier():
    tree = ast.parse(BUILD.read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_is_pipeline_data_task"]
    if len(body) != 1:
        raise AssertionError("не найден классификатор стадий в ui/wz_build.py")
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(BUILD), "exec"), ns)
    return ns["_is_pipeline_data_task"]


def main():
    is_data = load_classifier()
    runtime = [
        {"expert_name": "pc40_folder_monitor",
         "purpose": "Фоновый демон мониторинга рабочей папки: отслеживает появление новых файлов"},
        {"expert_name": "pc40_setup_monitor_autostart", "purpose": "Настраивает launchd autostart"},
        {"expert_name": "pc40_send_email_notification", "purpose": "Отправляет готовый отчёт"},
    ]
    pipeline = [
        {"expert_name": "pc40_parse_excel", "purpose": "Читает Excel и извлекает записи"},
        {"expert_name": "pc40_compare_labels", "purpose": "Сверяет номера лейблов"},
        {"expert_name": "pc40_budget_monitor", "purpose": "Вычисляет отклонения бюджета в записях"},
        {"expert_name": "pc40_fetch_email_attachment", "purpose": "Получает входной файл из письма"},
    ]
    assert not any(is_data(t) for t in runtime)
    assert all(is_data(t) for t in pipeline)
    print("стройка: data-стадии обязательны, watcher/autostart/доставка не блокируют DAG ✓")


if __name__ == "__main__":
    main()
