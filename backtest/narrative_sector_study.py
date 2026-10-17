"""Нарративы рынка: можно ли вовремя опознать сектор-лидер и заработать на нём (Q16).

Вопрос владельца (17.10.2026): «можно ли выявлять нарративы рынка? Сейчас хайпуют ИИ-проекты,
ИИ-агенты для автоматизации торговли; кажется, если выбирать правильное направление в цикле,
можно попасть в правильный сектор». Проверяем на истории: опознаётся ли сектор-лидер ПО ДАННЫМ
НА ДАТУ (без заглядывания) и даёт ли его покупка доход после издержек сверх индекса альтов,
BTC и случайного выбора сектора.

ДАННЫЕ. Снимки CMC listings/historical на первое воскресенье месяца (.cache/cmc_hist_deep,
3000 мест, 2015-01…2026-10): id, место, цена, капа, теги. Цена — «способ B» Q6
(size_band_study.clean_series): цена снимка по id, одиночные выбросы (5× к обоим соседям)
заменены средним соседей; монета, выпавшая из 3000 / сменившая id, — по последней виденной
цене (0% за месяц, со следующего снимка её нет в составе; погибшие при этом в составе — без
«ошибки выживших» в самой вселенной); справочно «выпавшие навсегда = −100%» (GONE0).
Вселенная месяца — места 2–500 (проверка 2–200) без исключений Q4 (T.excluded: BTC, стейблы,
обёртки/стейкинг, токенизированное золото; сбои капы T.glitches + T.GLITCHES) и, сверх Q4,
без токенизированных акций/ETF (TOKSEC по имени: xStock, Ondo, bStocks, pre-IPO — с 2025-12
до 50 строк топ-500; иначе все они попадали бы в RWA и делали его индексом акций США). Сверх Q6 — две защиты от битых строк
CMC: (1) строки, где место по капе в 3+ раза выше места CMC и на 50+ мест (блок «нерейтингуемых»
сразу за 200-м местом: HEX, WBNB, TRIX с капой $33 млрд, DREP после свопа — $9.1 млрд на 201-м),
исключаются из состава (267 строк 2–500 с 2020-07, 0.8%); (2) скачок цены за месяц > ×5 при
обороте < 0.1% капы (самозаявленная цена: IZE, XT, TMG, BOTX) или > ×50 в несогласованную строку
(своп: DREP +26 692%) считается 0% — без этого один DREP давал индексу альтов равных весов +56%
за 2021-03. Реальные пампы с оборотом (AI16Z, ORC, CTC, VGX, MED, WRX) остаются.
Секторы — словарь Q14 (sector_cap_study.RULES / sector_of — импорт, не копия): 9 нарративных
секторов SECTORS (биржевые, мемы, AI, RWA, игры/NFT, L2, L1, DeFi, хранение/инфра) — их и
выбирают правила; «прочее» (теги есть, сектора нет: PoW, платёжные…) и «нет тега» входят в
индекс альтов, но не выбираются. Подсектор «AI-агенты» — теги AGENT_TAGS (ai-agents с 2024-11-03,
ai-agent-launchpad с 2025-01, virtuals-protocol-ecosystem с 2024-12, defai с 2025-02):
пересекается с AI и мемами, считается отдельной группой (C, F, вариант A «+агенты»).

ЛОВУШКА ТЕГОВ. CMC навешивает тег, когда нарратив уже в моде (VIRTUAL получил ai-agents
2024-11 при цене ×15 от 2024-07). Поэтому всё в двух вариантах:
  PIT  — сектор по тегам снимка на дату решения (честный; секторной таксономии нет до 2020-07,
         поэтому PIT с 2020-07-05; монеты без тегов — «нет тега»);
  LATE — сектор по ПОСЛЕДНЕМУ снимку, где у id есть теги (заглядывание — справочно, верхняя
         граница; монеты, умершие до 2020-07, тегов не имеют — LATE 2018–20 ещё и на выживших).
LATE сравнивается с PIT на том же окне (сигналы с тех же месяцев), LATE с 2018 — справочно.
Расхождение: совпадение составов PIT/LATE, у скольких монет AI-сектора 2026 был тег год/два
назад, когда тег появился относительно цены (пик и середина основного роста в окне ±12 мес.).

ИНДЕКС СЕКТОРА («фонд»): участники на начало месяца (снимок k), равные веса (ew) и по капе
(cw), доходность к снимку k+1; ребаланс каждый месяц, издержки 0.15% на оборот (FEE; стресс
0.5% FEE_HI) — внутренний оборот фонда Σ|w_k − w_дрейф| (смена состава + возврат к весам) и
оборот портфеля между фондами при смене выбора. Бенчмарки — те же фонды: индекс альтов (вся
вселенная, ew/cw), BTC (цена CMC id 1), случайный сектор (DRAWS = 1000 случайных выборов того же
числа секторов из тех же допустимых, та же схема когорт). Сектор допустим, если в нём ≥
MIN_MEMBERS монет и есть история сигнала.

ТЕСТЫ.
 A. Секторный моментум: каждый месяц t ранжировать допустимые секторы по прошлой доходности
    фонда к фонду альтов (той же схемы весов) за L = 1/3/6 мес., взять лучшие (и худшие —
    разворот) N = 1/2, держать H = 1/3/6 мес. ПЕРЕКРЫВАЮЩИМИСЯ КОГОРТАМИ (Jegadeesh–Titman):
    портфель месяца = среднее H последних когорт, раз в месяц возвращается к этим весам (оборот
    и комиссия считаются); до первого сигнала когорта = индекс альтов. Окно PIT: решения с
    2020-08 по 2026-09 (держание до 2026-10). Метрики: итог, к альтам (отношение NAV), к BTC,
    перцентиль среди 1000 случайных, доля месяцев лучше альтов, макс. просадка, по периодам
    2020-21 / 2022 / 2023-24 / 2025-26; сводка по 18 конфигурациям (множественная проверка).
 B. Ранние признаки «внимания» (PIT, места 2–500): (i) рост доли сектора в капе альтов за 1/3
    мес.; (ii) монет сектора, вошедших в топ-200 за 3 мес. (и на монету сектора в топ-200);
    (iii) доля сектора среди 20 лидеров роста месяца (и к доле сектора в числе монет);
    (iv) новых id в топ-3000 за 3 мес. с тегом сектора (и на монету сектора); справочно mom1/3.
    IC — Спирмен по секторам внутри месяца к доходности фонда сектора к альтам за следующие
    1/3/6 мес.; среднее, t = ср./σ·√n (у 3/6 мес. окна перекрываются — t завышен ~√h), доля
    IC > 0, по периодам. «Когда видно — уже поздно» = IC < 0.
 C. События (список задан задним числом, EVENTS): дно и пик относительного индекса сектора в
    окне, опознание по правилу PIT (первый месяц окна, когда фонд обогнал альты за 1 мес. на
    ≥ +20% или за 3 мес. на ≥ +40%, по отношению NAV), какая доля (лог) роста осталась, доход
    от опознания через 3/6/12 мес. к альтам и BTC, просадка от пика; все срабатывания правила
    за всё время — доля «ложных» (за 6 мес. сектор отстал от альтов).
 D. Оракул: лучший сектор каждого года / полугодия задним числом (по чистой доходности фонда)
    против правил A.
 E. Модель исполнителя (portfolio_vs_market.exec_book, шаг s6; сверка R +$1503 / −$186 к
    альтам, H +$1903): сектор монеты — по снимку CMC не позже дня входа (сопоставление как
    Q9/Q14: Q.matched_ids — тикер + цена ±20%), сила — доходность фонда сектора (ew, 2–500) к
    альтам за 3 мес. на этом снимке; порядок «сильный сектор первыми» (внутри случайно / по
    обороту), обратный, гейт «только секторы топ-1/2/3 по силе» (строгий: прочее/unknown не
    берём; мягкий: режем только монеты других нарративных секторов); до первой силы (PIT
    2020-10; LATE — с 2018-04, окно сигналов LATE с 2018) гейт пропускает всё, ключ порядка 0;
    сектор «прочее»/unknown в порядке — 0 (как рынок). 30 порядков J.SEEDS; вне выборки — вариант
    PIT выбран по циклу 2020-22, проверка на 2023-26. Метрики как Q13/Q14.
 F. Справочно, последний снимок: доходность секторов к альтам за 1/3/6/12 мес., признаки B.

Оговорки: по сути 2 нарративных цикла (2020–21 и 2023–25) — мало для вывода; теги CMC —
ярлыки, таксономия менялась (layer-1 с 2023-05, широкие RWA/AI-ярлыки на L1 в 2023–25), у
монеты с несколькими нарративами сектор зависит от порядка RULES; цена помесячная (первое
воскресенье), внутримесячные пики не видны; капа CMC у малого free float завышена; комиссия
без спреда и проскальзывания (0.5% — грубый стресс); перечень событий C задан задним числом.
Выводы без обещаний доходности и без рекомендаций по секторам.

Запуск из корня проекта:  PYTHONIOENCODING=utf-8 python backtest/narrative_sector_study.py
  (--skip-bot — без блока E, ~80 с; сеть не нужна)
Время прогона: ~6 мин (374 с 17.10.2026: A–D, F ~80 с — 26 режимов A × 18 конфигураций × 1000
  случайных; архив Binance и сигналы ~190 с; E 11 вариантов × 31 прогон × 2 книги ~70 с).
Проверки устойчивости A: без биржевых токенов, секторы от 10 монет, + AI-агенты в выборе,
  места 2–200, комиссия 0.5%, выпавшие = −100%.
Результат: backtest/narrative_sector_results.json
"""
from __future__ import annotations

import bisect
import collections
import datetime as dt
import json
import math
import random
import re
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cmc_rank_gate_study as G  # noqa: E402
import junk_filter_study as J  # noqa: E402
import ladder_dca_study as L  # noqa: E402
import portfolio_vs_market as P  # noqa: E402
import sector_cap_study as SC  # noqa: E402
import size_band_study as S  # noqa: E402
import supply_age_gate_study as Q  # noqa: E402
import top20_basket_study as T  # noqa: E402

OUT = Path(__file__).resolve().parent / "narrative_sector_results.json"
DAY = 86400
FEE, FEE_HI = 0.0015, 0.005
PIT_FROM = SC.TAGS_FROM                  # 2020-07-05 — первый снимок с секторными тегами
LATE_FROM = dt.date(2018, 1, 7)
UNIS = {"500": (2, 500), "200": (2, 200)}
MIN_MEMBERS = 3
SECTORS = ["exchange", "memes", "ai", "rwa", "gaming", "layer-2", "layer-1", "defi", "infra"]
AGENTS = "ai-agents"
AGENT_TAGS = {"ai-agents", "ai-agent-launchpad", "virtuals-protocol-ecosystem", "defai"}
GROUPS = SECTORS + ["other", "notag", AGENTS, "alt"]
GI = {g: i for i, g in enumerate(GROUPS)}
GI["btc"] = len(GROUPS)
RU = {**SC.SECTOR_RU, "notag": "нет тега", AGENTS: "AI-агенты", "alt": "альты", "btc": "BTC"}
LS, NS, HS = (1, 3, 6), (1, 2), (1, 3, 6)
DRAWS = 1000
# периоды держания: [начало, конец) по дате снимка начала месяца
PERIODS = [("2018-19", dt.date(2018, 1, 1), dt.date(2020, 1, 1)),
           ("2020-21", dt.date(2020, 1, 1), dt.date(2022, 1, 1)),
           ("2022", dt.date(2022, 1, 1), dt.date(2023, 1, 1)),
           ("2023-24", dt.date(2023, 1, 1), dt.date(2025, 1, 1)),
           ("2025-26", dt.date(2025, 1, 1), dt.date(2027, 1, 1))]
