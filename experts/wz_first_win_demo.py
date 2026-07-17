# expert: wz_first_win_demo
# description: Онбординг «первая победа за 5 минут» — самодостаточный демо-прогон на вшитой синтетике.
#   Реально считает сводку (не хардкод текста) и рендерит отчёт. Глобальный, детерминированный, ~1с,
#   работает на пустом аккаунте без стройки. Данные — синтетические, помечены как демо.

def wz_first_win_demo(example: str = "ads") -> dict:
    from datetime import datetime, timezone

    # --- вшитая синтетика (пример: ежедневный отчёт по рекламным бюджетам) ---
    rows = [
        {"client": "Альфа-Трейд",    "platform": "Meta Ads",      "campaign": "Лето_2025",     "spend": 184300, "budget_left_pct": 8},
        {"client": "Альфа-Трейд",    "platform": "Google Ads",    "campaign": "Поиск_бренд",   "spend": 96500,  "budget_left_pct": 8},
        {"client": "Альфа-Трейд",    "platform": "Яндекс Директ", "campaign": "Ретаргет",      "spend": 41200,  "budget_left_pct": 8},
        {"client": "Альфа-Трейд",    "platform": "VK Реклама",    "campaign": "РСЯ_акции",     "spend": 22800,  "budget_left_pct": 8},
        {"client": "Бета-Логистика", "platform": "Google Ads",    "campaign": "Лиды_июль",     "spend": 73400,  "budget_left_pct": 21},
        {"client": "Бета-Логистика", "platform": "Telegram Ads",  "campaign": "Охваты_лето",   "spend": 15900,  "budget_left_pct": 21},
        {"client": "Гамма-Строй",    "platform": "Meta Ads",      "campaign": "Каталог",       "spend": 51200,  "budget_left_pct": 64},
        {"client": "Гамма-Строй",    "platform": "Яндекс Директ", "campaign": "Поиск_услуги",  "spend": 33800,  "budget_left_pct": 64},
        {"client": "Дельта-Фуд",     "platform": "VK Реклама",    "campaign": "Канал_охваты",  "spend": 28100,  "budget_left_pct": 47},
        {"client": "Дельта-Фуд",     "platform": "Telegram Ads",  "campaign": "Конверсии",     "spend": 19700,  "budget_left_pct": 47},
        {"client": "Эпсилон-Мед",    "platform": "Google Ads",    "campaign": "Бренд_поиск",   "spend": 44600,  "budget_left_pct": 33},
        {"client": "Зета-Авто",      "platform": "Meta Ads",      "campaign": "Лето_охваты",   "spend": 61500,  "budget_left_pct": 12},
    ]

    def by(key):
        agg = {}
        for r in rows:
            agg[r[key]] = agg.get(r[key], 0) + 1
        return agg

    total_count = len(rows)
    total_spend = sum(r["spend"] for r in rows)
    # реальные бизнес-алерты из данных (не хардкод)
    crit = sorted({r["client"] for r in rows if r["budget_left_pct"] <= 12})
    summary = {"total_count": total_count, "total_sum": total_spend,
               "by_client": by("client"), "by_platform": by("platform"), "by_campaign": by("campaign")}

    def fmt(n):
        return format(n, ",").replace(",", " ")

    top = sorted(({"client": r["client"], "pct": r["budget_left_pct"]} for r in rows),
                 key=lambda x: x["pct"])
    seen, budget_rows = set(), []
    for t in top:
        if t["client"] in seen:
            continue
        seen.add(t["client"])
        dot = "🔴 пополнить" if t["pct"] <= 12 else ("🟡 следить" if t["pct"] <= 25 else "🟢 ок")
        budget_rows.append("| %s | %s%% | %s |" % (t["client"], t["pct"], dot))

    md = [
        "## Ежедневная отчётность и контроль рекламных бюджетов",
        "",
        "_Демо-прогон на синтетических данных · %s UTC_" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "",
        "**Итого:** %d позиций по %d клиентам · расход **%s ₸**. Остаток бюджета критический у %d клиентов."
        % (total_count, len(summary["by_client"]), fmt(total_spend), len(crit)),
        "",
        "### Остаток бюджета по клиентам",
        "| Клиент | Остаток | Статус |",
        "|---|---|---|",
    ] + budget_rows + [
        "",
        "### На подтверждение",
        "- **Бюджет почти исчерпан** — %s: нужно пополнение до простоя кампаний" % ", ".join(crit),
        "- **Проверьте распределение** — Meta Ads и Google Ads держат основную долю расхода",
        "",
        "_Это то, что процесс собирает и присылает каждый день сам. Соберите такой под свои данные._",
    ]

    return {"status": "success", "example": example,
            "digest_md": "\n".join(md), "summary": summary, "synthetic": True}
