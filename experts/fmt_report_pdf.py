$extens("include.py")
include("import fpdf", ["extella-pip install fpdf2"])

def fmt_report_pdf(input_path: str = "", records_json: str = "", spec_json: str = "",
                   output_path: str = "", brand_name: str = "", accent: str = "",
                   footer_note: str = "", sections_json: str = "") -> dict:
    """Оформитель отчётов: записи + СПЕКА ВИДА → PDF. Чистый Python (fpdf2) — работает на любом
    устройстве с листенером, без системного Chrome: у клиента может не быть ни нашего хостинга,
    ни браузера (требование Анвара «способность зашита в эксперта»).

    Документ принадлежит КЛИЕНТУ: знаков Extella здесь нет, шапка — имя владельца процесса.

    Спека вида (её и будет править клиент словами):
      {"report":"<заголовок>", "subtitle":"<подзаголовок>",
       "headline":{"metric":"count"|"sum","field":"<для sum>","label":"<подпись>"},
       "views":[{"group_by":"<колонка>","title":"<название раздела>"}],
       "style":{"accent":"#RRGGBB","brand_name":"...","footer":"..."}}
    Пустая спека = разумные умолчания: сам найдёт, по чему группировать.
    """
    import json
    import os
    from pathlib import Path
    from datetime import datetime, timezone
    from fpdf import FPDF

    # ── готовые разрезы: превью рисуется по числам ПОСЛЕДНЕГО ПРОГОНА, без повторного
    #    похода за данными. Владелец видит свой отчёт, а не выдуманный образец. ──
    ready = []
    if sections_json and not sections_json.startswith("{{"):
        try:
            ready = [s for s in (json.loads(sections_json) or []) if isinstance(s, dict) and s.get("items")]
        except Exception:
            ready = []

    # ── записи: из файла стадии или строкой ──
    recs = []
    if records_json and not records_json.startswith("{{"):
        try:
            recs = json.loads(records_json)
        except Exception:
            return {"status": "error", "message": "records_json не разобрался"}
    elif input_path and not input_path.startswith("{{") and Path(input_path).exists():
        try:
            data = json.loads(Path(input_path).read_text(encoding="utf-8"))
        except Exception:
            return {"status": "error", "message": "вход не читается как JSON: " + str(input_path)[:120]}
        recs = data if isinstance(data, list) else (data.get("records") or data.get("rows") or [])
    if not ready:
        if not isinstance(recs, list) or not recs:
            return {"status": "error", "message": "нет данных для отчёта (ни records_json, ни input_path, ни sections_json)"}
        recs = [r for r in recs if isinstance(r, dict)]
        if not recs:
            return {"status": "error", "message": "записи не в формате объектов"}
    else:
        recs = [r for r in (recs or []) if isinstance(r, dict)]

    spec = {}
    if spec_json and not spec_json.startswith("{{"):
        try:
            spec = json.loads(spec_json) or {}
        except Exception:
            spec = {}
    style = spec.get("style") or {}
    accent = accent or style.get("accent") or "#2F6B66"
    brand_name = brand_name or style.get("brand_name") or ""
    footer_note = footer_note or style.get("footer") or ""

    cols = list(recs[0].keys()) if recs else []

    def _num(v):
        try:
            return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
        except Exception:
            return None

    # ── разрезы: из спеки, иначе сами выбираем колонки-категории (мало уникальных значений) ──
    views = [v for v in (spec.get("views") or []) if isinstance(v, dict) and v.get("group_by") in cols]
    if not views:
        cand = []
        for c in cols:
            vals = [str(r.get(c, "")).strip() for r in recs if str(r.get(c, "")).strip()]
            uniq = len(set(vals))
            if vals and 1 < uniq <= max(2, min(12, len(recs))) and uniq < len(vals):
                cand.append((uniq, c))
        views = [{"group_by": c, "title": "По полю «" + c + "»"} for _, c in sorted(cand)[:3]]

    sections = list(ready)   # готовые разрезы имеют приоритет: это факты прошлого прогона
    for v in ([] if ready else views[:4]):
        g = v["group_by"]
        agg = {}
        for r in recs:
            k = str(r.get(g, "")).strip()
            agg[k or "не заполнено"] = agg.get(k or "не заполнено", 0) + 1
        # «не заполнено» — не категория данных, а качество данных: всегда внизу списка,
        # иначе пустое поле выглядит как полноценный разрез наравне с настоящими
        _named = {k: n for k, n in agg.items() if k != "не заполнено"}
        _items = dict(sorted(_named.items(), key=lambda x: -x[1])[:12])
        if agg.get("не заполнено"):
            _items["не заполнено"] = agg["не заполнено"]
        sections.append({"title": v.get("title") or g, "items": _items})

    # ── главное число ──
    hl = spec.get("headline") or {}
    if ready and not recs:
        _tot = spec.get("total")
        headline = {"value": _tot if _tot is not None else sum(sum(s["items"].values()) for s in ready),
                    "label": hl.get("label") or "записей\nв отчёте"}
    elif str(hl.get("metric", "")) == "sum" and hl.get("field") in cols:
        tot = sum(_num(r.get(hl["field"])) or 0 for r in recs)
        headline = {"value": ("%.0f" % tot).replace(",", " "), "label": hl.get("label") or hl["field"]}
    else:
        headline = {"value": len(recs), "label": hl.get("label") or "записей\nв отчёте"}

    # ── шрифт: статический, с кириллицей. Вариативный DM Sans НЕ подходит (нет кириллицы
    #    в базовом начертании и fpdf2 не берёт переменные шрифты) — проверено. ──
    FONTS = ["/System/Library/Fonts/Supplemental/Arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "C:/Windows/Fonts/arial.ttf"]
    BOLDS = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf"]
    reg = next((p for p in FONTS if os.path.exists(p)), "")
    bold = next((p for p in BOLDS if os.path.exists(p)), "") or reg
    if not reg:
        return {"status": "error", "code": "no_font",
                "message": "на устройстве нет статического шрифта с кириллицей — PDF не собрать. "
                           "Linux: apt-get install fonts-dejavu-core"}

    def _rgb(h):
        h = str(h or "").lstrip("#")
        if len(h) != 6:
            h = "2F6B66"
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            return (47, 107, 102)

    A = _rgb(accent)
    INK, INK2, SILVER, LINE, CREAM = (26, 26, 26), (42, 42, 42), (140, 140, 140), (235, 232, 225), (245, 243, 238)
    out = output_path or ("/tmp/report_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".pdf")

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(True, margin=22)
    pdf.add_font("body", "", reg)
    pdf.add_font("body", "B", bold)
    pdf.add_page()
    pdf.set_margins(18, 16, 18)
    W = 210 - 36

    pdf.set_font("body", "B", 11)
    pdf.set_text_color(*INK)
    if brand_name:
        pdf.cell(W * 0.6, 6, brand_name)
    pdf.set_font("body", "", 9)
    pdf.set_text_color(*SILVER)
    pdf.cell(W * (0.4 if brand_name else 1.0), 6,
             datetime.now(timezone.utc).strftime("%d.%m.%Y"), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_draw_color(*A)
    pdf.set_line_width(0.7)
    pdf.line(18, pdf.get_y(), 210 - 18, pdf.get_y())
    pdf.ln(6)

    pdf.set_font("body", "B", 19)
    pdf.set_text_color(*INK)
    pdf.multi_cell(W, 8.5, str(spec.get("report") or "Отчёт"), align="L")
    if spec.get("subtitle"):
        pdf.ln(1)
        pdf.set_font("body", "", 10)
        pdf.set_text_color(*INK2)
        pdf.multi_cell(W, 5.2, str(spec["subtitle"]), align="L")
    pdf.ln(4)

    y0 = pdf.get_y()
    pdf.set_fill_color(*CREAM)
    pdf.rect(18, y0, W, 24, "F")
    pdf.set_xy(24, y0 + 4)
    pdf.set_font("body", "B", 26)
    pdf.set_text_color(*A)
    pdf.cell(pdf.get_string_width(str(headline["value"])) + 4, 14, str(headline["value"]))
    pdf.set_font("body", "", 10)
    pdf.set_text_color(*INK2)
    lx, ly = pdf.get_x(), pdf.get_y()
    for i, ln in enumerate([x for x in str(headline["label"]).split("\n") if x.strip()][:2]):
        pdf.set_xy(lx, ly + 3.2 + i * 4.4)
        pdf.cell(60, 5, ln)
    pdf.set_y(y0 + 24)
    pdf.ln(6)

    # шкала ОБЩАЯ для всех разделов: иначе одинаковая длина полосы означает разное
    allv = [v for s in sections for v in s["items"].values()]
    gmax = max(allv) if allv else 1
    for s in sections:
        pdf.set_font("body", "B", 8)
        pdf.set_text_color(*SILVER)
        pdf.cell(W, 5, str(s["title"]).upper(), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        for k, v in s["items"].items():
            yb = pdf.get_y()
            pdf.set_font("body", "", 9.5)
            pdf.set_text_color(*INK)
            pdf.cell(52, 6, str(k)[:34])
            bx, bw = 70, W - 66
            pdf.set_fill_color(*LINE)
            pdf.rect(bx, yb + 2.2, bw, 2.2, "F")
            pdf.set_fill_color(*A)
            pdf.rect(bx, yb + 2.2, max(1.5, bw * (float(v) / gmax)), 2.2, "F")
            pdf.set_xy(18 + W - 14, yb)
            pdf.set_font("body", "B", 9.5)
            pdf.set_text_color(*INK2)
            pdf.cell(14, 6, str(v), align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_draw_color(*LINE)
            pdf.set_line_width(0.15)
            pdf.line(18, pdf.get_y(), 210 - 18, pdf.get_y())
        pdf.ln(5)
    if len(sections) > 1:
        pdf.set_font("body", "", 7.5)
        pdf.set_text_color(*SILVER)
        pdf.cell(W, 4, "Длина полосы сопоставима между разделами: общая шкала, максимум — %s" % gmax,
                 new_x="LMARGIN", new_y="NEXT")

    if footer_note:
        pdf.set_auto_page_break(False)
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.15)
        pdf.line(18, 277, 210 - 18, 277)
        pdf.set_xy(18, 279)
        pdf.set_font("body", "", 8)
        pdf.set_text_color(*SILVER)
        pdf.cell(W, 5, footer_note)

    try:
        pdf.output(out)
    except Exception as e:
        return {"status": "error", "message": "PDF не сохранился: " + str(e)[:140]}
    return {"status": "success", "path": out, "bytes": os.path.getsize(out),
            "records": len(recs), "preview": bool(ready and not recs), "sections": [s["title"] for s in sections],
            "font": os.path.basename(reg)}
