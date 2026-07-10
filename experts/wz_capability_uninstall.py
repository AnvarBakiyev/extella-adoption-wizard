# expert: wz_capability_uninstall
# description: РЕАЛЬНОЕ удаление способности с устройства (не только запись в списке): model -> Ollama api/delete (освобождает гигабайты), mcp -> вычистить из ~/.extella_mcp/allowlist.json, cli(brew) -> brew uninstall. Защита: nomic-embed-text не удаляем (движок баз знаний). Возвращает {status, device_removed, freed, message}.

def wz_capability_uninstall(kind="", ref="", method="", api_token="", api_base="https://api.extella.ai",
                            target="", client="") -> str:
    import json, os, urllib.request, urllib.error
    from pathlib import Path

    def _b(v):
        return (not v) or str(v).startswith("{{")

    if _b(kind) or _b(ref):
        return json.dumps({"status": "error", "message": "нужны kind и ref"}, ensure_ascii=False)
    if _b(method):
        # зеркало карты установщика: service тоже живёт в MCP-аллоулисте
        method = {"model": "ollama", "mcp": "mcp_connect", "service": "mcp_connect", "cli": "brew"}.get(kind, "")

    device_removed = False
    freed = ""
    msg = ""

    def _ollama_names():
        # None = Ollama не отвечает (не знаем), список = точный факт
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=6) as r:
                return [m.get("name", "") for m in json.loads(r.read().decode("utf-8")).get("models", [])]
        except Exception:
            return None

    try:
        if method == "ollama":
            # защита (без учёта регистра): движок эмбеддингов баз знаний — без него ломаются все kp_*
            if "nomic-embed-text" in ref.lower():
                return json.dumps({"status": "error", "device_removed": False,
                                   "message": "nomic-embed-text — движок баз знаний (Знания перестанут работать); не удаляю"},
                                  ensure_ascii=False)
            # размер до удаления — честный отчёт «сколько освободили»
            size_gb = 0.0
            try:
                with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=6) as r:
                    for m in json.loads(r.read().decode("utf-8")).get("models", []):
                        if m.get("name") == ref:
                            size_gb = round((m.get("size", 0) or 0) / 1e9, 1)
            except Exception:
                pass
            body = json.dumps({"model": ref, "name": ref}).encode("utf-8")  # оба ключа: старый/новый API Ollama
            req = urllib.request.Request("http://localhost:11434/api/delete", data=body,
                                         headers={"Content-Type": "application/json"}, method="DELETE")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    r.read()
                device_removed = True
                freed = ("%.1f ГБ" % size_gb) if size_gb else ""
                msg = "модель удалена из Ollama" + ((" (освобождено ~%s)" % freed) if freed else "")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    names = _ollama_names()
                    similar = [n for n in (names or []) if n.startswith(ref + ":") or n.split(":", 1)[0] == ref]
                    if names is not None and similar:
                        # ref без тега/с другим тегом — модель НА МЕСТЕ под похожим именем, не врём про чистоту
                        msg = "точного имени нет, но установлена похожая: %s — укажи её полное имя" % ", ".join(similar[:3])
                    elif names is not None:
                        device_removed = True   # перепроверили по /api/tags: модели реально нет
                        msg = "модели уже не было на устройстве"
                    else:
                        msg = "Ollama не отвечает — не могу подтвердить удаление"
                else:
                    msg = "Ollama не удалил: HTTP %d" % e.code

        elif method == "mcp_connect":
            al = Path.home() / ".extella_mcp" / "allowlist.json"
            if not al.exists():
                device_removed = True
                msg = "аллоулиста нет — подключений не осталось"
            else:
                data = json.loads(al.read_text(encoding="utf-8"))
                servers = data.get("servers", data) if isinstance(data, dict) else None
                if not isinstance(servers, dict):
                    return json.dumps({"status": "error", "device_removed": False,
                                       "message": "неожиданный формат аллоулиста — руками не трогаю"}, ensure_ascii=False)
                # ТОЧНОЕ совпадение: ключ ИЛИ явные поля pkg/title записи (никаких подстрок по всему JSON —
                # ref вроде "mcp" снёс бы чужой сервер)
                hits = []
                if ref in servers:
                    hits = [ref]
                else:
                    for k, v in servers.items():
                        vv = v if isinstance(v, dict) else {}
                        if vv.get("pkg") == ref or vv.get("title") == ref or vv.get("id") == ref:
                            hits.append(k)
                if len(hits) > 1:
                    return json.dumps({"status": "error", "device_removed": False,
                                       "message": "нашёл несколько записей (%s) — не угадываю, укажи точный ключ" % ", ".join(hits[:5])},
                                      ensure_ascii=False)
                if hits:
                    hit = hits[0]
                    servers.pop(hit, None)
                    if isinstance(data, dict) and "servers" in data:
                        data["servers"] = servers
                    else:
                        data = servers
                    tmp = al.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(str(tmp), str(al))
                    device_removed = True
                    msg = "MCP-сервер отключён (вычищен из аллоулиста: %s)" % hit
                else:
                    device_removed = True   # в аллоулисте нет — отключён и так
                    msg = "в аллоулисте не найден — уже отключён"

        elif method == "brew":
            import subprocess, re
            brew = "/opt/homebrew/bin/brew"
            if not os.path.exists(brew):
                brew = "/usr/local/bin/brew"
            if not os.path.exists(brew):
                return json.dumps({"status": "error", "device_removed": False,
                                   "message": "Homebrew не найден на устройстве"}, ensure_ascii=False)
            formula = ref.split(":", 1)[1] if ref.startswith("brew:") else ref
            if not re.fullmatch(r"[A-Za-z0-9@._+][A-Za-z0-9@._+/-]*", formula):
                return json.dumps({"status": "error", "device_removed": False,
                                   "message": "подозрительное имя формулы — не выполняю: %s" % formula[:60]}, ensure_ascii=False)
            env = dict(os.environ)
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
            # "--" отсекает трактовку имени как флага
            p = subprocess.run([brew, "uninstall", "--", formula], capture_output=True, text=True, timeout=120, env=env)
            if p.returncode == 0:
                device_removed = True
                msg = "brew uninstall %s — удалён с устройства" % formula
            elif "No such keg" in (p.stderr or ""):
                device_removed = True
                msg = "%s уже не установлен" % formula
            elif "No available formula" in (p.stderr or ""):
                device_removed = True
                msg = "формулы «%s» в brew нет — под этим именем на устройстве ничего не стояло" % formula
            else:
                msg = "brew uninstall не прошёл: " + ((p.stderr or p.stdout or "").strip()[:160])

        elif method in ("manual", "rule", "link", ""):
            # ссылка/навык/ручная запись — на устройстве ничего не лежит
            device_removed = True
            msg = "на устройстве ничего не установлено (запись-ссылка) — чистить нечего"
        else:
            # неизвестный метод — НЕ врём про успех
            return json.dumps({"status": "error", "device_removed": False,
                               "message": "неизвестный метод установки «%s» — не знаю, как чистить" % method}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "device_removed": False,
                           "message": str(e)[:200]}, ensure_ascii=False)

    return json.dumps({"status": "success" if device_removed else "error",
                       "device_removed": device_removed, "freed": freed,
                       "kind": kind, "ref": ref, "method": method,
                       "message": msg}, ensure_ascii=False)
