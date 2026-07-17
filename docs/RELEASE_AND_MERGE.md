# Релиз и мёрдж: единый процесс доставки Extella коллегам

Один источник правды, как **вести** все ветки и **доставить пользователю всё сразу** —
тулбар + эксперты + концепты + правила + локалхосты — так, чтобы «открылось, заработало и было у него».

Доставка = **одна команда** (`install-all.sh`). Всё остальное в этом доке — как держать источники
свежими, чтобы эта команда отдавала актуальное.

---

## 1. Три репо и их каноны (единственный источник на компонент)

| Компонент | Репо | Канон-ветка | Что содержит | Как доставляется |
|---|---|---|---|---|
| **Тулбар** (витрина, рабочий стол, системные ярлыки) | `AnvarBakiyev/extella-toolbar-src` (private) | **`ws-ui`** | исходники → `node build.js` → `toolbar.js` (~4.5 МБ) | собранный `toolbar.js` копируется в `extella-marketplace-pack/toolbar/toolbar.js` |
| **Дистрибутив** (то, что реально качает коллега) | `AnvarBakiyev/extella-marketplace-pack` (public) | **`main`** | `toolbar/toolbar.js`, `toolbar/install-all.sh`, 168 экспертов, 30 экспертов паков, каталоги, автоматизации | тарбол `.../archive/refs/heads/main.tar.gz` + RAW для toolbar.js |
| **Визард + мост** (кабина, оркестратор, wz_-эксперты, CSPL, мультитаргет, команда) | `AnvarBakiyev/extella-adoption-wizard` (public) | **`main`** | `ui/*.py` (мост), `wizard.html`, 45 wz_/cspl_ экспертов, install.py | тарбол `.../archive/refs/heads/main.tar.gz` |

**Правило одного источника:** у каждого компонента ровно один канон. Тулбар живёт в `ws-ui` и попадает
к людям только собранным файлом в marketplace-pack. Эксперты/мост живут в своих репо и ставятся
их `install.py`. Никто не редактирует `toolbar.js` руками — только пересборка из `ws-ui`.

---

