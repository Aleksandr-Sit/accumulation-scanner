"""Активность разработки по GitHub — замена CoinGecko developer_data (с 09.2026 этого
блока в ответе /coins/{id} нет совсем, при этом links.repos_url.github остаётся).

  • ссылка на репозиторий (github.com/org/repo) — публичная лента commits.atom: последние
    ~20 коммитов основной ветки с датами и авторами, без лимита API;
  • ссылка на организацию/пользователя (github.com/org) — GitHub API
    /orgs|users/{name}/repos?sort=pushed (60 запросов/час без токена, GITHUB_TOKEN в .env
    поднимает лимит): только дата последнего push, без счёта коммитов.
Тот же подход, что в backtest/quality_screen.py. Разбор — чистые функции (офлайн-тест).
Оба сигнала шумные: пустая ссылка ≠ нет разработчиков, старый репозиторий ≠ проект встал.
"""
from __future__ import annotations

import calendar
import os
import re
import time

from ..http import HttpClient

_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_UPDATED = re.compile(r"<updated>([^<]+)</updated>")
_AUTHOR = re.compile(r"<author>\s*<name>([^<]+)</name>", re.S)
FEED_CAP = 20          # столько коммитов отдаёт commits.atom — «20» значит «20 и больше»


def repo_links(detail: dict) -> list[str]:
    """Ссылки GitHub из ответа CoinGecko /coins/{id} (пустые и «github.com» без пути — вон)."""
    links = ((detail or {}).get("links") or {}).get("repos_url") or {}
    out = []
    for u in links.get("github") or []:
        if isinstance(u, str) and "github.com/" in u and u.split("github.com/", 1)[1].strip("/"):
            out.append(u.strip())
    return out


def split_link(url: str) -> tuple[str, str]:
    """'https://github.com/org/repo/tree/x' -> ('org', 'repo'); org-ссылка -> ('org', '')."""
    parts = [p for p in url.split("github.com/", 1)[1].split("/") if p]
    owner = parts[0] if parts else ""
    repo = parts[1].removesuffix(".git") if len(parts) > 1 else ""
    return owner, repo


def _iso_ts(stamp: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def parse_atom(xml: str | None) -> list[tuple[float, str]]:
    """commits.atom -> [(unix-время коммита, автор)], newest-first как в ленте."""
    out = []
    for body in _ENTRY.findall(xml or ""):
        m = _UPDATED.search(body)
        ts = _iso_ts(m.group(1)) if m else None
        if ts is None:
            continue
        a = _AUTHOR.search(body)
        out.append((ts, a.group(1).strip() if a else ""))
    return out


def summarize_commits(commits: list[tuple[float, str]], now: float, window_days: int = 28) -> dict:
    """{commits_4w, authors_4w, last_commit_days} по ленте одного репозитория."""
    if not commits:
        return {"commits_4w": None, "authors_4w": None, "last_commit_days": None}
    cut = now - window_days * 86400
    recent = [c for c in commits if c[0] >= cut]
    return {"commits_4w": len(recent),
            "authors_4w": len({a for _, a in recent if a}),
            "last_commit_days": max(0, int((now - max(t for t, _ in commits)) // 86400))}


def fetch_dev(http: HttpClient, links: list[str], now: float | None = None,
              max_links: int = 3) -> dict:
    """Активность по первым max_links ссылкам: коммиты/авторы за 4 недели — максимум по
    репозиториям, давность последнего коммита — минимум. Нет ссылок/ответа -> всё None."""
    now = now if now is not None else time.time()
    out = {"commits_4w": None, "authors_4w": None, "last_commit_days": None, "src": None}
    tok = os.getenv("GITHUB_TOKEN")
    hdr = {"Authorization": f"Bearer {tok}"} if tok else None
    seen = set()
    for url in links:
        owner, repo = split_link(url)
        if not owner or (owner, repo) in seen:
            continue
        seen.add((owner, repo))
        if len(seen) > max_links:
            break
        if repo:
            s = summarize_commits(parse_atom(http.get_text(
                f"https://github.com/{owner}/{repo}/commits.atom", retries=2)), now)
            src = "atom"
        else:
            s = {"commits_4w": None, "authors_4w": None, "last_commit_days": None}
            src = "api"
            for kind in ("orgs", "users"):
                r = http.get_json(f"https://api.github.com/{kind}/{owner}/repos",
                                  params={"sort": "pushed", "per_page": 1}, headers=hdr,
                                  retries=1)
                if isinstance(r, list) and r:
                    ts = _iso_ts(r[0].get("pushed_at") or "")
                    if ts:
                        s["last_commit_days"] = max(0, int((now - ts) // 86400))
                    break
        for k in ("commits_4w", "authors_4w"):
            if s[k] is not None:
                out[k] = max(out[k] or 0, s[k])
        if s["last_commit_days"] is not None:
            out["last_commit_days"] = (s["last_commit_days"] if out["last_commit_days"] is None
                                       else min(out["last_commit_days"], s["last_commit_days"]))
        if any(s[k] is not None for k in ("commits_4w", "last_commit_days")):
            out["src"] = out["src"] or src
    return out
