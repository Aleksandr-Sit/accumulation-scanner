"""Валидация рыночного контекста ПРОД-функциями: вход (spring_quality) и выход (evaluate_exit).

Вопросы:
  1. Вход. Ранжирует ли множитель качества пружины лучше с просадкой альт-рынка
     (market_dd_source=alt), чем с просадкой BTC (btc, прежний прод), и режут ли штрафы
     перегрева слабые входы? Подбор hot_penalty / hot_warm_penalty по сетке.
  2. Выход. A (прод-трейл) vs B (трейл сужен до market_hot_trailing_pct при перегреве
     рынка ≥ market_hot_alert) — то, что paper-книга B проверяет вперёд.

Данные:
  • рынок — таблица market_daily в scanner.db (заполнить: `python run.py market`);
    признаки и флаги перегрева — regime.market_feature_table / hot_flags (как в проде);
  • монеты — дневные свечи Binance из .cache/binance_1d (feature_study / zone_edges_study);
    не-альты (токенизированные акции, золото, стейблы) исключены по тегам Binance;
  • пружины — feature_study.find_springs (детектор зоны исследований, cooldown 90д).
Walk-forward: рыночные признаки на вход — за день ДО входа (день t ещё не закрыт в market_daily).

ВНИМАНИЕ: survivor-only (живые сегодня пары Binance) → все P — ВЕРХНЯЯ граница, сравнивать
варианты между собой. Эпизоды одного периода коррелированы (реальная n меньше), пороги
флагов перегрева подобраны in-sample по тем же вершинам — [оценка].

Запуск из корня проекта:  python backtest/market_regime_study.py
Результат: backtest/market_regime_results.json
"""
from __future__ import annotations

import copy
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from feature_study import (_EXCL, _tagged_non_alts, find_springs,  # noqa: E402
                           sim_exit, HORIZON)
from scanner import regime  # noqa: E402
from scanner.config import Config, load_config  # noqa: E402
from scanner.db import Store  # noqa: E402
from scanner.models import Candidate  # noqa: E402
from scanner.stages.entry_quality import spring_quality  # noqa: E402
from scanner.stages.exit import evaluate_exit  # noqa: E402

CACHE = ROOT / ".cache" / "binance_1d"
OUT = Path(__file__).resolve().parent / "market_regime_results.json"
DAY = 86400
FEE = 0.0015
CYCLES = [("2018-20", 1514764800, 1585699200),   # 2018-01-01 .. 2020-04-01
          ("2020-22", 1585699200, 1672531200),   # .. 2023-01-01
          ("2023-25", 1672531200, 1790640000)]   # .. (форвард режется концом данных)
MIN_CELL = 15


# ---------------------------------------------------------------- данные

def load_market(cfg: Config) -> tuple[dict[int, dict], dict[int, dict]]:
    """{day: признаки}, {day: hot_flags} — прод-функции на истории market_daily."""
    store = Store(cfg["output"]["db_path"])
    rows = store.market_rows()
    store.close()
    if not rows:
        sys.exit("market_daily пуста — сначала `python run.py market`")
    feats = regime.market_feature_table(regime.market_series(rows))
    hot = {d: regime.hot_flags(f, cfg) for d, f in feats.items()}
    return feats, hot


def load_coins() -> dict[str, tuple[list[int], list[float], list[float]]]:
    excl = _EXCL | _tagged_non_alts()
    out = {}
    for f in sorted(CACHE.glob("*.json")):
        base = f.stem[:-4] if f.stem.endswith("USDT") else f.stem
        if f.stem == "BTCUSDT" or base in excl:
            continue
        d = json.loads(f.read_text())
        if len(d["cl"]) >= 300:
            out[f.stem] = ([int(t) for t in d["ts"]], d["cl"], d["vol"])
    return out


def _sec(t: int) -> int:
    return t // 1000 if t > 1e11 else t