## 2. Единственная команда для коллеги

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/AnvarBakiyev/extella-marketplace-pack/main/toolbar/install-all.sh)
```

Что делает по шагам:
1. **Тулбар** — качает `toolbar.js` из marketplace-pack (RAW), проверяет, ставит (бэкапит старый).
2. **Токен** — берёт из `EXTELLA_TOKEN` или спрашивает; пишет `~/extella_wizard/app/config.json`
   (`agent_id = agent_extella_alibaba_default` — Qwen, платного Claude коллегам НЕ ставим).
3. **Python + SSL** — ставит `certifi`, симлинкует CA-сертификаты. **← корневой фикс Мергуль:**
   python.org-Python без CA не мог достучаться до `api.extella.ai` → `/x/health` не открывался,
   «Set up & open» ругался, апдейтер падал. Этот шаг чинит всё разом.
4. **Эксперты + Визард** — тарбол marketplace-pack → `install.py` (168+30 экспертов, концепты,
   правила, `composer:catalog`, каталоги, автоматизации); Activity Center (мост :8799); тарбол
   визарда → `ui/*.py` + `wizard.html` в `~/extella_wizard/app/` → `install.py` (45 wz_-экспертов,
   правила, концепты, реестр плагина).
5. **Запуск** — убивает старый мост, поднимает :8765, проверяет здоровье, переоткрывает Extella.

**Токен (правило безопасности).** Токен вставляет тот, кто ставит, на СВОЕЙ машине: в Extella пишет
«сгенерируй мне новый токен», копирует, вставляет в терминал. Я (ассистент) токены в поля/промпты
не ввожу и в чат не печатаю — это правило не обходится даже по прямой просьбе.

Идемпотентна: пользовательские данные (сессии, `config.json`, `vault.key`, логи) не трогает.

---

## 3. Чеклист релиза (перед тем как дать коллеге команду выше)

Порядок обязателен — иначе коллега получит рассинхрон (свежий тулбар + старый мост и т.п.):

1. **Тулбар:** правки в `ws-ui` → `node build.js` → `toolbar.js`.
2. **Синк дистрибутива:** копировать свежий `toolbar.js` → `extella-marketplace-pack/toolbar/toolbar.js`,
   `git commit && git push` (main). Проверить RAW отдаёт новый:
   `curl -s .../main/toolbar/toolbar.js | grep -o 'Built: [^"]*' | head -1`.
3. **Визард/мост:** правки в `extella-adoption-wizard` → `git push` (main). Поднять `BRIDGE_VERSION`
   в `ui/server.py`, если менялся мост.
4. **Аудит (см. §5):** прогнать дифф «зовёт vs в паках» — должно быть пусто.
5. Дать коллеге команду из §2.

---

## 4. Правила мёржа по компонентам

**Тулбар — домен чата тулбара; канон `ws-ui`, не `main`.**
- Не мёржить и не деплоить тулбар, не сверившись; `ws-ui` > `main`.
- Как безопасно править занятую `ws-ui`: свежий `git clone -b ws-ui` во временную папку (не `worktree add` —
  ветка занята рабочим деревом), точечная правка, `node build.js`, FF-push, синк в marketplace-pack.
- **Хвост для разбора:** моя ветка `main` тулбара (коммит d1ef2d8) — параллельная линия, НЕ канон.
  Чтобы был один источник — привести `main` к `ws-ui` (`git reset --hard ws-ui` силами чата тулбара)
  или забыть `main`. Сам их ветки не ресетлю.

**Эксперты / мост (визард, marketplace-pack) — обычный git-flow в `main`.**
- Новый эксперт = файл в `experts/*.py`, `install.py` подхватит автоматически (glob).
- Меняешь мост — подними `BRIDGE_VERSION`, чтобы `/x/health` и single-instance отличали свежий от старого.

---

## 5. Аудит «работает у всех» (повторяемый)

Проверяет, что ни мост, ни карточки не зовут эксперта, которого нет в дистрибутиве.
Прогонять перед каждым релизом (последний прогон 17.07 — чисто):

```bash
python3 - <<'PY'
import os, re, glob
W=os.path.expanduser("~/Documents/xtela/extella-adoption-wizard")
D=os.path.expanduser("~/extella_tools/extella-marketplace-pack")
union=set()
for base,sub in [(W,"experts"),(D,"experts"),(D,"automations/experts")]:
    for f in glob.glob(os.path.join(base,sub,"*.py")): union.add(os.path.basename(f)[:-3])
refs=set()
for pf in glob.glob(os.path.join(W,"ui","*.py")):
    s=open(pf,encoding="utf-8").read()
    refs|=set(re.findall(r'"expert_name":\s*"([a-z0-9_]+)"',s))
    refs|=set(re.findall(r'run_expert\(\s*"([a-z0-9_]+)"',s))
conn=["email","slack","sms","telegram","whatsapp"]; src=["gsheets","bitrix24","amocrm","mysql","postgres","1c_file","1c_winrm"]
need=(refs|{ "wz_connector_"+c for c in conn }|{ "wz_source_"+x for x in src }) - {"wz_connector_","wz_source_"}
miss=sorted(n for n in need if n not in union and not n.startswith(("build_","demo_","task_")))
print("ЗОВЁТ, НО НЕ В ПАКАХ:", miss or "— чисто ✓")
PY
```

Плюс проверить: оба репо `git status` чистые и `git rev-list --left-right --count origin/main...HEAD` = `0 0`.

---

## 6. Устаревшее — НЕ использовать

`extella-update.sh` (в корне визард-репо) — ранняя неполная версия апдейтера **без шага SSL-сертификатов**,
из-за чего у Мергуль и падало. Заменён на `install-all.sh`. Коллегам давать только команду из §2.