TOKSEC = re.compile(r"\(xstock\)|bstocks|pre-ipo|tokeni[sz]ed\b.*\b(stock|etf|currency|fund|trust|"
                    r"preferred|equity)\b", re.I)
ID_RULES = {"1м≥+20%": (1, 0.20), "3м≥+40%": (3, 0.40)}
JUMP_BAD, JUMP_SWAP, VOL_MIN = 5.0, 50.0, 0.001   # ложные скачки цены (см. Data.incons)
# (ключ, подпись, группа, окно поиска дна/пика/опознания с, по) — задано задним числом
EVENTS = [("defi20", "DeFi-лето 2020", "defi", (2020, 7), (2021, 1)),
          ("l1_21", "L1 2021", "layer-1", (2021, 1), (2021, 12)),
          ("game21", "игры/метавселенные/NFT 2021", "gaming", (2021, 6), (2022, 1)),
          ("meme21", "мемы 2021", "memes", (2021, 1), (2021, 12)),
          ("meme23", "мемы 2023–24", "memes", (2023, 3), (2024, 12)),
          ("ai23", "AI 2023–24", "ai", (2022, 12), (2024, 6)),
          ("rwa24", "RWA 2024", "rwa", (2023, 7), (2024, 12)),
          ("agents24", "AI-агенты 2024-10…2025-01", AGENTS, (2024, 9), (2025, 3))]
BOT_IS, BOT_OOS = ("2020-22",), ("2023-26",)


# ---------------------------------------------------------------- данные

class Data:
    """Все снимки deep: цены способом B (S.clean_series), теги, места, первое появление id."""

    def __init__(self):
        self.dates = Q.deep_dates()
        self.K = len(self.dates)
        self.snaps = [S.deep_snapshot(d) for d in self.dates]
        bad = T.glitches(self.snaps)
        for k, d in enumerate(self.dates):
            bad[k] |= {key for dd, key in T.GLITCHES if dd == d.isoformat()}
        self.bad = bad
        self.ser = S.clean_series(self.snaps)
        self.kpos = {d: k for k, d in enumerate(self.dates)}
        self.k_pit = self.kpos[PIT_FROM]
        self.k_late = self.kpos[LATE_FROM]
        self.tags: list[dict[int, list[str]]] = []
        self.rank: list[dict[int, int]] = []
        self.first_seen: dict[int, int] = {}
        self.sym: dict[int, str] = {}
        last: dict[int, list[str]] = {}
        for k, rows in enumerate(self.snaps):
            tg: dict[int, list[str]] = {}
            rk: dict[int, int] = {}
            for x in rows:
                i = x["id"]
                if i not in rk or x["rank"] < rk[i]:
                    rk[i] = x["rank"]
                    tg[i] = x.get("tags") or []
                self.first_seen.setdefault(i, k)
                self.sym[i] = x["symbol"]
                if x.get("tags"):
                    last[i] = x["tags"]
            self.tags.append(tg)
            self.rank.append(rk)
        self.last_tags = last
        self.last_seen = {i: max(s) for i, s in self.ser.items()}
        self.tok_excluded = 0
        self._pit: dict[tuple[int, int], str] = {}
        self._late: dict[int, str] = {}
        # Строки, где капа не сходится с местом CMC (место по капе в 3+ раза выше и на 50+ мест):
        # в исторических снимках это блок сразу за 200-м местом — нерейтингуемые CMC монеты
        # (HEX, WBNB, TRIX с капой $33 млрд на 201-м, DREP после свопа токена — $9.1 млрд на
        # 201-м). Из состава исключаются как битые строки.
        # Скачок цены за месяц > ×JUMP_BAD считается ложным (месяц как у выпавшей — 0%), если
        # оборот в снимке после скачка < VOL_MIN капы (неторгуемая/самозаявленная цена: IZE, XT,
        # TMG, BOTX) или скачок > ×JUMP_SWAP в строку с несогласованной капой (своп токена: DREP).
        self.incons: list[set[int]] = []
        self.liq: list[dict[int, float]] = []
        for rows in self.snaps:
            asc = sorted(x["mcap"] for x in rows if x["mcap"])
            bad_k = set()
            liq: dict[int, float] = {}
            for x in rows:
                v = x.get("vol") or 0.0
                liq[x["id"]] = v / x["mcap"] if x["mcap"] else (1.0 if v > 1e5 else 0.0)
                if not x["mcap"]:
                    continue
                mr = len(asc) - bisect.bisect_right(asc, x["mcap"]) + 1      # место по капе
                if mr * 3 < x["rank"] and x["rank"] - mr >= 50:
                    bad_k.add(x["id"])
            self.incons.append(bad_k)
            self.liq.append(liq)
        self.bad_jumps: set[tuple[int, int]] = set()

    def k_of(self, y: int, m: int) -> int:
        d = dt.date(y, m, 1)
        return next(k for k, x in enumerate(self.dates) if x >= d)

    def sec_pit(self, i: int, k: int) -> str:
        key = (i, k)
        s = self._pit.get(key)
        if s is None:
            tg = self.tags[k].get(i) if k >= self.k_pit else None
            s = self._pit[key] = SC.sector_of(tg)[0] if tg else "notag"
        return s

    def agent_pit(self, i: int, k: int) -> bool:
        return k >= self.k_pit and bool(AGENT_TAGS.intersection(self.tags[k].get(i) or ()))

    def sec_late(self, i: int) -> str:
        s = self._late.get(i)
        if s is None:
            tg = self.last_tags.get(i)
            s = self._late[i] = SC.sector_of(tg)[0] if tg else "notag"
        return s

    def agent_late(self, i: int) -> bool:
        return bool(AGENT_TAGS.intersection(self.last_tags.get(i) or ()))

    def ret(self, i: int, k: int, gone0: bool = False) -> float:
        """Доходность id за месяц k→k+1; нет в снимке k+1 — 0% (последняя цена), GONE0 — −100%,
        если id больше не появляется никогда."""
        s = self.ser[i]
        p = s.get(k + 1)
        if p is not None:
            r = p / s[k]
            if r > JUMP_BAD and (self.liq[k + 1].get(i, 1.0) < VOL_MIN
                                 or (r > JUMP_SWAP and i in self.incons[k + 1])):
                self.bad_jumps.add((i, k))
                return 0.0
            return r - 1.0
        if gone0 and self.last_seen[i] <= k:
            return -1.0
        return 0.0

    def universe(self, lo: int, hi: int) -> list[list[tuple[int, float]]]:
        """По снимкам: [(id, капа)] мест lo…hi без исключений Q4, сбоев капы и TOKSEC."""
        out = []
        tok = inc = 0
        for k, rows in enumerate(self.snaps):
            seen, mem = set(), []
            for x in sorted(rows, key=lambda r: r["rank"]):
                i = x["id"]
                if i in seen or not lo <= x["rank"] <= hi:
                    continue
                seen.add(i)
                if T.excluded(x) or f"{x['symbol']}|{x['name']}" in self.bad[k]:
                    continue
                if TOKSEC.search(x["name"] or ""):
                    tok += 1
                    continue
                if i in self.incons[k]:
                    if k >= self.k_pit:
                        inc += 1
                    continue
                if k not in self.ser.get(i, {}):
                    continue
                mem.append((i, x["mcap"] or 0.0))
            out.append(mem)
        self.tok_excluded = max(self.tok_excluded, tok)
        self.incons_excluded = max(getattr(self, "incons_excluded", 0), inc)
        return out


# ---------------------------------------------------------------- фонды групп

def build_funds(D: Data, univ, mode: str, gone0: bool = False) -> dict:
    """{ew|cw: {группа: {"R": брутто k→k+1 (None — пусто), "T": оборот ребаланса в k, "n": монет,
    "ids": [множество id в k]}}}. Группы: SECTORS, other, notag, AGENTS (пересекается), alt."""
    K = D.K
    out = {w: {g: {"R": [None] * (K - 1), "T": [0.0] * K, "n": [0] * K} for g in GROUPS}
           for w in ("ew", "cw")}
    members: dict[str, list[list[int]]] = {g: [] for g in GROUPS}
    prev = {w: {g: None for g in GROUPS} for w in ("ew", "cw")}
    for k in range(K):
        grp: dict[str, list[tuple[int, float]]] = {g: [] for g in GROUPS}
        for i, mc in univ[k]:
            if mode == "pit":
                s, a = D.sec_pit(i, k), D.agent_pit(i, k)
            else:
                s, a = D.sec_late(i), D.agent_late(i)
            grp[s].append((i, mc))
            grp["alt"].append((i, mc))
            if a:
                grp[AGENTS].append((i, mc))
        for g in GROUPS:
            members[g].append([i for i, _mc in grp[g]])
        rets = {i: D.ret(i, k, gone0) for i, _mc in univ[k]} if k < K - 1 else {}
        for w in ("ew", "cw"):
            for g, mem in grp.items():
                f = out[w][g]
                f["n"][k] = len(mem)
                if not mem:
                    prev[w][g] = None
                    continue
                tot = sum(mc for _i, mc in mem)
                if w == "ew" or tot <= 0:
                    wt = {i: 1.0 / len(mem) for i, _mc in mem}
                else:
                    wt = {i: mc / tot for i, mc in mem}
                pd = prev[w][g]
                if pd is not None:
                    f["T"][k] = sum(abs(wt.get(i, 0.0) - pd.get(i, 0.0)) for i in wt.keys() | pd.keys())
                if k < K - 1:
                    R = sum(x * rets[i] for i, x in wt.items())
                    f["R"][k] = R
                    prev[w][g] = ({i: x * (1 + rets[i]) / (1 + R) for i, x in wt.items()}
                                  if 1 + R > 1e-12 else None)
    out["members"] = members
    return out


def btc_returns(D: Data) -> list[float]:
    s = D.ser[1]
    return [s[k + 1] / s[k] - 1 if k in s and k + 1 in s else 0.0 for k in range(D.K - 1)]


def net_matrix(fw: dict, btc_r: list[float], fee: float, K: int) -> list[list[float]]:
    """Rn[k][GI[g]] — чистая доходность фонда за k→k+1 (ребаланс в k за fee × оборот; пустой
    фонд — кэш 0%); последний столбец — BTC."""
    Rn = []
    for k in range(K - 1):
        row = []
        for g in GROUPS:
            f = fw[g]
            R = f["R"][k]
            row.append((1 - fee * f["T"][k]) * (1 + R) - 1 if R is not None else 0.0)
        row.append(btc_r[k])
        Rn.append(row)
    return Rn


