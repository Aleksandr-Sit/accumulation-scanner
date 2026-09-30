"""Stage 4c — on-chain накопление (Dune). Чистая оценка, тестируется офлайн.

Сигналы (из механики, не из feature_study — на free невалидируемо, ЭВРИСТИКА):
  • net_flow нормируется НА ОБЪЁМ: доля недельного оборота, ушедшая нетто на биржи.
    Так $302M у LINK и $1M у мелкой монеты сопоставимы (иначе крупнокап всегда
    штрафуется абсолютным порогом). <0 доля = отток = накопление (бычье);
    >0 = приток = распределение (медвежье).
  • holders_change_pct_7d > 0: число холдеров растёт = приток участников (бычье).

Наполняет блок скора onchain, который иначе всегда None (режет confidence ≤0.90).
"""
from __future__ import annotations


def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def match_onchain(onchain_map: dict, symbol: str | None, address: str | None) -> dict | None:
    """Запись Dune для монеты: сначала по адресу контракта (однозначно), потом по
    тикеру (однофамильцы из других сетей возможны — поэтому только fallback)."""
    if not onchain_map:
        return None
    addr = (address or "").lower()
    if addr and addr in onchain_map:
        return onchain_map[addr]
    return onchain_map.get((symbol or "").upper()) if symbol else None


def flow_ratio(net_flow_usd_7d, volume_24h) -> float | None:
    """Нетто-поток на биржи как доля недельного оборота (net_flow / (vol*7))."""
    if not isinstance(net_flow_usd_7d, (int, float)):
        return None
    if not isinstance(volume_24h, (int, float)) or volume_24h <= 0:
        return None
    return net_flow_usd_7d / (volume_24h * 7)


def assess_onchain(net_flow_usd_7d, holders_change_pct_7d, cfg,
                   volume_24h=None) -> tuple[float | None, list[str]]:
    """(под-балл 0–10 | None, заметки). None = нет данных Dune."""
    o = cfg.get("onchain", {}) or {}
    nf = net_flow_usd_7d if isinstance(net_flow_usd_7d, (int, float)) else None
    hc = holders_change_pct_7d if isinstance(holders_change_pct_7d, (int, float)) else None
    if nf is None and hc is None:
        return None, []

    s = 5.0
    notes: list[str] = []
    used = False        # был ли применён хоть один полезный сигнал (иначе -> None)
    if nf is not None:
        ratio = flow_ratio(nf, volume_24h)
        big = o.get("flow_big_ratio", 0.10)      # 10% недельного оборота = значимо
        small = o.get("flow_small_ratio", 0.03)
        sanity = o.get("flow_sanity_cap", 0.5)   # >50% оборота = механический артефакт
        usd_lbl = f"${abs(nf)/1e6:.1f}M"
        if ratio is not None and abs(ratio) > sanity:
            # аномальная доля: нативный/врапнутый токен или тонкий объём — не доверяем
            notes.append(f"поток {usd_lbl} ({ratio*100:+.0f}% оборота) аномален — "
                         f"вероятно нативный/врапнутый токен, on-chain игнорируется")
        elif ratio is not None:
            used = True
            pct = f"{ratio*100:+.0f}% нед.оборота"
            if ratio <= -big:
                s += 2.5; notes.append(f"сильный отток с бирж {usd_lbl} ({pct}) — накопление")
            elif ratio < -small:
                s += 1.0; notes.append(f"отток с бирж {usd_lbl} ({pct}) — накопление")
            elif ratio >= big:
                s -= 2.5; notes.append(f"⚠ сильный приток на биржи {usd_lbl} ({pct}) — распределение")
            elif ratio > small:
                s -= 1.0; notes.append(f"⚠ приток на биржи {usd_lbl} ({pct})")
            else:
                notes.append(f"нейтральный поток бирж ({pct})")
        else:
            # нет объёма для нормировки -> абсолютный порог (грубее)
            used = True
            big_usd = o.get("flow_big_usd", 1_000_000)
            if nf <= -big_usd:
                s += 2.0; notes.append(f"отток с бирж {usd_lbl} — накопление")
            elif nf >= big_usd:
                s -= 2.0; notes.append(f"⚠ приток на биржи {usd_lbl} — распределение")

    if hc is not None:
        used = True
        big_h = o.get("holders_big_pct", 5)
        if hc >= big_h:
            s += 2.5; notes.append(f"холдеров +{hc:.1f}%/7д — сильный приток участников")
        elif hc > 0:
            s += 1.0; notes.append(f"холдеров +{hc:.1f}%/7д")
        elif hc <= -big_h:
            s -= 2.5; notes.append(f"⚠ холдеров {hc:.1f}%/7д — исход")
        elif hc < 0:
            s -= 1.0; notes.append(f"холдеров {hc:.1f}%/7д")

    if not used:
        return None, notes    # только аномальный поток без холдеров — сигнала нет
    return round(_clamp(s), 1), notes
