# expert: kp_install_pack
# description: kp_install_pack
# params:

def kp_install_pack(pack_id="") -> str:
    import os, re, ssl, html, json, time, subprocess, urllib.request, urllib.parse
    PACKS = {
        # --- Право и налоги (adilet.zan.kz) ---
        "nalog_rk":  {"name": "Налоговый кодекс РК", "adilet": ["K1700000120"]},
        "trud_rk":   {"name": "Трудовой кодекс РК",  "adilet": ["K1500000414"]},
        "grazhd_rk": {"name": "Гражданский кодекс РК","adilet": ["K940001000_", "K990000409_"]},
        "admin_rk":  {"name": "Кодекс об админправонарушениях РК","adilet": ["K1400000235"]},
        "pred_rk":   {"name": "Предпринимательский кодекс РК","adilet": ["K1500000375"]},
        "ugol_rk":   {"name": "Уголовный кодекс РК", "adilet": ["K1400000226"]},
        # --- Бизнес и управление (Википедия) ---
        "pm":         {"name": "Управление проектами", "wiki": ["Управление проектами","Scrum","Agile","Канбан (разработка)","Диаграмма Ганта"]},
        "management": {"name": "Менеджмент и лидерство","wiki": ["Менеджмент","Лидерство","Мотивация","SWOT-анализ","Ключевые показатели эффективности","Делегирование полномочий"]},
        "sales":      {"name": "Продажи и маркетинг",   "wiki": ["Продажи","Маркетинг","Воронка продаж","CRM-система","Реклама","Бренд"]},
        "strategy":   {"name": "Стратегия и бизнес-модели","wiki": ["Стратегическое управление","Бизнес-модель","Конкурентное преимущество","Бизнес-план"]},
        "hr":         {"name": "Персонал и найм","wiki": ["Управление персоналом","Подбор персонала","Корпоративная культура"]},
        # --- Финансы (Википедия) ---
        "fin_acc":    {"name": "Финансы и учёт","wiki": ["Бухгалтерский учёт","Финансовый анализ","Бюджетирование","Финансовая отчётность"]},
        "invest":     {"name": "Инвестиции и рынки","wiki": ["Инвестиции","Фондовый рынок","Ценная бумага","Облигация"]},
        "personal_fin":{"name": "Личные финансы","wiki": ["Личные финансы","Кредит"]},
        # --- Технологии (Википедия) ---
        "programming":{"name": "Программирование","wiki": ["Программирование","Язык программирования","Алгоритм","Объектно-ориентированное программирование"]},
        "ai_ml":      {"name": "ИИ и машинное обучение","wiki": ["Искусственный интеллект","Машинное обучение","Искусственная нейронная сеть","Глубокое обучение","Большая языковая модель"]},
        "security":   {"name": "Кибербезопасность","wiki": ["Информационная безопасность","Шифрование","Компьютерный вирус","Фишинг"]},
        "databases":  {"name": "Базы данных","wiki": ["База данных","SQL","Реляционная база данных"]},
        # --- Наука и здоровье (Википедия) ---
        "health":     {"name": "Здоровый образ жизни","wiki": ["Здоровый образ жизни","Здоровое питание"]},
        "first_aid":  {"name": "Первая помощь","wiki": ["Первая помощь","Сердечно-лёгочная реанимация"]},
        "space":      {"name": "Космос и астрономия","wiki": ["Астрономия","Солнечная система","Чёрная дыра"]},
        "eco":        {"name": "Экология и климат","wiki": ["Изменение климата","Возобновляемая энергетика"]},
    }
    p = PACKS.get(pack_id)
    if not p: return json.dumps({"status":"error","message":"неизвестный пак: "+str(pack_id)}, ensure_ascii=False)
    def serve():
        try: urllib.request.urlopen("http://localhost:11434/api/version", timeout=3); return True
        except Exception: pass
        for ob in ["/usr/local/bin/ollama","/opt/homebrew/bin/ollama"]:
            if os.path.exists(ob): subprocess.Popen([ob,"serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); break
        time.sleep(3)
        try: urllib.request.urlopen("http://localhost:11434/api/version", timeout=5); return True
        except Exception: return False
    if not serve(): return json.dumps({"status":"error","message":"движок знаний не запущен (Ollama)"}, ensure_ascii=False)
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    def fetch_adilet(docid):
        req=urllib.request.Request("https://adilet.zan.kz/rus/docs/"+docid, headers={"User-Agent":"Mozilla/5.0"})
        h=urllib.request.urlopen(req, timeout=90, context=ctx).read().decode("utf-8","ignore")
        t=re.sub(r"(?is)<(script|style|nav|header|footer).*?</\1>"," ",h)
        t=re.sub(r"(?s)<[^>]+>"," ",t); t=html.unescape(t); t=re.sub(r"[ \t]+"," ",t)
        m=re.search(r"Статья\s+\d+[.\s]", t)
        if m: t=t[m.start():]
        return t.strip()
    def fetch_wiki(title):
        UA="ExtellaKnowledge/1.0 (https://extella.ai; contact@extella.ai)"
        u="https://ru.wikipedia.org/w/api.php?"+urllib.parse.urlencode({"action":"query","prop":"extracts","explaintext":1,"redirects":1,"format":"json","titles":title})
        for attempt in range(4):
            try:
                req=urllib.request.Request(u, headers={"User-Agent":UA})
                d=json.loads(urllib.request.urlopen(req, timeout=60, context=ctx).read())
                for pid, pg in d.get("query",{}).get("pages",{}).items():
                    return pg.get("extract","") or ""
                return ""
            except Exception:
                time.sleep(2*(attempt+1))
        return ""
    def chunks(txt, size=1200, ov=150):
        txt=re.sub(r"\s+"," ",txt).strip(); out=[]
        parts=re.split(r"(?=Статья\s+\d+[.\s])", txt)
        if len(parts) < 3: parts=[txt]
        for c in parts:
            c=c.strip()
            if not c: continue
            if len(c) <= size: out.append(c)
            else:
                i=0
                while i < len(c): out.append(c[i:i+size]); i+=size-ov
        return out
    def embed_batch(texts):
        req=urllib.request.Request("http://localhost:11434/api/embed", data=json.dumps({"model":"nomic-embed-text","input":["search_document: "+t for t in texts]}).encode(), headers={"Content-Type":"application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=180).read()).get("embeddings", [])
    items=[]
    try:
        if "adilet" in p:
            for docid in p["adilet"]:
                for ch in chunks(fetch_adilet(docid)):
                    if ch.strip(): items.append((ch, p["name"]))
        elif "wiki" in p:
            for title in p["wiki"]:
                txt=fetch_wiki(title)
                if txt and len(txt) > 200:
                    for ch in chunks(txt):
                        if ch.strip(): items.append((ch, title))
                time.sleep(0.5)
    except Exception as e:
        return json.dumps({"status":"error","message":"не скачался источник: "+str(e)[:90]}, ensure_ascii=False)
    if not items: return json.dumps({"status":"error","message":"источник пустой"}, ensure_ascii=False)
    store=[]; last_err=""
    for i in range(0, len(items), 64):
        part=items[i:i+64]
        try:
            embs=embed_batch([t for t, s in part])
            for j in range(len(part)):
                if j < len(embs) and embs[j]: store.append({"text":part[j][0],"src":part[j][1],"emb":embs[j]})
        except Exception as e: last_err=str(e)[:110]
    if not store: return json.dumps({"status":"error","message":"векторизация не сработала: "+(last_err or "нет ответа")}, ensure_ascii=False)
    d=os.path.expanduser("~/.extella_kp"); os.makedirs(d, exist_ok=True)
    safe=re.sub(r"[^a-zA-Z0-9_]","_",p["name"])
    json.dump({"name":p["name"],"count":len(store),"chunks":store}, open(os.path.join(d,safe+".json"),"w"), ensure_ascii=False)
    return json.dumps({"status":"success","name":p["name"],"chunks":len(store)}, ensure_ascii=False)