def momentum(fw: dict, lb: int, kmin: int, K: int) -> list[dict[str, float | None]]:
    """mom[t][g] = рост фонда g за lb мес. до снимка t / рост фонда альтов − 1 (брутто, по данным
    с kmin); None — у группы нет истории."""
    out: list[dict] = [dict() for _ in range(K)]
    alt = fw["alt"]["R"]
    for g in GROUPS:
        R = fw[g]["R"]
        for t in range(kmin + lb, K):
            num = den = 1.0
            ok = True
            for j in range(t - lb, t):
                if R[j] is None or alt[j] is None:
                    ok = False
                    break
                num *= 1 + R[j]
                den *= 1 + alt[j]
            out[t][g] = num / den - 1 if ok else None
    return out


def eligible(fw: dict, mom: list[dict], sel: list[str], K: int,
             min_members: int = MIN_MEMBERS) -> list[list[str]]:
    return [[g for g in sel if fw[g]["n"][t] >= min_members and mom[t].get(g) is not None]
            for t in range(K)]


# ---------------------------------------------------------------- портфель фондов

def run(Rn, k0: int, k1: int, cohorts, H: int, fee: float = FEE) -> list[float]:
    """NAV (снимки k0…k1) портфеля: в месяц t — среднее H последних когорт (когорта — кортеж
    номеров фондов; None — индекс альтов), до старта — альты. Ребаланс к этим весам раз в
    месяц, комиссия fee × оборот (первая покупка — оборот 1)."""
    alt = GI["alt"]
    q = collections.deque([(alt,)] * H, maxlen=H)
    nav, navs, b = 1.0, [1.0], None
    for t in range(k0, k1):
        q.append(cohorts[t - k0] or (alt,))
        A: dict[int, float] = {}
        for cc in q:
            w = 1.0 / (H * len(cc))
            for g in cc:
                A[g] = A.get(g, 0.0) + w
        to = 1.0 if b is None else sum(abs(A.get(g, 0.0) - b.get(g, 0.0)) for g in A.keys() | b.keys())
        nav *= 1 - fee * to
        r = Rn[t]
        gross = sum(w * (1 + r[g]) for g, w in A.items())
        nav *= gross
        b = {g: w * (1 + r[g]) / gross for g, w in A.items()}
        navs.append(nav)
    return navs


def random_finals(Rn, elig_idx, k0: int, k1: int, N: int, H: int, seed: str,
                  bounds: dict[str, tuple[int, int]], draws: int = DRAWS) -> tuple[list[float], dict]:
    """Итоговые NAV draws случайных портфелей (каждая когорта — N случайных допустимых секторов),
    по возрастанию, и рост за периоды bounds {имя: (снимок начала, снимок конца)} — тоже
    отсортированные списки."""
    rnd = random.Random(seed)
    alt = GI["alt"]
    out = []
    per: dict[str, list[float]] = {p: [] for p in bounds}
    need = {k for ab in bounds.values() for k in ab}
    wq = 1.0 / H
    for _ in range(draws):
        q = collections.deque([(alt,)] * H, maxlen=H)
        nav, b = 1.0, None
        mark = {k0: 1.0} if k0 in need else {}
        for t in range(k0, k1):
            el = elig_idx[t]
            q.append(tuple(rnd.sample(el, N)) if len(el) >= N else (alt,))
            A: dict[int, float] = {}
            for cc in q:
                w = wq / len(cc)
                for g in cc:
                    A[g] = A.get(g, 0.0) + w
            if b is None:
                to = 1.0
            else:
                to = 0.0
                for g in A.keys() | b.keys():
                    to += abs(A.get(g, 0.0) - b.get(g, 0.0))
            nav *= 1 - FEE * to
            r = Rn[t]
            gross = 0.0
            for g, w in A.items():
                gross += w * (1 + r[g])
            nav *= gross
            b = {g: w * (1 + r[g]) / gross for g, w in A.items()}
            if t + 1 in need:
                mark[t + 1] = nav
        out.append(nav)
        for p, (ka, kb) in bounds.items():
            per[p].append(mark[kb] / mark[ka])
    out.sort()
    for v in per.values():
        v.sort()
    return out, per


def mdd(navs: list[float]) -> float:
    pk, m = navs[0], 0.0
    for v in navs:
        pk = max(pk, v)
        m = max(m, 1 - v / pk)
    return m


def period_bounds(D: Data, k0: int, k1: int) -> dict[str, tuple[int, int]]:
    out = {}
    for name, a, b in PERIODS:
        ka = max(k0, next((k for k, d in enumerate(D.dates) if d >= a), D.K))
        kb = min(k1, next((k for k, d in enumerate(D.dates) if d >= b), D.K - 1))
        if kb > ka:
            out[name] = (ka, kb)
    return out


