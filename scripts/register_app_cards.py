#!/usr/bin/env python3
"""Регистрация карточек приложений визарда на Plugins-главной (Рабочем столе).

Решение Анвара 17.07: «Рабочий стол = главная Plugins». Приложения визарда
(Композитор, Студия языков, Команда) — карточки в реестре плагинов тулбара
(~/extella-plugins/_registry/), каждая открывает своё окно через deep-link
wizard.html?app=<...>. Карточка Workspace и сам Визард регистрируются отдельно.

Реестр — локальная среда устройства (как сессии/KV), в git не версионируется;
этот скрипт — воспроизводимый источник карточек для любой машины.
Запуск: python3 scripts/register_app_cards.py
"""
import json
import os
from pathlib import Path

REG = Path.home() / "extella-plugins" / "_registry"

APPS = [
    ("extella_composer_studio", "Композитор",
     "Задачи из готовых блоков — опишите словами, Композитор соберёт и запустит",
     "Студия Композитора: задача словами → подбор блоков из вашей библиотеки → доустановка "
     "недостающего → запуск с результатом в приложении. Доводка словами пересобирает ту же композицию.",
     "wizard.html?app=composer"),
    ("extella_cspl_studio", "Студия языков",
     "Свои предметные языки: программа словами дела → проверенный исполняемый результат",
     "CSPL Studio: языки аккаунта (отчёты, конвейеры) с контрольными прогонами, проба компиляции и "
     "создание нового языка словами (спецификация поверх проверенного ядра, fixtures-гейт).",
     "wizard.html?app=cspl"),
    ("extella_team", "Команда",
     "Пригласить в общий контур — именные ключи доступа, отзыв в один клик",
     "Команда Extella: приглашение = именной ключ этого контура (показывается один раз, передать "
     "лично); общие процессы, витрина и кабинеты. Роли и права — на подходе.",
     "wizard.html?app=team"),
]


def main():
    REG.mkdir(parents=True, exist_ok=True)
    for pid, name, tagline, desc, mainfile in APPS:
        manifest = {
            "id": pid, "name": name, "tagline": tagline, "description": desc,
            "category": "ai-work", "type": "custom", "version": "1.0.0",
            "source": "local://extella_wizard", "mode": "repo_ui", "system": True,
            "ui": {"type": "local_server", "port": 8765, "rootPath": "~/extella_wizard/app",
                   "startExpert": "_etb_srv_extella_adoption_wizard", "mainFile": mainfile,
                   "openInBrowser": False, "expectsHealth": False},
            "service": {"isApp": True, "port": 8765, "startExpert": "_etb_srv_extella_adoption_wizard",
                        "healthPath": "/x/sessions", "launchCmd": "python3 ~/extella_wizard/app/server.py",
                        "ready": True},
        }
        (REG / (pid + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("зарегистрирована карточка:", pid, "->", mainfile)
    print("готово. Обновите список плагинов (↺ на вкладке Plugins) или перезапустите Extella.")


if __name__ == "__main__":
    main()
