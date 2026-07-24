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


# Хостинговые карточки: сервер на VPS команды, открывается в панели по ui.url.
HOSTED = [
    ("extella_predictive_hosted", "Predictive Sales (команда)",
     "Воронка Bitrix24 с AI-прогнозами — общий кокпит команды",
     "Командный кокпит Predictive Sales на хостинге Extella: воронка сделок, "
     "рабочие шансы, риски и следующие действия. Подключение Bitrix24 "
     "настраивает владелец; записи в CRM — только после подтверждения.",
     "https://predictive.82-115-42-21.sslip.io"),
    ("extella_targetolog_hosted", "Таргетолог AI (команда)",
     "Рекламные брифы, медиапланы и кампании — общий контур команды",
     "Командный Таргетолог на хостинге Extella: брифы, медиапланы, черновики "
     "кампаний и отчёты в одной базе. Подключения рекламных кабинетов "
     "настраиваются владельцем; внешние отправки — только после approval.",
     "https://targetolog.82-115-42-21.sslip.io"),
    ("extella_kz_grocery", "Бага — цены на продукты Казахстана",
     "Цены продуктов — общий сервер команды (хостинг Extella)",
     "Матрица 100 × 6 сетей, честное сравнение и история цен. Общая база на "
     "хостинге Extella: все смотрят одни и те же данные, ставить ничего не нужно.",
     "https://baga.82-115-42-21.sslip.io"),
]


# Инфо-карточки: создать, если нет (у владельца может жить полноценная
# локальная версия под тем же id — её не перезаписываем).
INFO = [
    ("extella_1c_agent", "Агент 1С", "Безопасная работа с 1С — только чтение",
     "Читает живую 1С 8.3 через выделенного Qwen-агента (остатки, регистры, "
     "документы). Ставится на Windows-машину с 1С — инструкция и архив "
     "открываются прямо из карточки, GitHub-доступ не нужен.",
     "https://github.com/AnvarBakiyev/extella-1c-agent",
     """# Агент 1С — установка (Windows)

## Что нужно
- Windows с лицензионной 1С 8.3 (право внешнего соединения, `V83.COMConnector`)
- Python 3.11+ и pywin32
- Extella Desktop 1.2.0+ с Listener

## Установка
- Скачайте архив: https://files.82-115-42-21.sslip.io/extella-1c-agent-0.2.0-beta.1.zip и распакуйте
- Запустите `INSTALL_AGENT_1C.cmd` — мастер выберет **Qwen**-агента, запишет подключение 1С (base, пользователь, пароль) в зашифрованный Extella KV — пароль не попадает в код, чат и логи — и зарегистрирует карточку плагина
- Проверка: спросите агента об остатках или регистрах. Первый запрос — `op=registers`: в «Бухгалтерии для Казахстана» регистр называется «Типовой», не «Хозрасчетный»

## Важно
- Только чтение: запись, проведение и удаление выключены
- Работает только выделенный **Qwen**-агент (Claude запрещён и заблокирован)
- Вопросы и доступы — к Анвару"""),
]


def register_info(reg):
    import json
    for pid, name, tagline, desc, source, guide in INFO:
        path = reg / (pid + ".json")
        if path.exists():
            # Обновляем только собственные info-указатели; полноценную
            # локальную карточку владельца под тем же id не трогаем.
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            if existing.get("mode") != "info":
                continue
        manifest = {
            "id": pid, "name": name, "tagline": tagline, "description": desc,
            "category": "analytics", "type": "custom", "version": "1.1.0",
            "source": source, "mode": "info", "guide": guide,
        }
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("зарегистрирована инфо-карточка:", pid)


def register_hosted(reg):
    import json
    for pid, name, tagline, desc, url in HOSTED:
        manifest = {
            "id": pid, "name": name, "tagline": tagline, "description": desc,
            "category": "analytics", "type": "custom", "version": "1.0.0",
            "source": "hosted://extella-vps", "mode": "repo_ui",
            "ui": {"type": "local_server", "url": url, "openInBrowser": False,
                   "mainFile": "index.html"},
            "service": {"isApp": True, "hosted": True, "ready": True},
        }
        (reg / (pid + ".json")).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("зарегистрирована хостинговая карточка:", pid, "->", url)


def main():
    REG.mkdir(parents=True, exist_ok=True)
    register_hosted(REG)
    register_info(REG)
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
