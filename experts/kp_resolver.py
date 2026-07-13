# expert: kp_resolver
# description: База знаний: ставит движок (Ollama + модель эмбеддингов nomic-embed-text) на устройство. Зови ПЕРЕД первой сборкой базы у нового пользователя.

def kp_resolver() -> str:
    import os, json, subprocess, time, urllib.request
    def ob():
        import shutil
        cands = ["/usr/local/bin/ollama","/opt/homebrew/bin/ollama",
                 "/Applications/Ollama.app/Contents/Resources/ollama",
                 os.path.expanduser("~/Applications/Ollama.app/Contents/Resources/ollama")]
        for p in cands:
            if os.path.exists(p): return p
        w = shutil.which("ollama")
        return w or ""
    b = ob()
    if not b:
        import platform, tempfile
        # 1) через Homebrew, если он есть
        brew = "/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew") else ("/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew") else "")
        if brew:
            try: subprocess.run([brew,"install","ollama"], capture_output=True, text=True, timeout=600)
            except Exception: pass
            b = ob()
        # 2) без Homebrew — сами скачиваем официальный Ollama.app (macOS)
        if not b and platform.system() == "Darwin":
            try:
                z = os.path.join(tempfile.gettempdir(), "ollama_dl.zip")
                urllib.request.urlretrieve("https://ollama.com/download/Ollama-darwin.zip", z)
                dest = "/Applications"
                if not os.access(dest, os.W_OK):
                    dest = os.path.expanduser("~/Applications"); os.makedirs(dest, exist_ok=True)
                subprocess.run(["ditto","-x","-k",z,dest], capture_output=True, text=True, timeout=300)
                try: os.remove(z)
                except Exception: pass
                b = ob()
            except Exception as e:
                return json.dumps({"status":"error","message":"Не удалось авто-установить Ollama: "+str(e)[:80]+". Можно вручную: https://ollama.com/download"}, ensure_ascii=False)
        if not b:
            return json.dumps({"status":"error","message":"Для баз знаний нужен движок Ollama: https://ollama.com/download"}, ensure_ascii=False)
    def serving():
        try: urllib.request.urlopen("http://localhost:11434/api/version", timeout=3); return True
        except Exception: return False
    if not serving():
        subprocess.Popen([b,"serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(12):
            time.sleep(2)
            if serving(): break
    if not serving(): return json.dumps({"status":"error","message":"Ollama не запускается."}, ensure_ascii=False)
    have = False
    try:
        r = subprocess.run([b,"list"], capture_output=True, text=True, timeout=30)
        have = "nomic-embed-text" in (r.stdout or "")
    except Exception: pass
    if not have:
        try: subprocess.run([b,"pull","nomic-embed-text"], capture_output=True, text=True, timeout=900)
        except Exception as e: return json.dumps({"status":"error","message":"Не удалось скачать модель эмбеддингов: "+str(e)[:90]}, ensure_ascii=False)
    return json.dumps({"status":"ok","message":"Движок знаний готов (Ollama + nomic-embed-text)."}, ensure_ascii=False)