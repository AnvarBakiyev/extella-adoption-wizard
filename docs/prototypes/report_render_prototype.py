#!/usr/bin/env python3
"""Ядро оформителя отчётов. Чистый Python — работает на ЛЮБОМ устройстве, где живёт листенер,
без системного Chrome. Это и есть «способность зашита в эксперта» (требование Анвара).

Документ принадлежит КЛИЕНТУ: никаких знаков Extella. Клиент отдаёт отчёт своим заказчикам,
и наш логотип в его внутреннем документе неуместен. Шапка — имя владельца процесса,
акцентный цвет настраивается.

Шрифты: DM Sans вариативный не годится (нет кириллицы в базовом начертании + fpdf2 не берёт
переменные шрифты) — ищем статический шрифт с кириллицей среди системных, с честным отказом,
если не нашли. Порядок предпочтения одинаков на macOS и Linux.
"""
import os
from fpdf import FPDF

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",              # macOS
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",           # Linux/VPS
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",                                # Windows
]
BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _pick(cands):
    for p in cands:
        if os.path.exists(p):
            return p
    return None


def _hex(h):
    h = str(h or "").lstrip("#")
    if len(h) != 6:
        h = "2F6B66"
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def render(data, out_path, accent="#2F6B66", brand_name="", footer_note=""):
    """data: {title, subtitle, at, headline:{value,label}, sections:[{title, items:{k:v}}], note}"""
    reg, bold = _pick(FONT_CANDIDATES), _pick(BOLD_CANDIDATES)
    if not reg:
        return {"ok": False, "err": "на устройстве не нашлось статического шрифта с кириллицей — "
                                    "PDF не собрать; поставьте DejaVu (Linux: fonts-dejavu-core)"}
    A = _hex(accent)
    INK, INK2, SILVER, LINE, CREAM = (26, 26, 26), (42, 42, 42), (140, 140, 140), (235, 232, 225), (245, 243, 238)

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font("body", "", reg)
    pdf.add_font("body", "B", bold or reg)
    pdf.add_page()
    pdf.set_margins(18, 16, 18)
    W = 210 - 36

    # ── Шапка: имя ВЛАДЕЛЬЦА (или пусто), дата. Наших знаков здесь нет ──
    pdf.set_font("body", "B", 11)
    pdf.set_text_color(*INK)
    if brand_name:
        pdf.cell(W * 0.6, 6, brand_name)
    pdf.set_font("body", "", 9)
    pdf.set_text_color(*SILVER)
    pdf.cell(W * (0.4 if brand_name else 1.0), 6, data.get("at", ""), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_draw_color(*A)
    pdf.set_line_width(0.7)
    y = pdf.get_y()
    pdf.line(18, y, 210 - 18, y)
    pdf.ln(6)

    # ── Заголовок документа ──
    pdf.set_font("body", "B", 19)
    pdf.set_text_color(*INK)
    pdf.multi_cell(W, 8.5, data.get("title", "Отчёт"), align="L")   # без выключки: растянутый заголовок читается как вёрстка 90-х
    if data.get("subtitle"):
        pdf.ln(1)
        pdf.set_font("body", "", 10)
        pdf.set_text_color(*INK2)
        pdf.multi_cell(W, 5.2, data["subtitle"], align="L")
    pdf.ln(4)

    # ── Главное число: одно, крупно ──
    h = data.get("headline") or {}
    if h.get("value") is not None:
        y0 = pdf.get_y()
        pdf.set_fill_color(*CREAM)
        pdf.rect(18, y0, W, 24, "F")
        pdf.set_xy(24, y0 + 4)
        pdf.set_font("body", "B", 26)
        pdf.set_text_color(*A)
        vw = pdf.get_string_width(str(h["value"])) + 4
        pdf.cell(vw, 14, str(h["value"]))
        pdf.set_font("body", "", 10)
        pdf.set_text_color(*INK2)
        _lbl = [ln for ln in str(h.get("label", "")).split("\n") if ln.strip()]
        _lx, _ly = pdf.get_x(), pdf.get_y()
        for _i, _ln in enumerate(_lbl[:2]):        # cell() не переносит строки — рисуем построчно
            pdf.set_xy(_lx, _ly + 3.2 + _i * 4.4)
            pdf.cell(60, 5, _ln)
        if data.get("note"):
            pdf.set_xy(18 + W - 78, y0 + 6)
            pdf.set_font("body", "", 8.5)
            pdf.set_text_color(*SILVER)
            pdf.multi_cell(72, 4, data["note"], align="R")
        pdf.set_y(y0 + 24)
        pdf.ln(6)

    # ── Разрезы. Шкала ОБЩАЯ для всех блоков: иначе полоса одной длины
    #    означает в разных разделах разное — читатель обманывается. ──
    allv = [v for s in (data.get("sections") or []) for v in (s.get("items") or {}).values()
            if isinstance(v, (int, float))]
    gmax = max(allv) if allv else 1
    for s in (data.get("sections") or []):
        items = s.get("items") or {}
        if not items:
            continue
        pdf.set_font("body", "B", 8)
        pdf.set_text_color(*SILVER)
        pdf.cell(W, 5, str(s.get("title", "")).upper(), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        for k, v in sorted(items.items(), key=lambda x: -x[1] if isinstance(x[1], (int, float)) else 0):
            yb = pdf.get_y()
            pdf.set_font("body", "", 9.5)
            pdf.set_text_color(*INK)
            pdf.cell(52, 6, str(k)[:34])
            bx, bw = 18 + 52, W - 52 - 14
            pdf.set_fill_color(*LINE)
            pdf.rect(bx, yb + 2.2, bw, 2.2, "F")
            frac = (float(v) / gmax) if gmax else 0
            pdf.set_fill_color(*A)
            pdf.rect(bx, yb + 2.2, max(1.5, bw * frac), 2.2, "F")
            pdf.set_xy(18 + W - 14, yb)
            pdf.set_font("body", "B", 9.5)
            pdf.set_text_color(*INK2)
            pdf.cell(14, 6, str(v), align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_draw_color(*LINE)
            pdf.set_line_width(0.15)
            pdf.line(18, pdf.get_y(), 210 - 18, pdf.get_y())
        pdf.ln(5)

    if allv and len(data.get("sections") or []) > 1:
        pdf.set_font("body", "", 7.5)
        pdf.set_text_color(*SILVER)
        pdf.cell(W, 4, "Длина полосы сопоставима между разделами: общая шкала, максимум — %s" % gmax,
                 new_x="LMARGIN", new_y="NEXT")

    # ── Подвал: только то, что нужно клиенту. Ни слова про нас. ──
    if footer_note:
        # auto_page_break съедал подвал: рисуем по абсолютным координатам, отключив разрыв
        pdf.set_auto_page_break(False)
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.15)
        pdf.line(18, 277, 210 - 18, 277)
        pdf.set_xy(18, 279)
        pdf.set_font("body", "", 8)
        pdf.set_text_color(*SILVER)
        pdf.cell(W, 5, footer_note)

    pdf.output(out_path)
    return {"ok": True, "path": out_path, "bytes": os.path.getsize(out_path),
            "font": os.path.basename(reg)}
