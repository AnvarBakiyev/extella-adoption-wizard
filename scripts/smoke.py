#!/usr/bin/env python3
"""smoke.py — сквозной smoke-тест keyless-круга Визарда через живой мост.

СЛУЖЕБНЫЙ инструмент РАЗРАБОТЧИКА. Гонять ПЕРЕД релизом и ПОСЛЕ рефакторинга (Фаза 1/4),
чтобы убедиться: рабочий путь не сломан. Требует запущенный мост на 127.0.0.1:8765
в keyless-режиме (llm_api_key пуст → платформенная Qwen).

Проверяет инварианты:
  1) /x/health отвечает, версия есть
  2) сессия создаётся, 8 ответов интервью сохраняются
  3) образец грузится
  4) blueprint: status=success, стадий >= 3
  5) стройка: доходит до 'built', аудит 'allow', собран >=1 эксперт
     (сам факт 'built' уже гарантирует, что стадии ВЫЧИСЛИЛИ числа — это условие приёмки _build_one)

Выход 0 — всё зелёно; 1 — есть провал. Пер-проверка печатает PASS/FAIL.
"""
import sys, json, time, io, base64, urllib.request

BR = "http://127.0.0.1:8765"
FAILS = []


def _req(path, payload=None, timeout=960):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BR + path, data=data,
                               headers={"Content-Type": "application/json"},
                               method="POST" if data is not None else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as x:
        return json.loads(x.read().decode())


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def main():
    print("=== smoke: keyless-круг Визарда ===")
    # 1) health
    try:
        h = _req("/x/health", timeout=6)
    except Exception as e:
        print(f"  [FAIL] мост недоступен на {BR}: {e}"); sys.exit(1)
    check("health: версия", h.get("status") == "ok" and h.get("version"), f"v{h.get('version')}")

    # 2) сессия + ответы
    s = _req("/x/expert", {"expert_name": "wz_session",
                           "params": {"action": "create", "client_name": "SMOKE TEST"}})
    sid = (s.get("session") or {}).get("session_id")
    check("сессия создана", sid, sid or "нет session_id")
    ans = {"bol": "еженедельная сводка продаж по филиалам вручную", "roli": "аналитик→директор",
           "vhod": "Excel: Филиал, Продажи", "rezultat": "итоговая сумма и разбивка по филиалам",
           "period": "еженедельно", "kanaly": "Telegram", "ogranich": "ПДн нет", "uspeh": "сумма совпадает"}
    a = _req("/x/expert", {"expert_name": "wz_session",
                           "params": {"action": "save_answers", "session_id": sid,
                                      "payload_json": json.dumps(ans, ensure_ascii=False)}})
    check("8 ответов сохранены", a.get("answers_count") == 8, f"answers_count={a.get('answers_count')}")

    # 3) образец
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(["Филиал", "Продажи"])
    for row in [["Алматы", 12000000], ["Астана", 9000000], ["Шымкент", 5000000]]:
        ws.append(row)
    buf = io.BytesIO(); wb.save(buf)
    up = _req("/x/upload", {"session_id": sid, "filename": "prodazhi.xlsx",
                            "content_base64": base64.b64encode(buf.getvalue()).decode()})
    check("образец загружен", up.get("status") == "success", up.get("status"))

    # 4) blueprint
    t0 = time.time()
    bp = {"status": None}
    for _ in range(2):
        bp = _req("/x/expert", {"expert_name": "wz_generate_blueprint",
                                "params": {"session_id": sid, "language": "ru"}})
        if bp.get("status") == "success":
            break
        time.sleep(3)
    check("blueprint success + стадий>=3",
          bp.get("status") == "success" and (bp.get("stages_count") or 0) >= 3,
          f"status={bp.get('status')} стадий={bp.get('stages_count')} за {int(time.time()-t0)}с")

    # 5) стройка → built
    b = _req("/x/build", {"session_id": sid})
    bid = b.get("build_id")
    check("стройка запущена", bid, bid or str(b)[:120])
    prog, t0 = {}, time.time()
    if bid:
        for _ in range(90):  # ~12 мин потолок
            time.sleep(8)
            try:
                prog = (_req("/x/build_progress?build_id=" + bid, timeout=30).get("progress") or {})
            except Exception:
                continue
            if prog.get("status") in ("built", "done", "success", "error", "failed"):
                break
    st = prog.get("status")
    verdict = (prog.get("audit") or {}).get("verdict")
    built = prog.get("built_experts") or []
    check("стройка дошла до 'built'", st == "built", f"status={st} за {int(time.time()-t0)}с")
    check("аудит 'allow'", verdict == "allow", f"verdict={verdict}")
    check("собран >=1 эксперт", len(built) >= 1, f"built_experts={built}")

    print(f"\n{'✅ SMOKE PASS' if not FAILS else '❌ SMOKE FAIL: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
