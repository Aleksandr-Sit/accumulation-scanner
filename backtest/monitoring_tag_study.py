"""Метка Binance «Monitoring Tag» (кандидаты на делистинг): запрет входа и сигнал выхода (Q12).

Повод (16.10.2026): Binance поставил метку на BICO 09.10.2026, пробный исполнитель купил BICO
10.10.2026 (на Bybit stTag 0 — исполнитель знает только метку ST Bybit и статус пары Bybit).
Вопросы: что бывает с монетой после метки; стоило ли не покупать монеты с меткой и продавать
открытые позиции, когда метка появляется.

Источник событий — публичный CMS Binance без ключа (кэш ответов .cache/binance_cms/,
<каталог>_<страница>.json — снимок на день прогона; страницы сдвигаются с новыми статьями,
для свежего снимка кэш удалить):
  .../bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=<c>&pageNo=<p>&pageSize=50
Каталоги: 49 «Latest Binance News» (метки «Binance Will Extend the Monitoring Tag to Include …,
Remove the Monitoring Tag for … on DATE»), 161 «Delisting» («Binance Will Delist A, B on DATE»;
одна метка — 18.06.2026 ACT, BLUR, PIVX, QKC — лежит здесь), 48 / 93 / 157 — проверены на
метки в заголовках. Поиск по всем каталогам (article/all/query?queryKeywords=…) работает, но
отдаёт не больше 50 последних статей — для истории непригоден, поэтому обходятся каталоги.
Метка — с даты в заголовке («on DATE»); снятие метки — «Remove the Monitoring Tag for …»;
вводная статья 26.07.2023 (26 монет) — список берётся из текста статьи (article/detail/query),
окно замера — с 26.07.2023. Тикер → пара XXXUSDT архива Binance (ALIASES — переименования).
Делистинг: дата анонса (releaseDate статьи), дата в заголовке и фактическая последняя свеча
пары в архиве (.cache/binance_1d_full, до ~28.09.2026).

Что считаем:
  1. События: монета → метка (и снятие) → анонс делистинга → последняя свеча.
  2. Цена после метки: вход по закрытию дня метки, комиссия 0.15% × 2, доходность через
     7/30/90/180/365 дн. (делистнута раньше — последнее закрытие), против индекса альтов
     (portfolio_vs_market.market_index) на тех же окнах; падение в день анонса делистинга
     (закрытие дня анонса к закрытию накануне и к закрытию через 3 дн.); «похожие немеченые» —
     монеты архива того же места по 30-дн. обороту Binance (ранг ±20%) в тот же день, без
     метки на этот день, медиана по ним — пара к каждому событию.
  3. Запрет входа: сигналы прода (модель исполнителя, шаг s6 portfolio_vs_market.exec_book:
     15 мест, лестница 5 × $10, кулдаун 90 дн., USDT $1500, фильтры прода, J.SEEDS случайных
     порядков), у которых на день сигнала метка уже стоит и не снята, не берутся. Сверка: база
     повторяет s6 (R +$1503 / H +$1903). Вся история и окно 2023-10…2026-09 (позиции по дню
     входа; до 10.2023 гейт ничего не меняет).
  4. Выход по метке: открытая на день метки позиция продаётся целиком по закрытию следующего
     дня (метка — днём, исполнитель увидит её в следующий прогон), комиссия рыночная; ряд
     обрезается этим днём — simulate_exec закрывает остаток как «делистинг» по закрытию,
     снимает лимитки. Сравнение с правилами R/H: по затронутым позициям и в портфеле
     (тот же exec_book, освободившиеся места и деньги — следующим сигналам). И гейт + выход.
  5. Справочно: торговалась ли монета на Bybit spot после делистинга Binance (публичный
     v5/market/kline, кэш .cache/binance_cms/bybit_<SYM>.json).

Оговорки: событий мало (с 10.2023 ~110 монет с меткой, позиций модели с меткой — единицы),
окно — один цикл; дневные свечи; анонс делистинга обычно идёт после метки, но не всегда
(44% спот-делистингов с 10.2023 шли после метки); архив Binance заканчивается ~28.09.2026 —
метки 09.10.2026 (BICO, CVC) в цену не попадают; выход «по закрытию +1 день» — оценка.

Запуск из корня проекта:  PYTHONIOENCODING=utf-8 python backtest/monitoring_tag_study.py
Время прогона: ≈ 4 мин (16.10.2026: 237 с с кэшем CMS; первый обход CMS без кэша — ещё ≈ 1.5–3 мин).
Результат: backtest/monitoring_tag_results.json
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics as st
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402

OUT = Path(__file__).resolve().parent / "monitoring_tag_results.json"
CACHE = ROOT / ".cache" / "binance_cms"
URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
       "?type=1&catalogId={c}&pageNo={p}&pageSize=50")
BYBIT = "https://api.bybit.com/v5/market/kline?category=spot&symbol={s}&interval=D&limit=1000&end={e}"
U = dt.timezone.utc
DAY = 86400
FEE = 0.0015
SINCE = dt.date(2023, 7, 1)          # метки появились 26.07.2023
WIN0 = int(dt.datetime(2023, 7, 26, tzinfo=U).timestamp())   # первая метка (вводная статья)
HORIZONS = (7, 30, 90, 180, 365)
PEER_TOL = 0.20                      # «похожие»: ранг оборота ±20%
# Переименования: тикер в анонсе -> пары архива (по порядку предпочтения).
ALIASES = {"VELODROME": ["VELODROMEUSDT"], "D": ["DUSDT"], "A2Z": ["A2ZUSDT"]}


# ---------------------------------------------------------------- анонсы

def get_json(url: str, tries: int = 6) -> dict:
    """GET с паузой при 429 (CMS Binance режет частые запросы)."""
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.loads(urllib.request.urlopen(req, timeout=60).read())
        except urllib.error.HTTPError as e:
            if e.code != 429 or k == tries - 1:
                raise
            time.sleep(20 * (k + 1))
    raise RuntimeError(url)


def page(c: int, p: int) -> list[dict]:
    f = CACHE / f"{c}_{p}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    d = get_json(URL.format(c=c, p=p))
    cats = d["data"]["catalogs"]
    arts = [{"t": a["title"], "d": a["releaseDate"], "code": a.get("code"), "cat": c}
            for a in (cats[0]["articles"] if cats else [])]
    f.write_text(json.dumps(arts, ensure_ascii=False), encoding="utf-8")
    time.sleep(1.5)
    return arts


def crawl(c: int, stop_before: dt.date) -> list[dict]:
    out, p = [], 1
    while True:
        arts = page(c, p)
        if not arts:
            break
        out += arts
        if dt.datetime.fromtimestamp(arts[-1]["d"] / 1000, U).date() < stop_before:
            break
        p += 1
    return out


def rel_day(a: dict) -> dt.date:
    return dt.datetime.fromtimestamp(a["d"] / 1000, U).date()


def tickers(s: str) -> list[str]:
    s = re.sub(r"\([^)]*\)", "", s)
    parts = re.split(r",\s*|\s+&\s+|\s+and\s+", s)
    out = []
    for x in parts:
        x = x.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{1,15}", x):
            out.append(x)
    return out


END = r"(?=,?\s+(?:and\s+)?(?:Remove|the Seed|Extend)|\s+on\s+\d{4}-\d{2}-\d{2}|$)"


def parse_tag(title: str) -> tuple[list[str], list[str], dt.date | None]:
    """-> (кому ставят метку, с кого снимают, дата из заголовка)."""
    add, rem = [], []
    m = re.search(r"Extend the Monitoring Tag to (?:Include )?(.+?)" + END, title)
    if m:
        add = tickers(m.group(1))
    m = re.search(r"Remove the Monitoring Tag for (.+?)" + END, title)
    if m:
        rem = tickers(m.group(1))
    m = re.search(r"\bon (\d{4}-\d{2}-\d{2})", title)
    return add, rem, (dt.date.fromisoformat(m.group(1)) if m else None)


def parse_delist(title: str) -> tuple[list[str], dt.date | None]:
    # только спот: «Binance Will Delist …» / «Binance Announced … and Will Delist …»; маржа,
    # займы, Alpha, фьючерсы, опционы, пары — не делистинг монеты
    if not re.match(r"Binance (Will Delist|Announced .+ Will Delist)", title) or \
            re.search(r"Options|Leveraged|Trading Pair", title):
        return [], None
    m = re.search(r"Will Delist (.+?) on (\d{4}-\d{2}-\d{2})", title)
    if not m:
        m2 = re.search(r"Will Delist ([A-Z0-9]{1,15}) and Support", title)
        return (tickers(m2.group(1)), None) if m2 else ([], None)
    return tickers(m.group(1)), dt.date.fromisoformat(m.group(2))


DETAIL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode={c}"


def intro_tickers(code: str) -> list[str]:
    """Монеты, получившие метку 26.07.2023: «(TICKER)» между «following tokens» и «periodic»."""
    f = CACHE / f"article_{code}.json"
    if not f.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(get_json(DETAIL.format(c=code)), ensure_ascii=False),
                     encoding="utf-8")
    body = json.loads(f.read_text(encoding="utf-8"))["data"]["body"]
    text = " ".join(re.findall(r'"text":"([^"]*)"', body))
    m = re.search(r"applied to the following tokens(.+?)Binance will conduct", text)
    return re.findall(r"\(([A-Z0-9]{1,15})\)", m.group(1)) if m else []


def load_events() -> dict:
    news = crawl(49, dt.date(2022, 12, 20))
    other = {c: crawl(c, SINCE) for c in (48, 93, 157)}
    delist = crawl(161, dt.date(2000, 1, 1))
    pool = news + delist + [a for v in other.values() for a in v]
    tag_arts = sorted({(a["d"], a["t"]): a for a in pool if "Monitoring Tag" in a["t"]}.values(),
                      key=lambda a: a["d"])
    events: dict[str, dict] = {}
    log = []
    intro = next((a for a in news if a["t"].startswith("Introducing Seed Tags & Monitoring Tags")),
                 None)
    if intro:                        # первый список меток — только в тексте вводной статьи
        d0 = rel_day(intro)
        first = intro_tickers(intro["code"])
        log.append({"day": d0.isoformat(), "cat": 49, "add": first, "remove": [],
                    "title": intro["t"] + " (список из текста статьи)"})
        for t in first:
            events.setdefault(t, {"tags": [], "removes": []})["tags"].append(d0)
    for a in tag_arts:
        if a is intro:
            continue
        add, rem, d = parse_tag(a["t"])
        d = d or rel_day(a)
        log.append({"day": d.isoformat(), "cat": a["cat"], "add": add, "remove": rem,
                    "title": a["t"]})
        for t in add:
            ev = events.setdefault(t, {"tags": [], "removes": []})
            ev["tags"].append(d)
        for t in rem:
            ev = events.setdefault(t, {"tags": [], "removes": []})
            ev["removes"].append(d)
    dl: dict[str, dict] = {}
    for a in sorted(delist, key=lambda a: a["d"]):
        tk, eff = parse_delist(a["t"])
        for t in tk:
            dl.setdefault(t, {"ann": rel_day(a), "eff": eff, "title": a["t"]})
    return {"events": events, "delist": dl, "log": log,
            "counts": {"news": len(news), "delist_arts": len(delist),
                       **{f"cat{c}": len(v) for c, v in other.items()},
                       "tag_titles": len(tag_arts),
                       "tag_titles_by_cat": {str(c): sum(1 for a in tag_arts if a["cat"] == c)
                                             for c in sorted({a["cat"] for a in tag_arts})}}}


def tag_intervals(ev: dict) -> list[tuple[dt.date, dt.date | None]]:
    """[(с, по)] — интервалы, когда метка стоит (по — день снятия, None — не снята)."""
    marks = sorted([(d, 1) for d in ev["tags"]] + [(d, 0) for d in ev["removes"]])
    out, cur = [], None
    for d, kind in marks:
        if kind and cur is None:
            cur = d
        elif not kind and cur is not None:
            out.append((cur, d))
            cur = None
    if cur is not None:
        out.append((cur, None))
    return out


def ts_of(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=U).timestamp())


def date_of(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, U).date().isoformat()




# ---------------------------------------------------------------- события ↔ архив

def coin_of(sym: str) -> str:
    return sym.split("#")[0][:-4]


def seg_at(segs_by_coin: dict, coin: str, ts: int) -> tuple[dict, int] | None:
    """Отрезок пары монеты, в котором есть день ts (или ближайший следующий в 5 дн.)."""
    for t in ALIASES.get(coin, [coin + "USDT"]):
        for s in segs_by_coin.get(t[:-4], []):
            if s["ts"][0] <= ts <= s["ts"][-1]:
                for k in range(6):
                    i = s["idx"].get(ts + k * DAY)
                    if i is not None:
                        return s, i
    return None


def is_tagged(iv: list, day: dt.date) -> bool:
    return any(a <= day and (b is None or day < b) for a, b in iv)


def net(c0: float, c1: float) -> float:
    return c1 / c0 * (1 - FEE) ** 2 - 1


def fwd(s: dict, i: int, h: int) -> tuple[float, int, bool]:
    """Доходность от закрытия i через h дн. (оборвался раньше — по последнему закрытию)."""
    j = s["idx"].get(s["ts"][i] + h * DAY)
    cut = j is None
    if cut:
        j = max(k for k in range(i, len(s["ts"])) if s["ts"][k] <= s["ts"][i] + h * DAY)
    return net(s["c"][i], s["c"][j]), j, cut and not s["alive"]


def qstats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    return {"n": len(xs), "med": st.median(xs), "mean": st.fmean(xs),
            "le_m50": sum(1 for x in xs if x <= -0.5) / len(xs),
            "p25": xs[len(xs) // 4], "p75": xs[3 * len(xs) // 4],
            "pos": sum(1 for x in xs if x > 0) / len(xs)}


def build_table(E: dict, segs: list[dict]) -> list[dict]:
    by_coin: dict[str, list[dict]] = {}
    for s in segs:
        by_coin.setdefault(coin_of(s["sym"]), []).append(s)
    rows = []
    for coin, ev in sorted(E["events"].items()):
        iv = tag_intervals(ev)
        if not iv:
            continue
        t0 = iv[0][0]
        dl = E["delist"].get(coin)
        hit = seg_at(by_coin, coin, ts_of(t0))
        row = {"coin": coin, "tag": t0.isoformat(),
               "tags": [(a.isoformat(), b.isoformat() if b else None) for a, b in iv],
               "removed": any(b is not None for _a, b in iv),
               "delist_ann": dl["ann"].isoformat() if dl else None,
               "delist_eff": dl["eff"].isoformat() if dl and dl["eff"] else None,
               "delist_before_tag": bool(dl and dl["ann"] < t0),
               "pair": None, "last_candle": None}
        if hit:
            s, i = hit
            row["pair"] = s["sym"]
            row["_seg"], row["_i"] = s, i
            row["last_candle"] = None if s["alive"] else date_of(s["ts"][-1])
        rows.append(row)
    return rows


def price_after(rows: list[dict], alt: dict, ranks: dict, segs: list[dict], tagged_on) -> dict:
    """П.2: доходность после метки против альтов и «похожих немеченых» (ранг оборота ±20%)."""
    by_day: dict[int, list[tuple[str, int]]] = {}
    for (sym, day), r in ranks.items():
        by_day.setdefault(day, []).append((sym, r))
    seg_by_sym = {s["sym"]: s for s in segs}
    alt_end = max(alt)
    out: dict = {"h": {}, "delist_day": {}}
    per = {h: {"coin": [], "alt": [], "ex_alt": [], "peer": [], "ex_peer": [], "peer_all": [],
               "cut": 0} for h in HORIZONS}
    for row in rows:
        if "_seg" not in row:
            continue
        s, i = row["_seg"], row["_i"]
        d0 = s["ts"][i]
        rk = ranks.get((s["sym"], d0))
        row["rank"] = rk
        peers = []
        if rk:
            lo, hi = rk * (1 - PEER_TOL), rk * (1 + PEER_TOL)
            for sym, r in by_day.get(d0, []):
                if sym != s["sym"] and lo <= r <= hi and not tagged_on(coin_of(sym), d0):
                    ps = seg_by_sym.get(sym)
                    if ps and d0 in ps["idx"]:
                        peers.append(ps)
        row["peers"] = len(peers)
        row["ret"] = {}
        for h in HORIZONS:
            end = d0 + h * DAY
            if end > alt_end or (s["alive"] and end > s["ts"][-1]):
                continue                          # окно ещё не закончилось
            r, j, dead = fwd(s, i, h)
            a0, a1 = P.at(alt, d0), P.at(alt, s["ts"][j])
            if not a0 or not a1:
                continue
            ra = net(a0, a1)
            pr = [fwd(ps, ps["idx"][d0], h)[0] for ps in peers
                  if not (ps["alive"] and end > ps["ts"][-1])]
            row["ret"][h] = {"coin": r, "alt": ra, "dead": dead,
                             "peer": st.median(pr) if pr else None}
            pp = per[h]
            pp["coin"].append(r)
            pp["alt"].append(ra)
            pp["ex_alt"].append(r - ra)
            pp["cut"] += dead
            if pr:
                pp["peer"].append(st.median(pr))
                pp["ex_peer"].append(r - st.median(pr))
                pp["peer_all"].extend(pr)
        # падение в день анонса делистинга
        if row["delist_ann"] and not row["delist_before_tag"]:
            da = ts_of(dt.date.fromisoformat(row["delist_ann"]))
            k = s["idx"].get(da)
            if k and k >= 1:
                row["ann_day"] = s["c"][k] / s["c"][k - 1] - 1
                if k + 3 < len(s["c"]):
                    row["ann_3d"] = s["c"][k + 3] / s["c"][k - 1] - 1
                row["tag_to_ann"] = s["c"][k - 1] / s["c"][i] - 1
                if row["last_candle"]:
                    row["ann_to_last"] = s["c"][-1] / s["c"][k - 1] - 1
    for h, pp in per.items():
        out["h"][h] = {"n": len(pp["coin"]), "coin": qstats(pp["coin"]), "alt": qstats(pp["alt"]),
                       "ex_alt": qstats(pp["ex_alt"]), "peer_med": qstats(pp["peer"]),
                       "ex_peer": qstats(pp["ex_peer"]), "peer_all": qstats(pp["peer_all"]),
                       "delisted_inside": pp["cut"]}
    for k in ("ann_day", "ann_3d", "tag_to_ann", "ann_to_last"):
        out["delist_day"][k] = qstats([r[k] for r in rows if k in r])
    return out


# ---------------------------------------------------------------- книги

def truncated(seg: dict, k: int) -> dict:
    """Ряд, обрезанный днём k включительно: simulate_exec закроет остаток по закрытию k
    («делистинг», рыночная комиссия) и снимет лимитки."""
    s = {key: seg[key][:k + 1] for key in ("ts", "o", "h", "l", "c", "qv")}
    s.update(sym=seg["sym"], alive=False, swap_end=False)
    return s


def trigger_day(seg: dict, e: int, iv: list) -> int | None:
    """Первый день после входа (≥ e+1), когда метка стоит -> индекс дня продажи (+1 день)."""
    ts = seg["ts"]
    for a, b in iv:
        a_ts, b_ts = ts_of(a), (ts_of(b) if b else None)
        for k in range(e + 1, len(ts)):
            if ts[k] < a_ts:
                continue
            if b_ts is not None and ts[k] >= b_ts:
                break
            return k + 1 if k + 1 < len(ts) else None
    return None


def run_books(src, alt, btc, intervals, ref) -> dict:
    out: dict = {}
    base_take, illiq = P.EXEC_POLICY

    def tag_on(ep):
        iv = intervals.get(coin_of(ep["seg"]["sym"]))
        return bool(iv) and is_tagged(iv, dt.datetime.fromtimestamp(ep["day"], U).date())

    gate_take = lambda ep: base_take(ep) and not tag_on(ep)  # noqa: E731
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}
        cache_x: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]

        def sim_x(ep, sell=sell, cache_x=cache_x, sim=sim):
            k = (ep["seg"]["sym"], ep["e"])
            if k in cache_x:
                return cache_x[k]
            r = sim(ep)
            iv = intervals.get(coin_of(ep["seg"]["sym"]))
            kd = trigger_day(ep["seg"], ep["e"], iv) if (r and iv) else None
            if r is None or kd is None or r["exit_d"] <= kd:
                cache_x[k] = r
            else:
                rx = L.simulate_exec(truncated(ep["seg"], kd), ep["e"], sell, budget=J.BUDGET,
                                     horizon=None, rules=L.EXEC_ALL)
                cache_x[k] = dict(rx, how="метка", base_pnl=r["pnl"], base_how=r["how"],
                                  sold=date_of(ep["seg"]["ts"][kd]))
            return cache_x[k]

        variants = {"base": (sim, base_take), "gate": (sim, gate_take),
                    "exit": (sim_x, base_take), "gate_exit": (sim_x, gate_take)}
        ob: dict = {}
        base_seeds: list = []
        for vk, (sf, take) in variants.items():
            taken = P.exec_book(src, sf, take, illiq, cooldown=True, cash=True)
            m = P.evaluate_flows(taken, alt, btc)
            if vk == "base":
                s6 = ref[b]["s6"]
                assert m["n"] == s6["n"] and abs(m["pnl"] - s6["pnl"]) < 1e-6 and \
                    abs(m["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({m['pnl']})"
            win = [(ep, r) for ep, r in taken if ep["day"] >= WIN0]
            mw = P.evaluate_flows(win, alt, btc)
            m = {k: m[k] for k in ("n", "spent", "pnl", "alt", "btc", "hows")}
            m["win"] = {k: mw[k] for k in ("n", "spent", "pnl", "alt", "btc", "hows")} if mw else {}
            m["died2y"] = sum(1 for ep, _r in taken if ep["died2y"])
            m["died2y_win"] = sum(1 for ep, _r in win if ep["died2y"])
            m["delist"] = sum(1 for _ep, r in taken if r["how"] == "делистинг"
                              or r.get("base_how") == "делистинг")
            m["tagged_entry"] = [f"{coin_of(ep['seg']['sym'])} {date_of(ep['day'])} {r['pnl']:+.1f}$"
                                 f" ({r.get('base_how', r['how'])})"
                                 for ep, r in taken if tag_on(ep)]
            m["tag_exits"] = [{"coin": coin_of(ep["seg"]["sym"]), "entry": date_of(ep["day"]),
                               "sold": r["sold"], "pnl": r["pnl"], "base_pnl": r["base_pnl"],
                               "base_how": r["base_how"], "spent": r["spent"],
                               "tag_at_entry": tag_on(ep)}
                              for ep, r in taken if r.get("how") == "метка"]
            runs = []
            for sd in range(J.SEEDS):
                tk = P.exec_book(src, sf, take, illiq, cooldown=True, cash=True,
                                 order="random", seed=sd)
                a = P.evaluate_flows(tk, alt, btc)
                w = P.evaluate_flows([(ep, r) for ep, r in tk if ep["day"] >= WIN0], alt, btc)
                runs.append({"pnl": a["pnl"], "alt": a["alt"], "btc": a["btc"], "spent": a["spent"],
                             "died": sum(1 for ep, _r in tk if ep["died2y"]),
                             "w_pnl": w["pnl"] if w else 0.0, "w_alt": w["alt"] if w else 0.0,
                             "w_spent": w["spent"] if w else 0.0,
                             "tag_exits": sum(1 for _ep, r in tk if r.get("how") == "метка"),
                             "tag_entries": sum(1 for ep, _r in tk if tag_on(ep))})
            if vk == "base":
                base_seeds = runs
            z = list(zip(runs, base_seeds))
            m["random"] = {
                "pnl_mean": st.fmean(x["pnl"] for x in runs),
                "ex_alt_mean": st.fmean(x["pnl"] - x["alt"] for x in runs),
                "ex_btc_mean": st.fmean(x["pnl"] - x["btc"] for x in runs),
                "spent_mean": st.fmean(x["spent"] for x in runs),
                "died_mean": st.fmean(x["died"] for x in runs),
                "w_pnl_mean": st.fmean(x["w_pnl"] for x in runs),
                "w_ex_alt_mean": st.fmean(x["w_pnl"] - x["w_alt"] for x in runs),
                "tag_exits_mean": st.fmean(x["tag_exits"] for x in runs),
                "tag_entries_mean": st.fmean(x["tag_entries"] for x in runs),
                "beat_alt_runs": sum(1 for x in runs if x["pnl"] > x["alt"]),
                "better_pnl": sum(1 for x, y in z if x["pnl"] > y["pnl"] + 1e-6),
                "equal_pnl": sum(1 for x, y in z if abs(x["pnl"] - y["pnl"]) <= 1e-6),
                "better_ex_alt": sum(1 for x, y in z if x["pnl"] - x["alt"] > y["pnl"] - y["alt"] + 1e-6),
                "w_better_ex_alt": sum(1 for x, y in z if x["w_pnl"] - x["w_alt"] >
                                       y["w_pnl"] - y["w_alt"] + 1e-6),
                "d_pnl_mean": st.fmean(x["pnl"] - y["pnl"] for x, y in z),
                "d_ex_alt_mean": st.fmean((x["pnl"] - x["alt"]) - (y["pnl"] - y["alt"]) for x, y in z),
                "d_pnl_min": min(x["pnl"] - y["pnl"] for x, y in z),
                "d_pnl_max": max(x["pnl"] - y["pnl"] for x, y in z)}
            ob[vk] = m
        out[b] = ob
    return out


def signals_tagged(src, intervals) -> dict:
    """П.3: сигналы прода (после фильтров) с меткой на день сигнала и их исход в R/H —
    отдельно от книги (каждый сигнал как будто взят)."""
    base_take = P.EXEC_POLICY[0]
    sig = [ep for ep in src if base_take(ep) and ep["day"] >= WIN0]
    hit, rest = [], []
    for ep in sig:
        iv = intervals.get(coin_of(ep["seg"]["sym"]))
        (hit if iv and is_tagged(iv, dt.datetime.fromtimestamp(ep["day"], U).date())
         else rest).append(ep)
    res: dict = {"signals_window": len(sig), "tagged": len(hit),
                 "coins": sorted({coin_of(ep["seg"]["sym"]) for ep in hit})}
    for b, (sell, _s) in J.BOOKS.items():
        for name, grp in (("tagged", hit), ("untagged", rest)):
            rr = []
            for ep in grp:
                r = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET, horizon=None,
                                    rules=L.EXEC_ALL)
                if r and r["spent"] > 0:
                    rr.append(r)
            res[f"{b}_{name}"] = {
                "n": len(rr), "rod": qstats([r["rod"] for r in rr]),
                "pnl_sum": sum(r["pnl"] for r in rr), "spent_sum": sum(r["spent"] for r in rr),
                "delist_share": sum(1 for r in rr if r["how"] == "делистинг") / max(1, len(rr)),
                "hows": {h: sum(1 for r in rr if r["how"] == h) for h in sorted({r["how"] for r in rr})}}
    return res


def bybit_after(rows: list[dict], n: int = 20) -> list[dict]:
    """П.5: торговалась ли пара XXXUSDT на Bybit spot после последней свечи Binance."""
    out = []
    for r in rows[:n]:
        sym = r["pair"].split("#")[0]
        f = CACHE / f"bybit_{sym}.json"
        if not f.exists():
            try:
                d = get_json(BYBIT.format(s=sym, e=int(time.time() * 1000)))
            except Exception as e:  # noqa: BLE001
                d = {"err": str(e)}
            f.write_text(json.dumps(d), encoding="utf-8")
            time.sleep(0.3)
        d = json.loads(f.read_text(encoding="utf-8"))
        lst = (d.get("result") or {}).get("list") or []
        bars = sorted((int(x[0]) // 1000, float(x[1]), float(x[4])) for x in lst)
        last_bn = ts_of(dt.date.fromisoformat(r["last_candle"]))
        after = [x for x in bars if x[0] > last_bn]
        out.append({"coin": r["coin"], "binance_last": r["last_candle"],
                    "bybit_first": date_of(bars[0][0]) if bars else None,
                    "bybit_last": date_of(bars[-1][0]) if bars else None,
                    "bybit_days_after": len(after), "retCode": d.get("retCode"),
                    "ret_after": after[-1][2] / after[0][1] - 1 if after else None})
    return out


def pct(x) -> str:
    return "—" if x is None else f"{x * 100:+.0f}%"


def main() -> int:
    t0 = time.time()
    E = load_events()
    print("Анонсы CMS:", json.dumps(E["counts"], ensure_ascii=False))
    print("снятия метки: " + "; ".join(f"{x['day']} {', '.join(x['remove'])}"
                                       for x in E["log"] if x["remove"]))
    coins = L.load_archive()
    all_segs = L.segments(coins)
    for s in all_segs:
        s["idx"] = {t: i for i, t in enumerate(s["ts"])}
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    alt, _ = P.market_index(ROOT / "scanner.db")
    intervals = {c: tag_intervals(ev) for c, ev in E["events"].items()}

    def tagged_on(coin: str, ts: int) -> bool:
        iv = intervals.get(coin)
        return bool(iv) and is_tagged(iv, dt.datetime.fromtimestamp(ts, U).date())

    rows = build_table(E, all_segs)
    ranks = L.vol_ranks(all_segs)
    matched = [r for r in rows if r["pair"]]
    print(f"монет с меткой {len(rows)}, сопоставлено с парой архива на день метки {len(matched)}; "
          f"нет: {', '.join(r['coin'] for r in rows if not r['pair'])}  [{time.time() - t0:.0f} с]")

    # ---- п.1: события
    arch_end = dt.datetime.fromtimestamp(last_day, U).date()
    dd = lambda a, b: (dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days  # noqa: E731
    old = [r for r in rows if (arch_end - dt.date.fromisoformat(r["tag"])).days >= 365]
    dl_after = [r for r in rows if r["delist_ann"] and not r["delist_before_tag"]]
    gaps = sorted(dd(r["delist_ann"], r["tag"]) for r in dl_after)
    gaps_last = sorted(dd(r["last_candle"], r["tag"]) for r in matched if r["last_candle"])
    gaps_eff = sorted(dd(r["last_candle"], r["delist_ann"]) for r in dl_after if r["last_candle"])
    first_tag = min(dt.date.fromisoformat(r["tag"]) for r in rows)
    spot_dl = {t: v for t, v in E["delist"].items() if v["ann"] >= first_tag}
    tagged_first = [t for t, v in spot_dl.items() if t in intervals
                    and any(a <= v["ann"] for a, _b in intervals[t])]
    ev = {"coins": len(rows), "matched": len(matched),
          "unmatched": [r["coin"] for r in rows if not r["pair"]],
          "removed": sum(r["removed"] for r in rows),
          "removed_coins": {r["coin"]: r["tags"] for r in rows if r["removed"]},
          "delisted_after_tag": len(dl_after), "share": len(dl_after) / len(rows),
          "gap_ann_med": st.median(gaps), "gap_ann_p25": gaps[len(gaps) // 4],
          "gap_ann_p75": gaps[3 * len(gaps) // 4],
          "gap_last_med": st.median(gaps_last) if gaps_last else None,
          "gap_ann_to_last_med": st.median(gaps_eff) if gaps_eff else None,
          "dead_in_archive": sum(1 for r in matched if r["last_candle"]),
          "old_1y": len(old),
          "old_1y_delisted_in_1y": sum(1 for r in old if r["delist_ann"] and not r["delist_before_tag"]
                                       and dd(r["delist_ann"], r["tag"]) <= 365),
          "spot_delist_since_first_tag": len(spot_dl), "of_them_tagged_before": len(tagged_first),
          "delisted_without_tag": sorted(t for t in spot_dl if t not in tagged_first)}
    print(f"\n1. События с {first_tag}: монет {ev['coins']}, с парой архива {ev['matched']} (нет: "
          f"{', '.join(ev['unmatched'])}); метку сняли у {ev['removed']} "
          f"({', '.join(ev['removed_coins'])}); анонс делистинга после метки — "
          f"{ev['delisted_after_tag']} ({ev['share']:.0%}); дней от метки до анонса: медиана "
          f"{ev['gap_ann_med']:.0f} (p25 {ev['gap_ann_p25']}, p75 {ev['gap_ann_p75']}); от метки до "
          f"последней свечи — медиана {ev['gap_last_med']}, от анонса до последней свечи — "
          f"{ev['gap_ann_to_last_med']}; меченых ≥ 1 года до конца архива {ev['old_1y']}, делистинг в "
          f"течение года у {ev['old_1y_delisted_in_1y']} "
          f"({ev['old_1y_delisted_in_1y'] / max(1, ev['old_1y']):.0%}); спот-делистингов с "
          f"{first_tag}: {ev['spot_delist_since_first_tag']}, из них метка до анонса "
          f"{ev['of_them_tagged_before']}")

    # ---- п.2: цена после метки
    pa = price_after(rows, alt, ranks, all_segs, tagged_on)
    print("\n2. Цена после метки (вход — закрытие дня метки, 0.15% × 2); медиана, среднее, доля ≤ −50%;"
          " «похожие» — медиана монет того же места по обороту ±20% без метки, по событиям:")
    print(f"  {'дн.':>4} {'n':>4} | {'монета':^21} | {'альты':^13} | {'монета−альты':^13} | "
          f"{'похожие':^21} | {'монета−похожие':^13} | делист.")
    for h, v in pa["h"].items():
        if not v["n"]:
            continue
        c, a, x, p, xp = v["coin"], v["alt"], v["ex_alt"], v["peer_med"], v["ex_peer"]
        print(f"  {h:>4} {v['n']:>4} | {pct(c['med']):>6} {pct(c['mean']):>6} {c['le_m50']:>6.0%} | "
              f"{pct(a['med']):>6} {pct(a['mean']):>6} | {pct(x['med']):>6} {pct(x['mean']):>6} | "
              f"{pct(p.get('med')):>6} {pct(p.get('mean')):>6} {p.get('le_m50', 0):>6.0%} | "
              f"{pct(xp.get('med')):>6} {pct(xp.get('mean')):>6} | {v['delisted_inside']:>4}")
    d2 = pa["delist_day"]
    n20 = sum(1 for r in rows if r.get("ann_day", 0) <= -0.2)
    print(f"  анонс делистинга (n {d2['ann_day']['n']}): день анонса к закрытию накануне — медиана "
          f"{pct(d2['ann_day'].get('med'))}, среднее {pct(d2['ann_day'].get('mean'))}, ≤ −20% у {n20}; "
          f"+3 дня — {pct(d2['ann_3d'].get('med'))}; от метки до кануна анонса — медиана "
          f"{pct(d2['tag_to_ann'].get('med'))}; от кануна анонса до последней свечи — "
          f"{pct(d2['ann_to_last'].get('med'))}")

    # ---- модель исполнителя
    npairs = P.pairs_per_day(ranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, ranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
    print(f"\nдней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")
    sg = signals_tagged(src, intervals)
    print(f"3. Сигналы прода после фильтров с {date_of(WIN0)}: {sg['signals_window']}, с меткой на день "
          f"сигнала {sg['tagged']} ({', '.join(sg['coins'])}); исход каждого сигнала отдельно (P&L на $):")
    for b in J.BOOKS:
        for name in ("tagged", "untagged"):
            v = sg[f"{b}_{name}"]
            print(f"  {b} {'с меткой' if name == 'tagged' else 'без метки':9} n {v['n']:>4}  мед. "
                  f"{pct(v['rod'].get('med')):>5}  ср. {pct(v['rod'].get('mean')):>5}  ≤−50% "
                  f"{v['rod'].get('le_m50', 0):>4.0%}  Σ {v['pnl_sum']:+.0f}$ на {v['spent_sum']:.0f}$  "
                  f"делистинг {v['delist_share']:.0%}  {v['hows']}")

    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    bk = run_books(src, alt, btc, intervals, ref)
    names = {"base": "база (прод)", "gate": "запрет входа", "exit": "выход по метке",
             "gate_exit": "запрет + выход"}
    for b, ob in bk.items():
        print(f"\n  === книга {b} (база = s6 ✓). По рангу: вся история | окно с {date_of(WIN0)}. "
              f"{J.SEEDS} случайных порядков: средние и «лучше базы того же порядка» (P&L / −альты / "
              f"−альты в окне; «=» — совпало)")
        print(f"  {'вариант':15} {'поз':>4} {'P&L':>6} {'−альты':>7} {'умер':>4} {'делист':>6} | "
              f"{'поз':>3} {'P&L':>5} {'−альты':>6} | {'P&L':>6} {'−альты':>6} {'ΔP&L':>6} {'Δ−альт':>6} "
              f"{'окно−альт':>9} {'умер':>5} {'лучше':>9} {'=':>3} {'ΔP&L мин…макс':>14}")
        for vk, m in ob.items():
            w, r = m["win"], m["random"]
            print(f"  {names[vk]:15} {m['n']:>4} {m['pnl']:>+6.0f} {m['pnl'] - m['alt']:>+7.0f} "
                  f"{m['died2y']:>4} {m['delist']:>6} | {w.get('n', 0):>3} {w.get('pnl', 0):>+5.0f} "
                  f"{w.get('pnl', 0) - w.get('alt', 0):>+6.0f} | {r['pnl_mean']:>+6.0f} "
                  f"{r['ex_alt_mean']:>+6.0f} {r['d_pnl_mean']:>+6.1f} {r['d_ex_alt_mean']:>+6.1f} "
                  f"{r['w_ex_alt_mean']:>+9.0f} {r['died_mean']:>5.1f} "
                  f"{r['better_pnl']:>2}/{r['better_ex_alt']}/{r['w_better_ex_alt']:<2}".ljust(10)
                  + f" {r['equal_pnl']:>3} {r['d_pnl_min']:+.0f}…{r['d_pnl_max']:+.0f}")
        print(f"    куплено с меткой на день сигнала (база, по рангу): "
              + ("; ".join(ob["base"]["tagged_entry"]) or "—")
              + f"; в среднем по порядкам {ob['base']['random']['tag_entries_mean']:.1f}")
        tx = ob["exit"]["tag_exits"]
        print(f"    выход по метке (по рангу): " + ("; ".join(
            f"{x['coin']} вход {x['entry']}{' (метка уже стояла)' if x['tag_at_entry'] else ''} продано "
            f"{x['sold']} {x['pnl']:+.1f}$ против правил {x['base_pnl']:+.1f}$ ({x['base_how']})"
            for x in tx) or "—") + f"; в среднем по порядкам {ob['exit']['random']['tag_exits_mean']:.1f}")
        if tx:
            print(f"    по затронутым позициям: выход {sum(x['pnl'] for x in tx):+.1f}$ против правил "
                  f"{sum(x['base_pnl'] for x in tx):+.1f}$ (вложено {sum(x['spent'] for x in tx):.0f}$)")
    print(f"  [{time.time() - t0:.0f} с]")

    # ---- справочно: Bybit после делистинга Binance
    dead = sorted([r for r in rows if r.get("last_candle") and r["pair"]],
                  key=lambda r: r["last_candle"], reverse=True)
    by = bybit_after(dead, 20)
    print("\n5. Bybit spot после последней свечи Binance (20 последних делистингов меченых): " + "; ".join(
        f"{x['coin']} BN до {x['binance_last']}, BB {x['bybit_first'] or '—'}…{x['bybit_last'] or '—'}"
        f" (+{x['bybit_days_after']} дн., {pct(x['ret_after'])})" for x in by))

    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    out = {"meta": {"run": dt.datetime.now(U).isoformat(timespec="seconds"),
                    "archive_last_day": arch_end.isoformat(), "fee": FEE, "seeds": J.SEEDS,
                    "window_from": date_of(WIN0), "peer_tol": PEER_TOL, "cms_counts": E["counts"],
                    "runtime_s": round(time.time() - t0)},
           "tag_log": E["log"], "events": ev, "coins": clean, "price_after": pa,
           "signals_tagged": sg, "books": bk, "bybit_after": by}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