def metrics(D: Data, navs, alt_navs, btc_navs, k0: int, k1: int, k_first: int,
            rnd: tuple[list[float], dict] | None = None) -> dict:
    tot, ta, tb = navs[-1], alt_navs[-1], btc_navs[-1]
    rel = [a / b for a, b in zip(navs, alt_navs)]
    j0 = k_first - k0
    better = [navs[j + 1] / navs[j] > alt_navs[j + 1] / alt_navs[j] for j in range(j0, len(navs) - 1)]
    m = {"total": tot - 1, "ex_alt": tot / ta - 1, "ex_btc": tot / tb - 1,
         "beat_alt_months": sum(better) / len(better) if better else None,
         "mdd": mdd(navs), "mdd_rel": mdd(rel), "periods": {}}
    for name, (ka, kb) in period_bounds(D, k0, k1).items():
        a, b = ka - k0, kb - k0
        r, ra, rb = navs[b] / navs[a], alt_navs[b] / alt_navs[a], btc_navs[b] / btc_navs[a]
        m["periods"][name] = {"ret": r - 1, "alt": ra - 1, "btc": rb - 1, "ex_alt": r / ra - 1}
        if rnd:
            pl = rnd[1][name]
            m["periods"][name]["pct_random"] = bisect.bisect_left(pl, r) / len(pl) * 100
    if rnd:
        finals = rnd[0]
        m["pct_random"] = bisect.bisect_left(finals, tot) / len(finals) * 100
        m["random_median"] = finals[len(finals) // 2] - 1
    return m


# ---------------------------------------------------------------- A: секторный моментум

def mode_setup(D: Data, funds: dict, w: str, kmin: int, btc_r, fee: float = FEE) -> dict:
    fw = funds[w]
    Rn = net_matrix(fw, btc_r, fee, D.K)
    moms = {lb: momentum(fw, lb, kmin, D.K) for lb in LS}
    return {"fw": fw, "Rn": Rn, "mom": moms, "kmin": kmin}


def cohorts_of(mom, elig, k0: int, k1: int, N: int, best: bool):
    out = []
    for t in range(k0, k1):
        el = elig[t]
        if len(el) < N:
            out.append(None)
            continue
        srt = sorted(el, key=lambda g: mom[t][g], reverse=best)
        out.append(tuple(GI[g] for g in srt[:N]))
    return out


def test_A(D: Data, ms: dict, k0: int, k1: int, sel: list[str], tag: str, with_random: bool = True,
           fee: float = FEE, min_members: int = MIN_MEMBERS) -> dict:
    """Все конфигурации L×N×H×(лучшие/худшие) одного режима + случайные + бенчмарки."""
    Rn = ms["Rn"]
    alt_navs = run(Rn, k0, k1, [(GI["alt"],)] * (k1 - k0), 1, fee)
    btc_navs = run(Rn, k0, k1, [(GI["btc"],)] * (k1 - k0), 1, fee)
    res = {"window": [D.dates[k0].isoformat(), D.dates[k1].isoformat()],
           "alt": metrics(D, alt_navs, alt_navs, btc_navs, k0, k1, k0),
           "btc": metrics(D, btc_navs, alt_navs, btc_navs, k0, k1, k0), "configs": {},
           "random": {}}
    for lb in LS:
        mom = ms["mom"][lb]
        elig = eligible(ms["fw"], mom, sel, D.K, min_members)
        el_idx = [[GI[g] for g in e] for e in elig]
        k_first = next((t for t in range(k0, k1) if elig[t]), k1)
        bounds = period_bounds(D, k0, k1)
        for N in NS:
            for H in HS:
                rr = None
                if with_random:
                    rr = random_finals(Rn, el_idx, k0, k1, N, H, f"{tag}|{lb}|{N}|{H}", bounds)
                    fin = rr[0]
                    res["random"][f"L{lb}N{N}H{H}"] = {
                        "median": fin[len(fin) // 2] - 1, "p5": fin[len(fin) // 20] - 1,
                        "p95": fin[len(fin) * 19 // 20] - 1,
                        "median_ex_alt": fin[len(fin) // 2] / alt_navs[-1] - 1,
                        "periods_median": {p: v[len(v) // 2] - 1 for p, v in rr[1].items()}}
                for best in (True, False):
                    co = cohorts_of(mom, elig, k0, k1, N, best)
                    navs = run(Rn, k0, k1, co, H, fee)
                    m = metrics(D, navs, alt_navs, btc_navs, k0, k1, k_first, rr)
                    picks = collections.Counter(RU[GROUPS[g]] for c in co if c for g in c)
                    m["picks"] = dict(picks.most_common())
                    m["first_decision"] = D.dates[k_first].isoformat() if k_first < k1 else None
                    res["configs"][f"{'best' if best else 'worst'}_L{lb}N{N}H{H}"] = m
    return res


def summary_A(res: dict) -> dict:
    out = {}
    for dr in ("best", "worst"):
        cs = [v for k, v in res["configs"].items() if k.startswith(dr)]
        out[dr] = {"n": len(cs), "beat_alt": sum(c["ex_alt"] > 0 for c in cs),
                   "beat_btc": sum(c["ex_btc"] > 0 for c in cs),
                   "ex_alt_median": st.median(c["ex_alt"] for c in cs),
                   "ex_alt_min": min(c["ex_alt"] for c in cs), "ex_alt_max": max(c["ex_alt"] for c in cs),
                   "total_median": st.median(c["total"] for c in cs)}
        if "pct_random" in cs[0]:
            p = [c["pct_random"] for c in cs]
            out[dr].update({"pct_median": st.median(p), "pct_ge50": sum(x >= 50 for x in p),
                            "pct_ge90": sum(x >= 90 for x in p), "pct_ge95": sum(x >= 95 for x in p),
                            "pct_le10": sum(x <= 10 for x in p)})
            out[dr]["periods"] = {}
            for per in cs[0]["periods"]:
                pp = [c["periods"][per]["pct_random"] for c in cs]
                ea = [c["periods"][per]["ex_alt"] for c in cs]
                out[dr]["periods"][per] = {"pct_median": st.median(pp), "pct_ge50": sum(x >= 50 for x in pp),
                                           "ex_alt_median": st.median(ea)}
    return out


def pr_A(title: str, res: dict, full: bool) -> None:
    a, b = res["alt"], res["btc"]
    sm = summary_A(res)
    print(f"\n=== A {title}: окно {res['window'][0]} → {res['window'][1]}; альты {a['total']:+.0%} "
          f"(просадка {a['mdd']:.0%}), BTC {b['total']:+.0%}; альты по периодам: "
          + ", ".join(f"{p} {v['ret']:+.0%}" for p, v in a["periods"].items()))
    for dr in ("best", "worst"):
        s = sm[dr]
        name = "лучшие (моментум)" if dr == "best" else "худшие (разворот)"
        line = (f"  {name}: из {s['n']} конфигураций лучше альтов {s['beat_alt']}, лучше BTC "
                f"{s['beat_btc']}; к альтам медиана {s['ex_alt_median']:+.0%} ({s['ex_alt_min']:+.0%}…"
                f"{s['ex_alt_max']:+.0%}), итог медиана {s['total_median']:+.0%}")
        if "pct_median" in s:
            line += (f"; перцентиль к случайным медиана {s['pct_median']:.0f}, ≥50: {s['pct_ge50']}, ≥90: "
                     f"{s['pct_ge90']}, ≥95: {s['pct_ge95']}, ≤10: {s['pct_le10']}; по периодам (медиана "
                     f"перц., ≥50 из {s['n']}): " + ", ".join(
                         f"{p} {v['pct_median']:.0f} ({v['pct_ge50']})" for p, v in s["periods"].items()))
        print(line)
    if not full:
        return
    pers = list(a["periods"])
    print(f"  {'конф.':16} {'итог':>7} {'к альт':>7} {'к BTC':>7} {'перц.':>5} {'случ.мед':>8} "
          f"{'мес>альт':>8} {'просад':>6} " + " ".join(f"{p:>11}" for p in pers)
          + "  (по периодам — к альтам / перцентиль)")
    for k, c in res["configs"].items():
        rnd = res["random"].get(k.split("_", 1)[1], {})
        print(f"  {k:16} {c['total']:>+7.0%} {c['ex_alt']:>+7.0%} {c['ex_btc']:>+7.0%} "
              f"{c.get('pct_random', float('nan')):>5.0f} {rnd.get('median', float('nan')):>+8.0%} "
              f"{c['beat_alt_months'] or 0:>8.0%} {c['mdd']:>6.0%} "
              + " ".join(f"{c['periods'][p]['ex_alt']:+.0%}/{c['periods'][p].get('pct_random', float('nan')):.0f}"
                         .rjust(11) if p in c["periods"] else "          —" for p in pers))


# ---------------------------------------------------------------- B: признаки внимания

def features_B(D: Data, univ500, univ200, fw: dict, mom1, mom3) -> list[dict]:
    """Признаки по (месяц t, сектор), PIT. -> строки {t, g, признаки}."""
    K = D.K
    rows = []
    in200 = [{i for i, _mc in m} for m in univ200]
    share: list[dict[str, float]] = []
    for k in range(K):
        tot = sum(mc for _i, mc in univ500[k])
        sh = {g: 0.0 for g in SECTORS}
        if k >= D.k_pit and tot > 0:
            for i, mc in univ500[k]:
                s = D.sec_pit(i, k)
                if s in sh:
                    sh[s] += mc / tot
        share.append(sh)
    for t in range(D.k_pit + 1, K):
        n_all = len(univ500[t])
        cnt = collections.Counter(D.sec_pit(i, t) for i, _mc in univ500[t])
        cnt200 = collections.Counter(D.sec_pit(i, t) for i, _mc in univ200[t])
        new200 = collections.Counter()
        if t - 3 >= 0:
            for i, _mc in univ200[t]:
                if i not in in200[t - 3]:
                    new200[D.sec_pit(i, t)] += 1
        # лидеры роста месяца t−1 → t среди вселенной t−1
        gain = []
        for i, _mc in univ500[t - 1]:
            s = D.ser[i]
            if t in s:
                gain.append((s[t] / s[t - 1] - 1, i))
        gain.sort(reverse=True)
        top = collections.Counter()
        for _r, i in gain[:20]:
            top[D.sec_pit(i, t) if i in D.tags[t] else D.sec_pit(i, t - 1)] += 1
        # новые id в топ-3000 за 3 снимка t−2…t
        newc = collections.Counter()
        for k in range(max(1, t - 2), t + 1):
            for x in D.snaps[k]:
                i = x["id"]
                if D.first_seen.get(i) != k or T.excluded(x) or TOKSEC.search(x["name"] or ""):
                    continue
                newc[D.sec_pit(i, t) if D.tags[t].get(i) else D.sec_pit(i, k)] += 1
        for g in SECTORS:
            n = fw[g]["n"][t]
            if n < MIN_MEMBERS:
                continue
            r = {"t": t, "g": g, "n": n, "mom1": mom1[t].get(g), "mom3": mom3[t].get(g)}
            for lag in (1, 3):
                a, b = share[t].get(g, 0.0), share[t - lag].get(g, 0.0) if t - lag >= D.k_pit else 0.0
                r[f"dshare{lag}"] = a / b - 1 if b > 0 else None
            r["new200"] = new200.get(g, 0) if t - 3 >= D.k_pit else None
            r["new200_per"] = (new200.get(g, 0) / cnt200[g] if cnt200.get(g) else 0.0) if t - 3 >= D.k_pit else None
            r["gain20"] = top.get(g, 0) / 20
            r["gain20_over"] = (top.get(g, 0) / 20) / (cnt[g] / n_all) if cnt.get(g) and n_all else None
            r["newcoins3"] = newc.get(g, 0) if t - 2 >= D.k_pit else None
            r["newcoins3_per"] = newc.get(g, 0) / n if t - 2 >= D.k_pit else None
            r["share"] = share[t].get(g, 0.0)
            rows.append(r)
    return rows


FEATS_B = [("mom1", "справ.: сила к альтам 1 мес."), ("mom3", "справ.: сила к альтам 3 мес."),
           ("dshare1", "(i) рост доли в капе альтов, 1 мес."), ("dshare3", "(i) то же, 3 мес."),
           ("new200", "(ii) вошли в топ-200 за 3 мес., шт."), ("new200_per", "(ii) то же на монету сектора в топ-200"),
           ("gain20", "(iii) доля среди 20 лидеров роста"), ("gain20_over", "(iii) то же к доле в числе монет"),
           ("newcoins3", "(iv) новых id в топ-3000 за 3 мес., шт."), ("newcoins3_per", "(iv) то же на монету сектора")]


def fwd_rel(Rn, g: str, t: int, h: int, K: int) -> float | None:
    if t + h > K - 1:
        return None
    a = b = 1.0
    for k in range(t, t + h):
        a *= 1 + Rn[k][GI[g]]
        b *= 1 + Rn[k][GI["alt"]]
    return a / b - 1


def ic_B(D: Data, rows: list[dict], Rn, K: int) -> dict:
    by_t: dict[int, list[dict]] = {}
    for r in rows:
        by_t.setdefault(r["t"], []).append(r)
    pb = {name: (a, b) for name, a, b in PERIODS if name != "2018-19"}
    out = {}
    for ft, _lab in FEATS_B:
        o = {}
        for h in (1, 3, 6):
            vals = []
            for t, rr in sorted(by_t.items()):
                xs, ys = [], []
                for r in rr:
                    y = fwd_rel(Rn, r["g"], t, h, K)
                    if r[ft] is None or y is None:
                        continue
                    xs.append(r[ft])
                    ys.append(y)
                if len(xs) >= 4 and len(set(xs)) > 1 and len(set(ys)) > 1:
                    vals.append((t, G.spearman(xs, ys)))
            v = [x for _t, x in vals]
            if len(v) < 3:
                o[str(h)] = {"n": len(v)}
                continue
            m, sd = st.fmean(v), st.stdev(v)
            per = {}
            for name, (a, b) in pb.items():
                vv = [x for t, x in vals if a <= D.dates[t] < b]
                per[name] = st.fmean(vv) if vv else None
            o[str(h)] = {"n": len(v), "ic": m, "t": m / sd * math.sqrt(len(v)) if sd else None,
                         "pos": sum(x > 0 for x in v) / len(v), "periods": per}
        out[ft] = o
    return out


# ---------------------------------------------------------------- C: события

def fund_nav(Rn, g: str, k_start: int, K: int) -> list[float | None]:
    nav: list[float | None] = [None] * K
    nav[k_start] = 1.0
    for k in range(k_start, K - 1):
        nav[k + 1] = nav[k] * (1 + Rn[k][GI[g]])
    return nav


def first_member(fw: dict, g: str, kmin: int, K: int) -> int | None:
    return next((k for k in range(kmin, K - 1) if fw[g]["n"][k] > 0), None)


def event_C(D: Data, ms: dict, ev, K: int) -> dict:
    key, label, g, (ya, ma), (yb, mb) = ev
    fw, Rn = ms["fw"], ms["Rn"]
    k_s = first_member(fw, g, ms["kmin"], K)
    if k_s is None:
        return {"label": label, "group": g, "missing": True}
    ka, kb = max(D.k_of(ya, ma), k_s), min(D.k_of(yb, mb), K - 1)
    nav = fund_nav(Rn, g, k_s, K)
    alt = fund_nav(Rn, "alt", k_s, K)
    btc = fund_nav(Rn, "btc", k_s, K)
    rel = {k: nav[k] / alt[k] for k in range(k_s, K)}
    ks = list(range(ka, kb + 1))
    kp = max(ks, key=lambda k: rel[k])
    kt = min((k for k in range(ka, kp + 1)), key=lambda k: rel[k])
    kpa = max(ks, key=lambda k: nav[k])
    kta = min((k for k in range(ka, kpa + 1)), key=lambda k: nav[k])
    o = {"label": label, "group": g, "window": [D.dates[ka].isoformat(), D.dates[kb].isoformat()],
         "first_month": D.dates[k_s].isoformat(),
         "trough": D.dates[kt].isoformat(), "peak": D.dates[kp].isoformat(),
         "rel_growth": rel[kp] / rel[kt] - 1, "abs_growth_same": nav[kp] / nav[kt] - 1,
         "alt_same": alt[kp] / alt[kt] - 1, "btc_same": btc[kp] / btc[kt] - 1,
         "abs_trough": D.dates[kta].isoformat(), "abs_peak": D.dates[kpa].isoformat(),
         "abs_growth": nav[kpa] / nav[kta] - 1,
         "dd_rel_12m": min(rel[k] for k in range(kp, min(kp + 12, K - 1) + 1)) / rel[kp] - 1,
         "dd_abs_12m": min(nav[k] for k in range(kpa, min(kpa + 12, K - 1) + 1)) / nav[kpa] - 1,
         "id": {}}
    span = math.log(rel[kp] / rel[kt]) if rel[kp] > rel[kt] else 0.0
    for rule, (lb, thr) in ID_RULES.items():
        mom = ms["mom"][lb]
        kid = next((t for t in range(ka, kb + 1) if (mom[t].get(g) or -9) >= thr), None)
        if kid is None:
            o["id"][rule] = None
            continue
        x = {"date": D.dates[kid].isoformat(), "months_from_trough": kid - kt,
             "months_to_peak": kp - kid,
             "left_share": (math.log(rel[kp] / rel[kid]) / span if span and kid < kp else 0.0)}
        for h in (3, 6, 12):
            if kid + h <= K - 1:
                r = nav[kid + h] / nav[kid]
                x[f"f{h}"] = r - 1
                x[f"f{h}_ex_alt"] = r / (alt[kid + h] / alt[kid]) - 1
                x[f"f{h}_ex_btc"] = r / (btc[kid + h] / btc[kid]) - 1
        o["id"][rule] = x
    return o


def all_signals_C(D: Data, ms: dict, sel: list[str], K: int) -> dict:
    """Все срабатывания правил опознания (сектор-месяц) и доля «ложных» (за 6 мес. хуже альтов)."""
    fw, Rn = ms["fw"], ms["Rn"]
    out = {}
    for rule, (lb, thr) in ID_RULES.items():
        mom = ms["mom"][lb]
        sig, fired = [], {}
        for t in range(ms["kmin"], K):
            for g in sel:
                v = mom[t].get(g)
                on = v is not None and v >= thr and fw[g]["n"][t] >= MIN_MEMBERS
                if not on:
                    continue
                new = not any(fired.get((g, t - j)) for j in (1, 2, 3))
                fired[(g, t)] = True
                f = {h: fwd_rel(Rn, g, t, h, K) for h in (1, 3, 6, 12)}
                sig.append({"t": t, "g": g, "new": new, **{f"f{h}": f[h] for h in f}})
        ok6 = [s for s in sig if s["f6"] is not None]
        okn = [s for s in ok6 if s["new"]]

        def summ(ss):
            if not ss:
                return {"n": 0}
            return {"n": len(ss), "false_share": sum(s["f6"] < 0 for s in ss) / len(ss),
                    **{f"f{h}_median": st.median(s[f"f{h}"] for s in ss if s[f"f{h}"] is not None)
                       for h in (1, 3, 6)},
                    "f6_mean": st.fmean(s["f6"] for s in ss)}
        out[rule] = {"all": summ(ok6), "new": summ(okn),
                     "list_new": [f"{D.dates[s['t']].strftime('%Y-%m')} {RU[s['g']]} {s['f6']:+.0%}"
                                  for s in okn]}
    return out


# ---------------------------------------------------------------- D: оракул

def oracle(D: Data, ms: dict, sel: list[str], k0: int, k1: int, months: tuple[int, ...]) -> dict:
    fw, Rn = ms["fw"], ms["Rn"]
    cuts = [k0] + [k for k in range(k0 + 1, k1) if D.dates[k].month in months] + [k1]
    co, picks = [], []
    for ka, kb in zip(cuts, cuts[1:]):
        el = [g for g in sel if fw[g]["n"][ka] >= MIN_MEMBERS]

        def gr(g, ka=ka, kb=kb):
            v = 1.0
            for k in range(ka, kb):
                v *= 1 + Rn[k][GI[g]]
            return v
        best = max(el, key=gr)
        picks.append(f"{D.dates[ka].strftime('%Y-%m')} {RU[best]} {gr(best) - 1:+.0%}")
        co += [(GI[best],)] * (kb - ka)
    return {"navs": run(Rn, k0, k1, co, 1), "picks": picks}


# ---------------------------------------------------------------- расхождение PIT / LATE

def tag_lag(D: Data, univ500, cw_alt_nav: list[float | None]) -> dict:
    K = D.K
    out: dict = {"agreement": {}, "ai_now": {}, "timing": {}}
    groups = ["ai", AGENTS, "memes", "rwa", "gaming", "defi", "layer-1", "infra"]
    pb = [(name, a, b) for name, a, b in PERIODS if name != "2018-19"]
    for g in groups:
        o = {}
        for name, a, b in pb + [("всё", PIT_FROM, dt.date(2100, 1, 1))]:
            late_n = late_hit = pit_n = pit_hit = 0
            for k in range(D.k_pit, K):
                if not a <= D.dates[k] < b:
                    continue
                for i, _mc in univ500[k]:
                    if g == AGENTS:
                        lt, pt = D.agent_late(i), D.agent_pit(i, k)
                    else:
                        lt, pt = D.sec_late(i) == g, D.sec_pit(i, k) == g
                    late_n += lt
                    pit_n += pt
                    late_hit += lt and pt
                    pit_hit += lt and pt
            o[name] = {"late_n": late_n, "pit_n": pit_n,
                       "late_in_pit": late_hit / late_n if late_n else None,
                       "pit_in_late": pit_hit / pit_n if pit_n else None}
        out["agreement"][g] = o
    # монеты AI / агентов последнего снимка: был ли у них этот сектор год / два назад
    kl = K - 1
    for g in ("ai", AGENTS):
        now = [i for i, _mc in univ500[kl] if (D.agent_pit(i, kl) if g == AGENTS else D.sec_pit(i, kl) == g)]
        o = {"n": len(now), "coins": [D.sym[i] for i in now][:60]}
        for lag in (12, 24):
            kk = kl - lag
            present = [i for i in now if i in D.rank[kk]]
            had = [i for i in present if (D.agent_pit(i, kk) if g == AGENTS else D.sec_pit(i, kk) == g)]
            o[f"lag{lag}"] = {"date": D.dates[kk].isoformat(), "present": len(present),
                              "had_tag": len(had), "absent": len(now) - len(present)}
        out["ai_now"][g] = o
    # когда появился тег относительно цены (монеты, бывавшие во вселенной 2–500 с 2020-07)
    ever = {i for k in range(D.k_pit, K) for i, _mc in univ500[k]}
    for g in groups:
        born, late_tag, cens = 0, [], 0
        for i in ever:
            if g == AGENTS:
                kt = next((k for k in range(D.k_pit, K) if D.agent_pit(i, k)), None)
            else:
                kt = next((k for k in range(D.k_pit, K) if D.sec_pit(i, k) == g), None)
            if kt is None:
                continue
            f0 = D.first_seen[i]
            if kt == D.k_pit and f0 < D.k_pit:
                cens += 1
                continue
            if kt == f0:
                born += 1
                continue
            s = D.ser[i]
            lo, hi = max(f0, kt - 12), min(D.last_seen[i], kt + 12)
            ks = [k for k in range(lo, hi + 1) if k in s]
            if len(ks) < 3 or kt not in s:
                continue
            kp = max(ks, key=lambda k: s[k])
            ktr = min((k for k in ks if k <= kp), key=lambda k: s[k])
            if s[kp] <= s[ktr] * 1.0001:
                continue
            span = math.log(s[kp] / s[ktr])
            kmid = next(k for k in ks if ktr <= k <= kp and math.log(s[k] / s[ktr]) >= 0.5 * span)
            before = (min(max(math.log(s[kt] / s[ktr]) / span, 0.0), 1.0) if kt <= kp else 1.0)

            def rel(a, b):
                if a not in s or b not in s or cw_alt_nav[a] is None or cw_alt_nav[b] is None:
                    return None
                return (s[b] / s[a]) / (cw_alt_nav[b] / cw_alt_nav[a]) - 1
            late_tag.append({"tag_minus_peak": kt - kp, "before_peak": kt < kp, "before_mid": kt < kmid,
                             "growth_before": before, "rel6_before": rel(kt - 6, kt),
                             "rel6_after": rel(kt, kt + 6), "sym": D.sym[i],
                             "tag": D.dates[kt].strftime("%Y-%m")})
        o = {"born_with_tag": born, "censored_2020_07": cens, "retagged": len(late_tag)}
        if late_tag:
            rb = [x["rel6_before"] for x in late_tag if x["rel6_before"] is not None]
            ra = [x["rel6_after"] for x in late_tag if x["rel6_after"] is not None]
            o.update({"tag_minus_peak_median": st.median(x["tag_minus_peak"] for x in late_tag),
                      "before_peak_share": sum(x["before_peak"] for x in late_tag) / len(late_tag),
                      "before_mid_share": sum(x["before_mid"] for x in late_tag) / len(late_tag),
                      "growth_before_median": st.median(x["growth_before"] for x in late_tag),
                      "rel6_before_median": st.median(rb) if rb else None,
                      "rel6_after_median": st.median(ra) if ra else None,
                      "examples": [f"{x['sym']} {x['tag']} (тег − пик {x['tag_minus_peak']:+d} мес.)"
                                   for x in late_tag][:25]})
        out["timing"][g] = o
    return out


# ---------------------------------------------------------------- F: сейчас

def current_F(D: Data, ms_ew: dict, ms_cw: dict, rows_B: list[dict], univ500) -> dict:
    kl = D.K - 1
    out = {"date": D.dates[kl].isoformat(), "sectors": {}}
    fe = ms_ew["fw"]
    feat = {r["g"]: r for r in rows_B if r["t"] == kl}
    tot = sum(mc for _i, mc in univ500[kl]) or 1.0
    cap_ag = sum(mc for i, mc in univ500[kl] if D.agent_pit(i, kl)) / tot
    cap_ot = sum(mc for i, mc in univ500[kl] if D.sec_pit(i, kl) == "other") / tot
    for g in SECTORS + [AGENTS, "other", "notag"]:
        o = {"n": fe[g]["n"][kl]}
        for lb in (1, 3, 6, 12):
            for w, ms in (("ew", ms_ew), ("cw", ms_cw)):
                o[f"{w}{lb}"] = rel_back(ms["fw"], g, kl, lb)
        f = feat.get(g, {})
        for ft, _lab in FEATS_B:
            o[ft] = f.get(ft)
        o["share"] = f.get("share", cap_ag if g == AGENTS else (cap_ot if g == "other" else None))
        out["sectors"][g] = o
    for w in ("ew", "cw"):
        for lb in (1, 3, 6, 12):
            vals = {g: v[f"{w}{lb}"] for g, v in out["sectors"].items() if g in SECTORS + [AGENTS]
                    and v[f"{w}{lb}"] is not None and v["n"] >= MIN_MEMBERS}
            order = sorted(vals, key=vals.get, reverse=True)
            for g in vals:
                out["sectors"][g][f"rank_{w}{lb}"] = order.index(g) + 1
    return out


def rel_back(fw: dict, g: str, t: int, lb: int) -> float | None:
    a = b = 1.0
    for j in range(t - lb, t):
        R, A = fw[g]["R"][j], fw["alt"]["R"][j]
        if R is None or A is None:
            return None
        a *= 1 + R
        b *= 1 + A
    return a / b - 1


# ---------------------------------------------------------------- E: модель исполнителя

def bot_variants() -> list[dict]:
    def gate(tg, kk, strict):
        def f(ep):
            top = ep[f"top_{tg}"]
            if top is None:
                return True
            s = ep[f"sec_{tg}"]
            if s in top[:kk]:
                return True
            return (not strict) and s not in SECTORS
        return f
    vs = [{"key": "base", "label": "база (прод, по обороту)", "kind": "base", "tags": None}]
    for tg in ("pit", "late"):
        nm = tg.upper()
        vs.append({"key": f"{tg}_strong", "label": f"{nm}: сильный сектор первыми, внутри случ.",
                   "kind": "order", "tags": tg, "order": lambda ep, tg=tg: -(ep[f"str_{tg}"] or 0.0)})
        if tg == "pit":
            vs.append({"key": "pit_strong_rank", "label": "PIT: сильный сектор первыми, внутри обор.",
                       "kind": "order", "tags": tg,
                       "order": lambda ep: (-(ep["str_pit"] or 0.0), ep["rank"])})
            vs.append({"key": "pit_weak", "label": "PIT: слабый сектор первыми, внутри случ.",
                       "kind": "order", "tags": tg, "order": lambda ep: ep["str_pit"] or 0.0})
        for kk in ((1, 2, 3) if tg == "pit" else (2,)):
            vs.append({"key": f"{tg}_top{kk}", "label": f"{nm}: гейт топ-{kk} сектора (строгий)",
                       "kind": "gate", "tags": tg, "g": gate(tg, kk, True)})
        vs.append({"key": f"{tg}_top2soft", "label": f"{nm}: гейт топ-2 мягкий",
                   "kind": "gate", "tags": tg, "g": gate(tg, 2, False)})
    return vs


def bot_link(D: Data, ms_pit: dict, ms_late: dict, t0: float) -> dict:
    coins = L.load_archive()
    all_segs = L.segments(coins)
    px = T.Prices(all_segs)
    btc = P.btc_closes(all_segs)
    last_day = max(btc)
    alt, _ = P.market_index(ROOT / "scanner.db")
    comps = T.compositions(px, last_day)
    cap, _ = T.basket_level(px, comps, "cap", last_day)
    series = {"alt": alt, "cap": cap, "btc": btc}
    dates = [d for d in T.first_sundays() if T.day_ts(d) <= last_day]
    snaps = [S.deep_snapshot(d) for d in dates]
    days = [T.day_ts(d) for d in dates]
    bad = T.glitches(snaps)
    for k, d in enumerate(dates):
        bad[k] |= {key for dd, key in T.GLITCHES if dd == d.isoformat()}
    mids = Q.matched_ids(snaps, bad, days, px, last_day)
    print(f"\n[E] архив Binance до {dt.datetime.fromtimestamp(last_day, dt.timezone.utc).date()}, "
          f"снимков сопоставления {len(dates)}  [{time.time() - t0:.0f} с]")
    vranks = L.vol_ranks(all_segs)
    npairs = P.pairs_per_day(vranks)
    segs = [s for s in all_segs if len(s["c"]) >= 231]
    hot = L.load_hot()
    src = [ep for ep in L.signal_days(segs, vranks, hot, {"prod": L.is_spring_prod})["prod"]
           if ep["cycle"] != "?"]
    rank_lists = {}
    for tg, ms in (("pit", ms_pit), ("late", ms_late)):
        mom3, fw = ms["mom"][3], ms["fw"]
        rank_lists[tg] = [sorted((g for g in SECTORS if fw[g]["n"][t] >= MIN_MEMBERS
                                  and mom3[t].get(g) is not None), key=lambda g: mom3[t][g], reverse=True)
                          or None for t in range(D.K)]
    src_cnt = collections.Counter()
    for ep in src:
        n = npairs.get(ep["day"]) or 0
        ep["share"] = ep["rank"] / n if n else 1.0
        k = bisect.bisect_right(days, ep["day"]) - 1
        hit = mids[k].get(ep["seg"]["sym"]) if 0 <= k < len(mids) else None
        kd = D.kpos[dates[k]] if k >= 0 else None
        ep["cmc_id"] = hit[1] if hit else None
        for tg, ms in (("pit", ms_pit), ("late", ms_late)):
            if not hit:
                s = "unknown"
            elif tg == "pit":
                tags = D.tags[kd].get(hit[1]) if kd >= D.k_pit else None
                s = SC.sector_of(tags)[0] if tags else "unknown"
            else:
                s = D.sec_late(hit[1])
                s = "unknown" if s == "notag" else s
            ep[f"sec_{tg}"] = s
            ep[f"str_{tg}"] = ms["mom"][3][kd].get(s) if (kd is not None and s in SECTORS) else None
            ep[f"top_{tg}"] = rank_lists[tg][kd] if kd is not None else None
        src_cnt[("pit", ep["sec_pit"])] += 1
    print(f"[E] дней сигнала прода {len(src)}  [{time.time() - t0:.0f} с]")
    base_take, illiq = P.EXEC_POLICY
    sig = [ep for ep in src if base_take(ep)]
    cov = {}
    for c, _a, _b in L.CYCLES:
        ss = [ep for ep in sig if ep["cycle"] == c]
        cov[c] = {"n": len(ss),
                  "pit_known": sum(1 for ep in ss if ep["str_pit"] is not None) / len(ss),
                  "pit_ranked": sum(1 for ep in ss if ep["top_pit"] is not None) / len(ss),
                  "pit_unknown": sum(1 for ep in ss if ep["sec_pit"] == "unknown") / len(ss),
                  "pit_other": sum(1 for ep in ss if ep["sec_pit"] == "other") / len(ss),
                  "late_known": sum(1 for ep in ss if ep["str_late"] is not None) / len(ss),
                  "pit_top2": sum(1 for ep in ss if ep["top_pit"] and ep["sec_pit"] in ep["top_pit"][:2])
                  / len(ss)}
    print("[E] сигналы после фильтров прода по циклам: " + "; ".join(
        f"{c}: {v['n']}, сила PIT известна {v['pit_known']:.0%} (рейтинг есть {v['pit_ranked']:.0%}, "
        f"unknown {v['pit_unknown']:.0%}, прочее {v['pit_other']:.0%}, в топ-2 {v['pit_top2']:.0%}), "
        f"LATE {v['late_known']:.0%}" for c, v in cov.items()))
    VARIANTS = bot_variants()
    out: dict = {"meta": {"seeds": J.SEEDS, "is": BOT_IS, "oos": BOT_OOS,
                          "variants": {v["key"]: v["label"] for v in VARIANTS},
                          "archive_last_day": last_day}, "signals": {"n": len(sig), "coverage": cov}}
    ref = json.loads((Path(__file__).resolve().parent / "portfolio_vs_market_results.json")
                     .read_text(encoding="utf-8"))["exec"]
    by_key = {G.key_of(ep): ep for ep in src}
    for b, (sell, _stop) in J.BOOKS.items():
        cache: dict = {}

        def sim(ep, sell=sell, cache=cache):
            k = (ep["seg"]["sym"], ep["e"])
            if k not in cache:
                cache[k] = L.simulate_exec(ep["seg"], ep["e"], sell, budget=J.BUDGET,
                                           horizon=None, rules=L.EXEC_ALL)
            return cache[k]
        fl = Q.Flows(series)
        base = P.exec_book(src, sim, *P.EXEC_POLICY, cooldown=True, cash=True)
        chk = P.evaluate_flows(base, alt, btc)
        s6 = ref[b]["s6"]
        assert chk["n"] == s6["n"] and abs(chk["pnl"] - s6["pnl"]) < 1e-6 and \
            abs(chk["alt"] - s6["alt"]) < 1e-6, f"{b}: s6 не повторился ({chk['pnl']} / {s6['pnl']})"
        print(f"\n[E] сверка {b}: база s6 n={chk['n']} P&L {chk['pnl']:+.0f}$ (эталон {s6['pnl']:+.0f}$), "
              f"−альты {chk['pnl'] - chk['alt']:+.0f}$ ✓  [{time.time() - t0:.0f} с]")
        top = sorted(base, key=lambda t: t[1]["pnl"], reverse=True)[:max(1, len(base) // 10)]
        base_best = {G.key_of(ep) for ep, _r in top}
        # разбивка покупок базы по силе сектора на входе (PIT)
        bd: dict = {}
        for ep, r in base:
            tp = ep["top_pit"]
            if tp is None:
                grp = "до силы PIT"
            elif ep["sec_pit"] not in SECTORS:
                grp = "прочее/unknown"
            else:
                pos = tp.index(ep["sec_pit"]) + 1 if ep["sec_pit"] in tp else 99
                grp = "топ-2" if pos <= 2 else ("3–5" if pos <= 5 else "6+")
            x = bd.setdefault(grp, {"n": 0, "spent": 0.0, "pnl": 0.0, "ex_alt": 0.0, "spent_ok": 0.0})
            x["n"] += 1
            x["spent"] += r["spent"]
            x["pnl"] += r["pnl"]
            if r["spent"] > 0 and fl.row(ep, r)["alt"] is not None:
                x["ex_alt"] += r["pnl"] - fl.row(ep, r)["alt"]
                x["spent_ok"] += r["spent"]
        ob: dict = {"best_threshold_pnl": top[-1][1]["pnl"], "base_by_strength": bd, "variants": {}}
        seeds_base = base_main = None
        for v in VARIANTS:
            take = base_take if v["kind"] != "gate" else (lambda ep, g=v["g"]: base_take(ep) and g(ep))
            order = v.get("order", "rank")
            taken = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True, order=order, seed=0)
            m = Q.book_metrics(fl, taken, base_best, lambda k, take=take: take(by_key[k]))
            if v["key"] == "base":
                base_main = m
            m["vs_prod"] = {"pnl": m["pnl"] - base_main["pnl"],
                            **{f"ex_{k}": (m["pnl"] - m[k]) - (base_main["pnl"] - base_main[k])
                               for k, _n in G.BENCH}}
            runs, died = [], []
            for sd in range(J.SEEDS):
                tk = P.exec_book(src, sim, take, illiq, cooldown=True, cash=True,
                                 order="random" if v["kind"] != "order" else order, seed=sd)
                runs.append(G.seed_row(fl.vs(tk)))
                died.append(sum(1 for ep, _r in tk if ep["died2y"]))
            if v["key"] == "base":
                seeds_base = runs
            rnd = {"pnl_mean": st.fmean(x["pnl"] for x in runs),
                   "spent_mean": st.fmean(x["spent"] for x in runs), "died2y_mean": st.fmean(died),
                   "beat_base_pnl": sum(1 for x, y in zip(runs, seeds_base) if x["pnl"] > y["pnl"]),
                   "beat_base_ex_alt": sum(1 for x, y in zip(runs, seeds_base)
                                           if x["pnl"] - x["alt"] > y["pnl"] - y["alt"])}
            for k, _n in G.BENCH:
                rnd[f"ex_{k}_mean"] = st.fmean(x["pnl"] - x[k] for x in runs)
                rnd[f"ex_{k}_per_usd"] = st.fmean((x["pnl"] - x[k]) / x["spent"] for x in runs)
                rnd[f"beat_{k}_runs"] = sum(1 for x in runs if x["pnl"] > x[k])
            for part, cyc in (("is", BOT_IS), ("oos", BOT_OOS)):
                rnd[f"{part}_pnl"] = st.fmean(G.cyc_sum(x, cyc, "pnl") for x in runs)
                rnd[f"{part}_ex_alt"] = st.fmean(G.cyc_sum(x, cyc, "ex_alt") for x in runs)
                rnd[f"{part}_ex_btc"] = st.fmean(G.cyc_sum(x, cyc, "ex_btc") for x in runs)
                rnd[f"{part}_beat_base_ex_alt"] = sum(
                    1 for x, y in zip(runs, seeds_base) if G.cyc_sum(x, cyc, "ex_alt") > G.cyc_sum(y, cyc, "ex_alt"))
            rnd["cycles"] = {c: {"pnl": st.fmean(G.cyc_sum(x, (c,), "pnl") for x in runs),
                                 "ex_alt": st.fmean(G.cyc_sum(x, (c,), "ex_alt") for x in runs)}
                             for c, _a, _b in L.CYCLES}
            m["random"] = rnd
            ob["variants"][v["key"]] = m
        out[b] = ob
        name = "R правила" if b == "R" else "H держать"
        vb, rb = ob["variants"]["base"], ob["variants"]["base"]["random"]
        print(f"=== E книга {name}: прод поз. {vb['n']}, P&L {vb['pnl']:+.0f}$, −альты {vb['pnl'] - vb['alt']:+.0f}$, "
              f"−BTC {vb['pnl'] - vb['btc']:+.0f}$; случайный порядок (ср. {J.SEEDS}): P&L {rb['pnl_mean']:+.0f}$, "
              f"−альты {rb['ex_alt_mean']:+.0f}$")
        print("  покупки базы по силе сектора на входе (PIT): " + "; ".join(
            f"{g}: {x['n']} поз., P&L {x['pnl'] / x['spent'] * 100 if x['spent'] else 0:+.0f}% на $, к альтам "
            f"{x['ex_alt'] / x['spent_ok'] * 100 if x['spent_ok'] else 0:+.0f}% на $"
            for g, x in sorted(bd.items())))
        print(f"  {'вариант':44} {'P&L':>6} {'влож.':>6} {'−альты':>13} {'−BTC':>13} {'лучше б.':>9} "
              f"{'2018-20':>8} {'2020-22':>8} {'2023-26':>8} {'к проду':>7} {'поз.':>4} {'†ср.':>5} {'лучш.':>6}")
        for v in VARIANTS:
            m = ob["variants"][v["key"]]
            r = m["random"]
            print(f"  {v['label']:44} {r['pnl_mean']:>+6.0f} {r['spent_mean']:>6.0f} "
                  + " ".join(f"{r[f'ex_{k}_mean']:+.0f} ({r[f'ex_{k}_per_usd'] * 100:+.1f}%)".rjust(13)
                             for k in ("alt", "btc"))
                  + f"{r['beat_base_pnl']}/{r['beat_base_ex_alt']}".rjust(10)
                  + "".join(f"{r['cycles'][c]['ex_alt']:>+9.0f}" for c, _a, _b in L.CYCLES)
                  + f" {m['vs_prod']['ex_alt']:>+7.0f} {m['n']:>4} {r['died2y_mean']:>5.1f} "
                  + f"{m['best_lost']}({m['best_lost_by_gate']})".rjust(6))
        vb_r = ob["variants"]["base"]["random"]
        cand = [v for v in VARIANTS if v["tags"] == "pit"]
        pick = max(cand, key=lambda v: ob["variants"][v["key"]]["random"]["is_ex_alt"])
        vp = ob["variants"][pick["key"]]["random"]
        ob["oos"] = {"pick": pick["key"],
                     "is_ex_alt": vp["is_ex_alt"] - vb_r["is_ex_alt"], "is_beat": vp["is_beat_base_ex_alt"],
                     "oos_ex_alt": vp["oos_ex_alt"] - vb_r["oos_ex_alt"], "oos_beat": vp["oos_beat_base_ex_alt"],
                     "oos_pnl": vp["oos_pnl"] - vb_r["oos_pnl"], "oos_ex_btc": vp["oos_ex_btc"] - vb_r["oos_ex_btc"],
                     "all": {v["key"]: {"is": ob["variants"][v["key"]]["random"]["is_ex_alt"] - vb_r["is_ex_alt"],
                                        "oos": ob["variants"][v["key"]]["random"]["oos_ex_alt"] - vb_r["oos_ex_alt"]}
                             for v in VARIANTS if v["key"] != "base"}}
        o = ob["oos"]
        print(f"  вне выборки: по 2020-22 выбран «{pick['label']}» — к случ. базе −альты {o['is_ex_alt']:+.0f}$ "
              f"({o['is_beat']}/{J.SEEDS}); 2023-26: −альты {o['oos_ex_alt']:+.0f}$ ({o['oos_beat']}/{J.SEEDS}), "
              f"P&L {o['oos_pnl']:+.0f}$, −BTC {o['oos_ex_btc']:+.0f}$")
        print("  все варианты к случ. базе, −альты 2020-22 | 2023-26: " + "; ".join(
            f"{k} {x['is']:+.0f} | {x['oos']:+.0f}" for k, x in o["all"].items()))
        print(f"  [{time.time() - t0:.0f} с]")
    return out


# ---------------------------------------------------------------- main

def pct(x) -> str:
    return "—" if x is None else f"{x:+.0%}"


def main() -> int:
    t0 = time.time()
    skip_bot = "--skip-bot" in sys.argv
    D = Data()
    K = D.K
    univ = {u: D.universe(lo, hi) for u, (lo, hi) in UNIS.items()}
    print(f"снимков CMC {K} ({D.dates[0]} … {D.dates[-1]}); PIT с {D.dates[D.k_pit]}, LATE с "
          f"{D.dates[D.k_late]}; исключено из 2–500 строк: токенизированные акции/ETF {D.tok_excluded}, "
          f"капа не сходится с местом CMC (с 2020-07) {D.incons_excluded}  [{time.time() - t0:.0f} с]")
    btc_r = btc_returns(D)
    funds = {("pit", u): build_funds(D, univ[u], "pit") for u in UNIS}
    funds.update({("late", u): build_funds(D, univ[u], "late") for u in UNIS})
    funds[("pit0", "500")] = build_funds(D, univ["500"], "pit", gone0=True)
    bj = sorted(D.bad_jumps, key=lambda x: x[1])
    print(f"фонды групп построены; ложных скачков цены (×{JUMP_BAD:g}+ при обороте < {VOL_MIN:.1%} капы "
          f"или ×{JUMP_SWAP:g}+ в строку с несогласованной капой) обнулено {len(bj)}: " + ", ".join(f"{D.sym[i]} {D.dates[k].strftime('%Y-%m')}" for i, k in bj)
          + f"  [{time.time() - t0:.0f} с]")

    # ---- покрытие и выпавшие
    cov = {}
    for k in (D.k_pit, D.k_of(2021, 1), D.k_of(2022, 1), D.k_of(2023, 1), D.k_of(2024, 1),
              D.k_of(2024, 11), D.k_of(2025, 1), K - 1):
        cov[D.dates[k].isoformat()] = {g: funds[("pit", "500")]["ew"][g]["n"][k] for g in GROUPS}
    print("Состав вселенной 2–500 по секторам PIT (монет): ")
    for d, v in cov.items():
        print(f"  {d}: " + " ".join(f"{RU[g]} {v[g]}" for g in GROUPS))
    gone = collections.Counter()
    gone_forever = collections.Counter()
    mm = collections.Counter()
    for k in range(D.k_pit, K - 1):
        y = D.dates[k].year
        for i, _mc in univ["500"][k]:
            mm[y] += 1
            if k + 1 not in D.ser[i]:
                gone[y] += 1
                if D.last_seen[i] <= k:
                    gone_forever[y] += 1
    print("Выпали из 3000 к следующему снимку (вселенная 2–500, доля монето-месяцев): " + ", ".join(
        f"{y} {gone[y] / mm[y]:.2%} (навсегда {gone_forever[y] / mm[y]:.2%})" for y in sorted(mm)))

    out: dict = {"meta": {"fee": FEE, "fee_hi": FEE_HI, "min_members": MIN_MEMBERS, "draws": DRAWS,
                          "sectors": SECTORS, "agent_tags": sorted(AGENT_TAGS), "pit_from": PIT_FROM.isoformat(),
                          "late_from": LATE_FROM.isoformat(), "universes": UNIS, "ls": LS, "ns": NS, "hs": HS,
                          "events": [[e[0], e[1], e[2], e[3], e[4]] for e in EVENTS],
                          "id_rules": ID_RULES, "tok_excluded_rows": D.tok_excluded,
                          "incons_excluded_rows": D.incons_excluded, "jump_bad": JUMP_BAD,
                          "jump_swap": JUMP_SWAP, "vol_min": VOL_MIN,
                          "bad_jumps": [f"{D.sym[i]} {D.dates[k].isoformat()}" for i, k in bj],
                          "rules_q14": [[s, sorted(t)] for s, t in SC.RULES]},
                 "coverage": cov,
                 "gone": {str(y): {"share": gone[y] / mm[y], "forever": gone_forever[y] / mm[y]} for y in mm}}

    # ---- режимы
    k_end = K - 1
    k0_pit = D.k_pit + 1                              # 2020-08: первое решение по 1 мес. PIT
    k0_late18 = D.k_late + 1
    modes = {}
    for u in UNIS:
        for w in ("ew", "cw"):
            modes[("pit", u, w)] = mode_setup(D, funds[("pit", u)], w, D.k_pit, btc_r)
            modes[("late", u, w)] = mode_setup(D, funds[("late", u)], w, D.k_pit, btc_r)
            if u == "500":
                modes[("late18", u, w)] = mode_setup(D, funds[("late", u)], w, D.k_late, btc_r)
                modes[("pit0", u, w)] = mode_setup(D, funds[("pit0", u)], w, D.k_pit, btc_r)
                modes[("pithi", u, w)] = mode_setup(D, funds[("pit", u)], w, D.k_pit, btc_r, FEE_HI)
    print(f"режимы готовы  [{time.time() - t0:.0f} с]")

    # ---- A
    outA = {}
    plan = [("pit", "500", True, SECTORS, k0_pit), ("late", "500", True, SECTORS, k0_pit),
            ("late18", "500", True, SECTORS, k0_late18), ("pit", "200", True, SECTORS, k0_pit),
            ("late", "200", True, SECTORS, k0_pit), ("pit0", "500", False, SECTORS, k0_pit),
            ("pithi", "500", False, SECTORS, k0_pit)]
    for tg, u, rnd, sel, k0 in plan:
        for w in ("ew", "cw"):
            key = f"{tg}_{u}_{w}"
            fee = FEE_HI if tg == "pithi" else FEE
            outA[key] = test_A(D, modes[(tg, u, w)], k0, k_end, sel, key, rnd, fee)
            outA[key]["summary"] = summary_A(outA[key])
            print(f"  A {key} готово  [{time.time() - t0:.0f} с]")
    no_exch = [g for g in SECTORS if g != "exchange"]
    for tg in ("pit", "late"):
        for w in ("ew", "cw"):
            for suf, sel, mm in (("+agents", SECTORS + [AGENTS], MIN_MEMBERS), ("-exch", no_exch, MIN_MEMBERS),
                                 ("-min10", SECTORS, 10)):
                key = f"{tg}_500_{w}{suf}"
                outA[key] = test_A(D, modes[(tg, "500", w)], k0_pit, k_end, sel, key, True, FEE, mm)
                outA[key]["summary"] = summary_A(outA[key])
    print(f"A готово  [{time.time() - t0:.0f} с]")
    titles = {"pit_500": "PIT, места 2–500", "late_500": "LATE (теги задним числом), 2–500, то же окно",
              "late18_500": "LATE с 2018, 2–500 (справочно)", "pit_200": "PIT, места 2–200",
              "late_200": "LATE, 2–200", "pit0_500": "PIT 2–500, выпавшие навсегда = −100%",
              "pithi_500": "PIT 2–500, комиссия 0.5%"}
    suffix = {"+agents": " + AI-агенты в выборе", "-exch": " без биржевых токенов",
              "-min10": " — секторы от 10 монет"}
    for key, res in outA.items():
        suf = next((s for s in suffix if key.endswith(s)), "")
        base, w = key[:len(key) - len(suf)].rsplit("_", 1)
        t = titles.get(base, base) + suffix.get(suf, "")
        pr_A(f"{t}, {'равные веса' if w == 'ew' else 'по капе'}", res,
             full=key in ("pit_500_ew", "pit_500_cw", "late_500_ew", "late_500_cw"))
    out["A"] = outA

    # ---- B
    ms_e, ms_c = modes[("pit", "500", "ew")], modes[("pit", "500", "cw")]
    rows_B = features_B(D, univ["500"], univ["200"], ms_e["fw"], ms_e["mom"][1], ms_e["mom"][3])
    icB = {"ew": ic_B(D, rows_B, ms_e["Rn"], K), "cw": ic_B(D, rows_B, ms_c["Rn"], K)}
    out["B"] = {"ic": icB, "n_rows": len(rows_B)}
    print(f"\n=== B: IC (Спирмен по секторам внутри месяца) признака к доходности сектора к альтам за "
          f"следующие h мес.; PIT, 2–500, {len(rows_B)} сектор-месяцев  [{time.time() - t0:.0f} с]")
    pers = [p for p, _a, _b in PERIODS if p != "2018-19"]
    print(f"  {'признак':44} " + " ".join(f"{'h=' + str(h) + ' IC (t)':>14}" for h in (1, 3, 6))
          + f" {'IC>0 h3':>7} | h=3 по периодам: " + " ".join(f"{p:>7}" for p in pers) + " | по капе h=3")
    for ft, lab in FEATS_B:
        o, oc = icB["ew"][ft], icB["cw"][ft]
        cells = []
        for h in ("1", "3", "6"):
            x = o.get(h, {})
            cells.append(f"{x['ic']:+.3f} ({x['t']:+.1f})".rjust(14) if x.get("ic") is not None else "—".rjust(14))
        x3 = o.get("3", {})
        print(f"  {lab:44} " + " ".join(cells) + f" {x3.get('pos', 0):>7.0%} | "
              + " ".join(f"{x3['periods'][p]:+.3f}".rjust(7) if x3.get("periods", {}).get(p) is not None
                         else "      —" for p in pers)
              + f" | {oc.get('3', {}).get('ic', 0):+.3f} ({oc.get('3', {}).get('t', 0):+.1f})")

    # ---- C
    outC = {"events": {}, "signals": {}}
    for tg in ("pit", "late"):
        for w in ("ew", "cw"):
            ms = modes[(tg, "500", w)]
            outC["events"][f"{tg}_{w}"] = {ev[0]: event_C(D, ms, ev, K) for ev in EVENTS}
            outC["signals"][f"{tg}_{w}"] = all_signals_C(D, ms, SECTORS + [AGENTS], K)
    out["C"] = outC
    print(f"\n=== C: события (относительный индекс сектора к альтам; опознание — правило PIT по 1 / 3 мес.)")
    for w in ("ew", "cw"):
        print(f"  --- {'равные веса' if w == 'ew' else 'по капе'}: PIT | LATE")
        for ev in EVENTS:
            o, ol = outC["events"][f"pit_{w}"][ev[0]], outC["events"][f"late_{w}"][ev[0]]
            if o.get("missing"):
                print(f"  {ev[1]}: нет данных PIT")
                continue
            line = (f"  {ev[1]:28} с {o['first_month'][:7]}: дно {o['trough'][:7]} → пик {o['peak'][:7]} к альтам "
                    f"{o['rel_growth']:+.0%} (сам {o['abs_growth_same']:+.0%}, альты {o['alt_same']:+.0%}); "
                    f"после пика 12 мес. к альтам {o['dd_rel_12m']:+.0%}, сам {o['dd_abs_12m']:+.0%}")
            print(line)
            for rule in ID_RULES:
                x = o["id"].get(rule)
                xl = ol["id"].get(rule) if not ol.get("missing") else None
                s = "не опознан" if not x else (
                    f"{x['date'][:7]} ({x['months_from_trough']:+d} от дна, {x['months_to_peak']:+d} до пика), "
                    f"осталось {x['left_share']:.0%} роста; дальше к альтам 3/6/12 мес. "
                    f"{pct(x.get('f3_ex_alt'))}/{pct(x.get('f6_ex_alt'))}/{pct(x.get('f12_ex_alt'))}, к BTC "
                    f"{pct(x.get('f3_ex_btc'))}/{pct(x.get('f6_ex_btc'))}/{pct(x.get('f12_ex_btc'))}")
                sl = "—" if not xl else (f"{xl['date'][:7]}, осталось {xl['left_share']:.0%}, к альтам 6/12 мес. "
                                         f"{pct(xl.get('f6_ex_alt'))}/{pct(xl.get('f12_ex_alt'))}")
                print(f"      {rule}: {s} | LATE: {sl}")
        for tg in ("pit", "late"):
            sg = outC["signals"][f"{tg}_{w}"]
            print(f"  все срабатывания {tg.upper()} ({w}): " + "; ".join(
                f"{rule}: {v['all']['n']} сектор-мес., «ложных» (6 мес. хуже альтов) {v['all'].get('false_share', 0):.0%}, "
                f"медиана к альтам 1/3/6 мес. {pct(v['all'].get('f1_median'))}/{pct(v['all'].get('f3_median'))}/"
                f"{pct(v['all'].get('f6_median'))}; новых {v['new']['n']} (ложных {v['new'].get('false_share', 0):.0%}, "
                f"медиана 6 мес. {pct(v['new'].get('f6_median'))})" for rule, v in sg.items()))
    print("  новые срабатывания PIT ew (1м≥+20%): " + ", ".join(outC["signals"]["pit_ew"]["1м≥+20%"]["list_new"]))

    # ---- D
    outD = {}
    print("\n=== D: оракул (лучший сектор периода задним числом) против правил A")
    for tg in ("pit", "late"):
        for w in ("ew", "cw"):
            ms = modes[(tg, "500", w)]
            resA = outA[f"{tg}_500_{w}"]
            best_cfg = max((k for k in resA["configs"] if k.startswith("best")),
                           key=lambda k: resA["configs"][k]["total"])
            o = {"best_rule": best_cfg, "best_rule_total": resA["configs"][best_cfg]["total"],
                 "alt_total": resA["alt"]["total"], "btc_total": resA["btc"]["total"]}
            for nm, mon in (("year", (1,)), ("half", (1, 7))):
                orc = oracle(D, ms, SECTORS, k0_pit, k_end, mon)
                o[nm] = {"total": orc["navs"][-1] - 1,
                         "ex_alt": orc["navs"][-1] / (resA["alt"]["total"] + 1) - 1, "picks": orc["picks"]}
            outD[f"{tg}_{w}"] = o
            print(f"  {tg.upper()} {w}: оракул по годам {o['year']['total']:+.0%} (к альтам {o['year']['ex_alt']:+.0%}), "
                  f"по полугодиям {o['half']['total']:+.0%} (к альтам {o['half']['ex_alt']:+.0%}); лучшее правило A "
                  f"задним числом {best_cfg} {o['best_rule_total']:+.0%}; альты {o['alt_total']:+.0%}, BTC {o['btc_total']:+.0%}")
            print(f"     по годам: " + "; ".join(o["year"]["picks"]))
    out["D"] = outD

    # ---- расхождение PIT / LATE
    cw_alt = fund_nav(modes[("pit", "500", "cw")]["Rn"], "alt", 0, K)
    lag = tag_lag(D, univ["500"], cw_alt)
    out["tags"] = lag
    print("\n=== Теги PIT против LATE")
    for g, o in lag["agreement"].items():
        print(f"  {RU[g]:15} LATE-монет в PIT-секторе: " + ", ".join(
            f"{p} {v['late_in_pit']:.0%}" if v["late_in_pit"] is not None else f"{p} —" for p, v in o.items())
            + f"; PIT-монет в LATE-секторе (всё): {o['всё']['pit_in_late'] or 0:.0%}")
    for g, o in lag["ai_now"].items():
        print(f"  {RU[g]} на {D.dates[-1]}: {o['n']} монет; " + "; ".join(
            f"{x['date'][:7]}: были в снимке {x['present']}, с этим тегом {x['had_tag']}, ещё не было {x['absent']}"
            for x in (o["lag12"], o["lag24"])))
    for g, o in lag["timing"].items():
        if o.get("retagged"):
            print(f"  {RU[g]:15} тег с рождения {o['born_with_tag']}, был до 2020-07 {o['censored_2020_07']}, доразмечен "
                  f"позже {o['retagged']}: тег − пик медиана {o['tag_minus_peak_median']:+.0f} мес., тег до пика "
                  f"{o['before_peak_share']:.0%}, до середины роста {o['before_mid_share']:.0%}, роста до тега "
                  f"(медиана) {o['growth_before_median']:.0%}; к альтам 6 мес. до тега {pct(o['rel6_before_median'])}, "
                  f"после {pct(o['rel6_after_median'])}")
        else:
            print(f"  {RU[g]:15} тег с рождения {o['born_with_tag']}, был до 2020-07 {o['censored_2020_07']}, доразмечен 0")

    # ---- F
    outF = current_F(D, ms_e, ms_c, rows_B, univ["500"])
    out["F"] = outF
    print(f"\n=== F: последний снимок {outF['date']} — доходность к альтам (равные веса | по капе), место из "
          f"{len(SECTORS) + 1}, признаки B")
    print(f"  {'сектор':15} {'монет':>5} " + " ".join(f"{'ew' + str(lb):>6}" for lb in (1, 3, 6, 12)) + " | "
          + " ".join(f"{'cw' + str(lb):>6}" for lb in (1, 3, 6, 12))
          + f" | {'доля капы':>9} {'Δдоли 3м':>8} {'нов.200':>7} {'лидеры':>6} {'нов.id':>6}")
    for g, o in outF["sectors"].items():
        print(f"  {RU[g]:15} {o['n']:>5} " + " ".join(
            (f"{o[f'ew{lb}']:+.0%}" + (f"#{o[f'rank_ew{lb}']}" if o.get(f"rank_ew{lb}") else "")).rjust(6)
            if o[f"ew{lb}"] is not None else "     —" for lb in (1, 3, 6, 12)) + " | " + " ".join(
            (f"{o[f'cw{lb}']:+.0%}" + (f"#{o[f'rank_cw{lb}']}" if o.get(f"rank_cw{lb}") else "")).rjust(6)
            if o[f"cw{lb}"] is not None else "     —" for lb in (1, 3, 6, 12))
            + f" | {(o['share'] or 0):>9.1%} {pct(o['dshare3']):>8} {o['new200'] if o['new200'] is not None else '—':>7} "
            f"{(o['gain20'] or 0):>6.0%} {o['newcoins3'] if o['newcoins3'] is not None else '—':>6}")

    # ---- E
    if not skip_bot:
        out["E"] = bot_link(D, modes[("pit", "500", "ew")], modes[("late18", "500", "ew")], t0)

    out["runtime_s"] = time.time() - t0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nsaved -> {OUT.name} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
