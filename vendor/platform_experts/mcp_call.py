def mcp_call(server="", tool="__list__", args_json="{}") -> str:
    import json, os, subprocess, threading, queue, time
    server = "" if (not server or str(server).startswith("{{")) else str(server).strip()
    tool = "__list__" if (not tool or str(tool).startswith("{{")) else str(tool).strip()
    args_json = "{}" if (not args_json or str(args_json).startswith("{{")) else str(args_json)
    def err(m): return json.dumps({"status":"error","message":m}, ensure_ascii=False)

    def _abs(cmd0):
        ABS = {"uvx": ["/opt/homebrew/bin/uvx", "/usr/local/bin/uvx"],
               "npx": ["/opt/homebrew/bin/npx", "/usr/local/bin/npx", "/opt/homebrew/opt/node@24/bin/npx"]}
        for p_ in ABS.get(cmd0, []):
            if os.path.exists(p_): return p_
        return cmd0
    def _session(cmd, timeout):
        env = dict(os.environ); env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        q = queue.Queue()
        def reader():
            for line in p.stdout:
                line = line.strip()
                if line:
                    try: q.put(json.loads(line))
                    except Exception: pass
        threading.Thread(target=reader, daemon=True).start()
        rid = [0]
        def rpc(method, params=None, tmo=timeout):
            rid[0] += 1
            msg = {"jsonrpc":"2.0","id":rid[0],"method":method}
            if params is not None: msg["params"] = params
            p.stdin.write(json.dumps(msg)+"\n"); p.stdin.flush()
            deadline = time.time()+tmo
            while time.time() < deadline:
                try:
                    m = q.get(timeout=1.0)
                    if m.get("id") == rid[0]: return m
                except Exception:
                    if p.poll() is not None: return {"error":{"message":"server died"}}
            return {"error":{"message":"timeout"}}
        def notify(method):
            p.stdin.write(json.dumps({"jsonrpc":"2.0","method":method})+"\n"); p.stdin.flush()
        return p, rpc, notify

    BUILTIN = {
        "fetch": ["uvx", "mcp-server-fetch"],
        "time":  ["uvx", "mcp-server-time"],
        "git":   ["uvx", "mcp-server-git"],
        "filesystem": ["npx", "-y", "@modelcontextprotocol/server-filesystem", os.path.expanduser("~/Downloads")],
    }
    cmd = BUILTIN.get(server)
    if cmd is None:
        try:
            allow = json.load(open(os.path.expanduser("~/.extella_mcp/allowlist.json")))
            ent = allow.get(server)
            if ent: cmd = ent.get("cmd")
        except Exception: pass
    if not cmd: return err("сервер не подключён: "+server+". Подключи его из каталога (mcp_connect) или используй: "+", ".join(BUILTIN))
    try: args = json.loads(args_json)
    except Exception: return err("args_json — не валидный JSON")
    cmd = [_abs(cmd[0])] + list(cmd[1:])
    try:
        p, rpc, notify = _session(cmd, 90)
    except Exception as e:
        return err("не запустился сервер: "+str(e)[:110])
    try:
        init = rpc("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"extella-bridge","version":"0.2"}})
        if "error" in init: return err("initialize: "+str(init["error"])[:140])
        notify("notifications/initialized")
        if tool == "__list__":
            r = rpc("tools/list", {})
            # СХЕМА АРГУМЕНТОВ. MCP-сервер честно отдаёт inputSchema каждого инструмента, а мы её
            # выбрасывали — наружу шли только имя и описание. Из-за этого всё, что строит вызов
            # автоматически (обёртки для Композитора, агент), вынуждено УГАДЫВАТЬ имена полей.
            # Отдаём урезанно: поле → тип и пояснение, плюс список обязательных; полную схему не
            # тащим, она бывает на килобайты. Правка аддитивная — прежние читатели берут name/desc.
            def _slim(sc):
                if not isinstance(sc, dict): return {}
                props = {}
                for k, v in list((sc.get("properties") or {}).items())[:20]:
                    if isinstance(v, dict):
                        props[str(k)[:40]] = {"type": v.get("type") or "string",
                                              "desc": str(v.get("description") or "")[:100]}
                return {"properties": props,
                        "required": [str(x)[:40] for x in (sc.get("required") or []) if isinstance(x, str)]}
            tools = [{"name":t["name"],"desc":(t.get("description") or "")[:120],
                      "schema":_slim(t.get("inputSchema"))} for t in r.get("result",{}).get("tools",[])]
            return json.dumps({"status":"success","server":server,"tools":tools}, ensure_ascii=False)
        r = rpc("tools/call", {"name":tool,"arguments":args})
        if "error" in r: return err(str(r["error"])[:220])
        res = r.get("result", {})
        texts = [c.get("text","") for c in res.get("content",[]) if c.get("type")=="text"]
        return json.dumps({"status":"success","server":server,"tool":tool,"is_error":res.get("isError",False),"result":("\n".join(texts))[:3000]}, ensure_ascii=False)
    finally:
        try: p.terminate()
        except Exception: pass