def btc_dd_by_day() -> dict[int, float]:
    """Просадка BTC как в проде: classify_regime на окне 1000 дней (Bybit-свечи в скане)."""
    d = json.loads((CACHE / "BTCUSDT.json").read_text())
    ts, cl = [_sec(int(t)) for t in d["ts"]], d["cl"]
    out = {}
    for i in range(60, len(cl)):
        w = cl[max(0, i - 999):i + 1]
        r = regime.classify_regime(w, 30, 50)
        if r["drawdown"] is not None:
            out[ts[i] // DAY * DAY] = r["drawdown"]
    return out


def build_episodes(coins, feats, hot, btc_dd) -> list[dict]:
    eps = []
    for sym, (ts, cl, vol) in coins.items():
        for e, f in find_springs(cl, vol):
            day = _sec(ts[e]) // DAY * DAY
            mf = feats.get(day - DAY, {})
            h = hot.get(day - DAY, {})
            cand = Candidate(
                source="bt", track="A", symbol=sym[:-4], zone="ПРУЖИНА/ДНО",
                drawdown_from_ath_pct=f["dd_ath"] * 100,
                indicators={"base_len_days": f["base_len"], "vol_trend": f["vtrend"]},
                market_dd=btc_dd.get(day - DAY),
                alt_market_dd=mf.get("alt_dd"),
                market_hot_score=h.get("score"),
                market_hot_lit=list(h.get("lit") or []))
            fwd = len(cl) - 1 - e
            end365 = min(e + 365, len(cl) - 1)
            eps.append({"sym": sym, "e": e, "day": day, "fwd": fwd, "cand": cand,
                        "hit365": any(cl[u] >= cl[e] * 2 for u in range(e + 1, end365 + 1)),
                        "q": time.strftime("%Y", time.gmtime(day)) + "Q"
                             + str((time.gmtime(day).tm_mon - 1) // 3 + 1)})
    return eps


# ---------------------------------------------------------------- вход

def with_quality(cfg: Config, **over) -> Config:
    d = copy.deepcopy(cfg._d)
    d["stage4b_quality"].update(over)
    return Config(d)


def _p(b):
    return sum(x["hit365"] for x in b) / len(b) if b else None


def tercile_report(eps: list[dict], key: str) -> dict:
    """P(+100/365д) и медиана net по терцилям множителя; спред верх−низ по циклам."""
    xs = sorted(e[key] for e in eps)
    t1, t2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
    cells = {}
    for name, lo, hi in (("low", -1, t1), ("mid", t1, t2), ("high", t2, 9)):
        b = [e for e in eps if lo < e[key] <= hi]
        cells[name] = {"n": len(b), "p365": round(_p(b), 3) if b else None,
                       "med_net": round(statistics.median(e["net_a"] for e in b), 3) if b else None}
    spread = {}
    for cname, a, bnd in CYCLES:
        E = [e for e in eps if a <= e["day"] < bnd]
        if len(E) < 3 * MIN_CELL:
            continue
        ys = sorted(e[key] for e in E)
        lo3 = [e for e in E if e[key] <= ys[len(ys) // 3]]
        hi3 = [e for e in E if e[key] > ys[2 * len(ys) // 3]]
        if len(lo3) >= MIN_CELL and len(hi3) >= MIN_CELL:
            spread[cname] = round(_p(hi3) - _p(lo3), 3)
    return {"terciles": cells, "spread_high_minus_low_by_cycle": spread}


def entry_block(cfg: Config, eps: list[dict]) -> dict:
    full = [e for e in eps if e["fwd"] >= 365]
    variants = {
        "btc_no_hot": dict(market_dd_source="btc", hot_penalty=1.0, hot_warm_penalty=1.0),
        "alt_no_hot": dict(market_dd_source="alt", hot_penalty=1.0, hot_warm_penalty=1.0),
        "mix_no_hot": dict(market_dd_source="mix", hot_penalty=1.0, hot_warm_penalty=1.0),
        "alt_prod": {},   # как в config.json сейчас
    }
    res = {"episodes": len(full), "base_p365": round(_p(full), 3), "variants": {}}
    print(f"\n=== ВХОД: множитель качества пружины (прод spring_quality), {len(full)} эпизодов "
          f"с форвардом ≥365д, база P(+100/365д) {_p(full)*100:.0f}% ===")
    for name, over in variants.items():
        c = with_quality(cfg, **over)
        key = f"q_{name}"
        for e in full:
            e[key] = spring_quality(e["cand"], c)[0]
        r = tercile_report(full, key)
        res["variants"][name] = r
        t = r["terciles"]
        print(f"  {name:11} низ/сред/верх: " + " | ".join(
            f"P {t[k]['p365']*100:3.0f}% med {t[k]['med_net']*100:+4.0f}% n{t[k]['n']}"
            for k in ("low", "mid", "high") if t[k]["n"])
              + "  | спред по циклам: " + ", ".join(f"{k} {v*100:+.0f}пп"
                                                     for k, v in r["spread_high_minus_low_by_cycle"].items()))

    # Сетка штрафов перегрева поверх alt: что лучше отделяет верхнюю треть от нижней.
    print("\n--- сетка штрафов перегрева (alt-dd): P верхней трети по множителю / спред ---")
    grid = []
    for hp in (1.0, 0.85, 0.75, 0.6):
        for wp in (1.0, 0.9, 0.85, 0.75):
            if wp < hp:
                continue   # тёплый рынок не штрафуется сильнее горячего
            c = with_quality(cfg, market_dd_source="alt", hot_penalty=hp, hot_warm_penalty=wp)
            for e in full:
                e["_q"] = spring_quality(e["cand"], c)[0]
            r = tercile_report(full, "_q")
            sp = r["spread_high_minus_low_by_cycle"]
            row = {"hot_penalty": hp, "hot_warm_penalty": wp,
                   "p_high": r["terciles"]["high"]["p365"], "p_low": r["terciles"]["low"]["p365"],
                   "med_high": r["terciles"]["high"]["med_net"],
                   "min_cycle_spread": min(sp.values()) if sp else None, "spread": sp}
            grid.append(row)
            print(f"  hot×{hp:.2f} warm×{wp:.2f}: верх P {row['p_high']*100:3.0f}% med "
                  f"{row['med_high']*100:+4.0f}% · низ P {row['p_low']*100:3.0f}% · "
                  f"мин. спред по циклам {row['min_cycle_spread']*100 if row['min_cycle_spread'] is not None else float('nan'):+.0f}пп")
    res["hot_grid"] = grid

    # Прямой срез: горящих флагов на входе 0 vs ≥1 по циклам (то, на чём стоит штраф).
    print("\n--- перегрев на входе: 0 флагов vs ≥1 (P(+100/365д)) ---")
    cut = {}
    for cname, a, bnd in CYCLES:
        E = [e for e in full if a <= e["day"] < bnd and e["cand"].market_hot_score is not None]
        cold = [e for e in E if e["cand"].market_hot_score == 0]
        warm = [e for e in E if e["cand"].market_hot_score > 0]
        if len(cold) >= MIN_CELL and len(warm) >= MIN_CELL:
            cut[cname] = {"cold_p": round(_p(cold), 3), "cold_n": len(cold),
                          "warm_p": round(_p(warm), 3), "warm_n": len(warm)}
            print(f"  {cname}: холодно P {_p(cold)*100:3.0f}% (n{len(cold)}) | "
                  f"≥1 флаг P {_p(warm)*100:3.0f}% (n{len(warm)})")
    res["hot_cut_by_cycle"] = cut
    return res


# ---------------------------------------------------------------- выход

def sim_prod_exit(cfg: Config, cl: list[float], ts: list[int], e: int, hot: dict[int, dict],
                  variant: str) -> float:
    """Пошаговый выход ПРОД-функцией evaluate_exit + исполнение как paper-executor
    (инвалидация/трейлинг — продать всё, ladder_i — доля от начального объёма)."""
    x = cfg["stage8_exit"]
    entry = cl[e]
    pos = {"entry_price": entry, "base_low": min(cl[max(0, e - 30):e + 1]), "variant": variant}
    end = min(e + HORIZON, len(cl) - 1)
    rem, proc, hwm = 1.0, 0.0, entry
    triggered: set[str] = set()
    confirm = int(x.get("invalidation_confirm_days", 1))
    for t in range(e + 1, end + 1):
        c = cl[t]
        hwm = max(hwm, c)
        h = hot.get(_sec(ts[t]) // DAY * DAY - DAY) or {}
        market = {"hot_score": h.get("score"), "lit": h.get("lit") or []}
        sigs = evaluate_exit(pos, c, hwm, None, triggered, cfg,
                             recent_closes=cl[max(e + 1, t - confirm + 1):t + 1], market=market)
        for s in sigs:
            triggered.add(s["type"])
            if s["type"] in ("invalidation", "trailing"):
                proc += rem * c
                rem = 0.0
            elif s["type"].startswith("ladder_"):
                frac = x["ladder"][int(s["type"].split("_")[1])][1]
                q = min(frac, rem)
                proc += q * c
                rem -= q
        if rem <= 1e-9:
            break
    if rem > 1e-9:
        proc += rem * cl[end]
    return proc * (1 - FEE) / (entry * (1 + FEE)) - 1


def _stats(v: list[float]) -> dict:
    s = sorted(v)
    return {"n": len(v), "mean": round(statistics.fmean(v), 3), "median": round(statistics.median(v), 3),
            "p25": round(s[len(s) // 4], 3), "win": round(sum(x > 0 for x in v) / len(v), 3)}


def exit_block(cfg: Config, eps: list[dict], coins, hot) -> dict:
    print(f"\n=== ВЫХОД: прод evaluate_exit, A (трейл {cfg['stage8_exit']['trailing_from_hwm_pct']}%) "
          f"vs B (сужение до {cfg['stage8_exit']['market_hot_trailing_pct']}% при перегреве "
          f"≥{cfg['stage8_exit']['market_hot_alert']}), {len(eps)} эпизодов ===")
    x = cfg["stage8_exit"]
    lad = [tuple(v) for v in x["ladder"]]
    mism = 0
    for e in eps:
        ts, cl, _ = coins[e["sym"]]
        e["net_a"] = sim_prod_exit(cfg, cl, ts, e["e"], hot, "A")
        e["net_b"] = sim_prod_exit(cfg, cl, ts, e["e"], hot, "B")
        ref = sim_exit(cl, e["e"], lad, x["trailing_from_hwm_pct"] / 100,
                       x["trailing_arm_after_gain_pct"] / 100,
                       x["invalidation_below_base_low_pct"] / 100, x["invalidation_confirm_days"])
        mism += abs(ref - e["net_a"]) > 0.02
    print(f"  сверка A с feature_study.sim_exit: расхождение >2пп в {mism} из {len(eps)} эпизодов")
    A = _stats([e["net_a"] for e in eps])
    B = _stats([e["net_b"] for e in eps])
    touched = [e for e in eps if abs(e["net_a"] - e["net_b"]) > 1e-9]
    res = {"A": A, "B": B, "touched": len(touched),
           "b_better": sum(1 for e in touched if e["net_b"] > e["net_a"]), "by_cycle": {},
           "sanity_mismatch_vs_feature_study": mism}
    for nm, s in (("A", A), ("B", B)):
        print(f"  {nm}: mean {s['mean']*100:+6.1f}%  med {s['median']*100:+6.1f}%  "
              f"p25 {s['p25']*100:+6.1f}%  win {s['win']*100:3.0f}%")
    print(f"  затронуто сужением {len(touched)}, B лучше в {res['b_better']}")
    for cname, a, bnd in CYCLES:
        E = [e for e in eps if a <= e["day"] < bnd]
        if len(E) < MIN_CELL:
            continue
        sa, sb = _stats([e["net_a"] for e in E]), _stats([e["net_b"] for e in E])
        res["by_cycle"][cname] = {"A": sa, "B": sb}
        print(f"    {cname}: n {len(E):4}  A med {sa['median']*100:+6.1f}% mean {sa['mean']*100:+7.1f}%  |  "
              f"B med {sb['median']*100:+6.1f}% mean {sb['mean']*100:+7.1f}%")
    return res


def main() -> int:
    t0 = time.time()
    cfg = load_config()
    feats, hot = load_market(cfg)
    coins = load_coins()
    btc_dd = btc_dd_by_day()
    eps = build_episodes(coins, feats, hot, btc_dd)
    print(f"монет {len(coins)}, пружин {len(eps)} на {len({e['sym'] for e in eps})} монетах, "
          f"кварталов {len({e['q'] for e in eps})}; рынок {len(feats)} дней "
          f"({time.time()-t0:.0f} с)")
    ex = exit_block(cfg, eps, coins, hot)          # net_a нужен и для блока входа
    en = entry_block(cfg, eps)
    OUT.write_text(json.dumps({"generated": time.strftime("%Y-%m-%d"),
                               "coins": len(coins), "springs": len(eps),
                               "entry": en, "exit": ex}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n→ {OUT.name} ({time.time()-t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
