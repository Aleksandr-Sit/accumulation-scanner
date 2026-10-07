"""Картинка уровней для Telegram: свечи + ступени покупки, стоп, цели (уровни плана).

Профиля объёма и «поддержек/сопротивлений» нет намеренно: backtest/levels_study.py —
узлы объёма и структурные уровни не лучше placebo (docs/LEVELS_REPORT.md).
Pillow — НЕОБЯЗАТЕЛЬНАЯ зависимость: нет её (или шрифта) — render_levels() возвращает
None, и сообщение уходит текстом. Ядро сканера остаётся на stdlib. Внешние chart-API не
используются — уровни позиций не уходят третьей стороне.
"""
from __future__ import annotations

import io
import os

from .telegram import fmt_price

W, H = 1280, 720
PAD_L, PAD_R, PAD_T, PAD_B = 24, 260, 76, 40
BG, GRID, TXT, MUTED = (14, 22, 33), (34, 46, 60), (230, 236, 242), (130, 146, 160)
UP, DN, LINE = (72, 187, 120), (226, 85, 85), (150, 190, 240)
COLORS = {"buy": (64, 196, 120), "stop": (235, 87, 87), "target": (90, 160, 250),
          "entry": (170, 180, 190), "trail": (240, 170, 60)}

_FONTS = ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
          "/System/Library/Fonts/Supplemental/Arial.ttf")
_BOLD = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf")


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return any(os.path.exists(p) for p in _FONTS)


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for p in (_BOLD if bold else _FONTS) + _FONTS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return None                 # без TTF кириллица не отрисуется — картинку не делаем


LABEL_GAP = 24              # px между подписями уровней (шрифт 20)
LABEL_BOTTOM = H - 38       # центр нижней подписи — выше строки «уровни — правила сканера…»


def label_ys(ys: list[float], bottom: float, gap: float = LABEL_GAP) -> list[float]:
    """Высоты подписей уровней (ys — по возрастанию, у своих линий): не ближе gap друг к другу
    и не ниже bottom — иначе стопка сдвигается вверх целиком (5 ступеней + стоп у дна графика
    доходили до нижней строки и наезжали на неё)."""
    out: list[float] = []
    for y in ys:
        out.append(max(y, out[-1] + gap) if out else y)
    over = out[-1] - bottom if out else 0
    return [y - max(0, over) for y in out]


def render_levels(ohlcv: dict, *, title: str, subtitle: str = "", buys=(), stop=None,
                  targets=(), entry=None, extra=(), days: int = 180, fmt=None,
                  footer: str = "уровни — правила сканера, не рекомендация") -> bytes | None:
    """PNG (bytes) или None (нет Pillow/шрифта/истории).

    ohlcv — {o,h,l,c,(v|qv)} oldest→newest; без o/h/l — линия закрытий.
    buys — цены ступеней; stop — цена стопа; targets — [(цена, «+50%»)];
    entry — цена входа позиции; extra — [(цена, подпись, ключ цвета из COLORS)];
    fmt — формат цены в подписях (шаг цены пары), по умолчанию 4 значащие цифры."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    f_lab, f_title, f_sub = _font(20), _font(26, bold=True), _font(16)
    if f_lab is None:
        return None
    c = list((ohlcv or {}).get("c") or [])[-days:]
    if len(c) < 10:
        return None
    n = len(c)
    candles = all(len((ohlcv.get(k) or [])) >= n for k in ("o", "h", "l"))
    o = ohlcv["o"][-n:] if candles else c
    h = ohlcv["h"][-n:] if candles else c
    lo_ = ohlcv["l"][-n:] if candles else c

    F = fmt or fmt_price
    levels = ([(p, f"купить {i} · {F(p)}", "buy") for i, p in enumerate(buys, 1)]
              + ([(stop, f"стоп · {F(stop)}", "stop")] if stop else [])
              + [(p, f"цель {lab} · {F(p)}", "target") for p, lab in targets]
              + ([(entry, f"вход · {F(entry)}", "entry")] if entry else [])
              + [(p, lab, key) for p, lab, key in extra])
    levels = [lv for lv in levels if isinstance(lv[0], (int, float)) and lv[0] > 0]
    lo = min([min(lo_)] + [p for p, _, _ in levels]) * 0.96
    hi = max([max(h)] + [p for p, _, _ in levels]) * 1.03
    if hi <= lo:
        return None
    x0, x1, y0, y1 = PAD_L, W - PAD_R, PAD_T, H - PAD_B

    def Y(p: float) -> float:
        return y1 - (p - lo) / (hi - lo) * (y1 - y0)

    img = Image.new("RGB", (W, H), BG)
    g = ImageDraw.Draw(img)
    for k in range(6):
        y = Y(lo + (hi - lo) * k / 5)
        g.line([(x0, y), (x1, y)], fill=GRID, width=1)

    cw = (x1 - x0) / n
    if candles:
        for i in range(n):
            cx = x0 + (i + 0.5) * cw
            col = UP if c[i] >= o[i] else DN
            g.line([(cx, Y(h[i])), (cx, Y(lo_[i]))], fill=col, width=1)
            top, bot = Y(max(o[i], c[i])), Y(min(o[i], c[i]))
            g.rectangle([cx - cw * 0.35, top, cx + cw * 0.35, max(bot, top + 1)], fill=col)
    else:
        g.line([(x0 + (i + 0.5) * cw, Y(c[i])) for i in range(n)], fill=LINE, width=2)

    labels = []
    for p, lab, key in levels:
        y, col = Y(p), COLORS.get(key, TXT)
        if key in ("stop",):
            g.line([(x0, y), (x1, y)], fill=col, width=3)
        else:
            for xx in range(int(x0), int(x1), 14):
                g.line([(xx, y), (min(xx + 8, x1), y)], fill=col, width=2)
        labels.append([y, lab, col])
    labels.sort(key=lambda t: t[0])
    for t, y in zip(labels, label_ys([t[0] for t in labels], LABEL_BOTTOM)):
        t[0] = y
    # длинная цена («цель +150% · 0.00011036») не влезала в правое поле — шрифт меньше
    room = W - (x1 + 10) - 6
    for size in (18, 16):
        if max(g.textlength(lab, font=f_lab) for _, lab, _ in labels or [(0, "", 0)]) <= room:
            break
        f_lab = _font(size) or f_lab
    for y, lab, col in labels:
        g.text((x1 + 10, y), lab, fill=col, font=f_lab, anchor="lm")

    g.ellipse([x1 - 6, Y(c[-1]) - 6, x1 + 6, Y(c[-1]) + 6], outline=TXT, width=2)
    g.text((PAD_L, 24), title, fill=TXT, font=f_title, anchor="lm")
    if subtitle:
        g.text((PAD_L, 56), subtitle, fill=MUTED, font=f_sub, anchor="lm")
    if footer:
        g.text((W - 16, H - 16), footer, fill=MUTED, font=f_sub, anchor="rm")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
