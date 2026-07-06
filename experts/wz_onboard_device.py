# expert: wz_onboard_device
# description: Одно-кликовый онбординг устройства клиента (A2): сводит 4 ручных шага в ОДИН вызов, каждый — пиннингом на устройство <target> через /api/expert/run. Ш
# params: api_token, target, client, pin, llm_api_key, llm_base_url, llm_model, port, app_dir, seed_library, autostart, label, api_base

$extens("include.py")
include("import requests", ["extella-pip install requests"])

def wz_onboard_device(api_token: str = "", target: str = "", client: str = "default", pin: str = "",
                      llm_api_key: str = "", llm_base_url: str = "https://api.openai.com/v1",
                      llm_model: str = "gpt-4o", port: int = 8765, app_dir: str = "",
                      seed_library: bool = True, autostart: bool = True,
                      label: str = "ai.extella.wizard-bridge",
                      api_base: str = "https://api.extella.ai") -> dict:
    """Одно-кликовый онбординг устройства клиента (A2): сводит 4 ручных шага в ОДИН вызов,
    каждый — пиннингом на устройство <target> через /api/expert/run. Шаги:
      1) wz_wizard_serve  — развернуть+запустить мост+UI+каталог (обязательный, стоп при провале);
      2) wz_seed_library  — засеять отраслевую библиотеку;
      3) wz_vault_provision {pin,client} — vault-ключ из PIN (только если задан pin);
      4) wz_install_autostart — автозапуск сервиса (launchd/systemd).
    Возвращает {ok, target, steps:[{step,ok,detail}], ready}. Токен уходит на устройство в конфиг
    моста (так и задумано). app_dir/port/label — для изолированного теста без клоббера живого моста."""
    import json
    import requests

    if not api_token:
        return {"ok": False, "err": "нужен api_token (токен Extella для конфига моста)"}
    if not target:
        return {"ok": False, "err": "нужен target (id устройства клиента в Extella)"}
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def run(expert, params, timeout=600):
        body = {"expert_name": expert, "params": params, "global": True, "target": target}
        try:
            r = requests.post(api_base.rstrip("/") + "/api/expert/run", headers=headers, json=body, timeout=timeout)
            out = r.json().get("result", r.json())
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except Exception:
                    try:
                        import ast
                        out = ast.literal_eval(out)
                    except Exception:
                        out = {"raw": out[:150]}
            return out if isinstance(out, dict) else {"raw": str(out)[:150]}
        except Exception as e:
            return {"error": str(e)[:150]}

    steps = []

    # 1) МОСТ (обязательный) — стоп при провале
    sp = {"auth_token": api_token, "llm_api_key": llm_api_key, "llm_base_url": llm_base_url,
          "llm_model": llm_model, "port": port}
    if app_dir:
        sp["app_dir"] = app_dir
    r1 = run("wz_wizard_serve", sp, timeout=900)
    ok1 = r1.get("status") == "success"
    steps.append({"step": "bridge", "ok": ok1,
                  "detail": r1.get("url") or r1.get("message") or r1.get("error") or r1.get("health")})
    if not ok1:
        return {"ok": False, "target": target, "steps": steps, "ready": False,
                "err": "мост не поднялся — онбординг остановлен"}

    # 2) БИБЛИОТЕКА
    if seed_library:
        r2 = run("wz_seed_library", {})
        ok2 = bool(r2.get("ok") or r2.get("status") == "success" or r2.get("written")
                   or r2.get("industries") or r2.get("seeded"))
        steps.append({"step": "library", "ok": ok2,
                      "detail": r2.get("message") or r2.get("industries") or r2.get("error") or "ok"})

    # 3) VAULT ИЗ PIN (только если задан pin)
    if pin:
        r3 = run("wz_vault_provision", {"pin": pin, "client": client})
        ok3 = bool(r3.get("ok"))
        steps.append({"step": "vault", "ok": ok3,
                      "detail": ("key " + str(r3.get("key_sha256"))) if ok3 else (r3.get("err") or r3.get("error"))})

    # 4) АВТОЗАПУСК
    if autostart:
        ap = {"port": port, "label": label}
        if app_dir:
            ap["app_dir"] = app_dir
        r4 = run("wz_install_autostart", ap)
        steps.append({"step": "autostart", "ok": bool(r4.get("ok")),
                      "detail": r4.get("method") or r4.get("err") or r4.get("error")})

    ready = all(s["ok"] for s in steps)
    return {"ok": ready, "target": target, "client": client, "steps": steps, "ready": ready,
            "next": ("Устройство готово: мост на порту %d, библиотека, vault%s. "
                     "Открывайте визард и подключайте каналы/источники." % (port, "" if pin else " (PIN не задан — vault пропущен)"))}
