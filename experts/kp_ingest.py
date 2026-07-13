# expert: kp_ingest
# description: База знаний: загружает документы (.txt/.md/.pdf) из папки, режет на куски и векторизует ЛОКАЛЬНО (nomic-embed-text). Параметры: name, folder.

def kp_ingest(name="", folder="") -> str:
    import os, re, json, glob, subprocess, time, urllib.request
    if not name or name.startswith("{{"): return json.dumps({"status":"error","message":"нужно имя базы"}, ensure_ascii=False)
    folder = os.path.expanduser(folder or "")
    if not folder or not os.path.isdir(folder): return json.dumps({"status":"error","message":"нужна существующая папка"}, ensure_ascii=False)
    def serve():
        try: urllib.request.urlopen("http://localhost:11434/api/version", timeout=3); return True
        except Exception: pass
        for ob in ["/usr/local/bin/ollama","/opt/homebrew/bin/ollama"]:
            if os.path.exists(ob):
                subprocess.Popen([ob,"serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); break
        time.sleep(3)
        try: urllib.request.urlopen("http://localhost:11434/api/version", timeout=5); return True
        except Exception: return False
    if not serve(): return json.dumps({"status":"error","message":"Ollama не запущен"}, ensure_ascii=False)
    def embed_batch(texts):
        req=urllib.request.Request("http://localhost:11434/api/embed", data=json.dumps({"model":"nomic-embed-text","input":["search_document: "+t for t in texts]}).encode(), headers={"Content-Type":"application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=120).read()).get("embeddings", [])
    def read(fp):
        e=os.path.splitext(fp)[1].lower()
        if e in (".txt",".md"):
            try: return open(fp, encoding="utf-8", errors="ignore").read()
            except Exception: return ""
        if e==".pdf":
            for pb in ["/opt/homebrew/bin/pdftotext","/usr/local/bin/pdftotext"]:
                if os.path.exists(pb):
                    try: return subprocess.run([pb, fp, "-"], capture_output=True, text=True, timeout=60).stdout or ""
                    except Exception: return ""
        return ""
    def chunks(txt, size=1200, ov=150):
        txt=re.sub(r"\s+"," ",txt).strip(); out=[]
        parts=re.split(r"(?=Статья\s+\d+[.\s])", txt)
        if len(parts) < 3: parts=[txt]
        for p in parts:
            p=p.strip()
            if not p: continue
            if len(p) <= size: out.append(p)
            else:
                i=0
                while i < len(p): out.append(p[i:i+size]); i+=size-ov
        return out
    files=[f for f in glob.glob(os.path.join(folder,"**","*"), recursive=True) if os.path.splitext(f)[1].lower() in (".txt",".md",".pdf")]
    if not files: return json.dumps({"status":"error","message":"в папке нет файлов .txt/.md/.pdf"}, ensure_ascii=False)
    items=[]
    for fp in files:
        for ch in chunks(read(fp)):
            if ch.strip(): items.append((ch, os.path.basename(fp)))
    if not items: return json.dumps({"status":"error","message":"файлы есть, но текста в них не нашлось"}, ensure_ascii=False)
    store=[]; last_err=""
    for i in range(0, len(items), 64):
        part=items[i:i+64]
        try:
            embs=embed_batch([t for t, s in part])
            for j in range(len(part)):
                if j < len(embs) and embs[j]: store.append({"text":part[j][0],"src":part[j][1],"emb":embs[j]})
        except Exception as e: last_err=str(e)[:110]
    if not store: return json.dumps({"status":"error","message":"файлы найдены ("+str(len(files))+"), но эмбеддинг не сработал: "+(last_err or "нет ответа от Ollama")}, ensure_ascii=False)
    d=os.path.expanduser("~/.extella_kp"); os.makedirs(d, exist_ok=True)
    safe=re.sub(r"[^a-zA-Z0-9_]","_",name)
    json.dump({"name":name,"count":len(store),"chunks":store}, open(os.path.join(d,safe+".json"),"w"), ensure_ascii=False)
    return json.dumps({"status":"success","name":name,"chunks":len(store),"files":len(files)}, ensure_ascii=False)