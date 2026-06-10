# -*- coding: utf-8 -*-
"""
build_data.py — 每天由 GitHub Actions 執行，產生網站要讀的 data.json
伺服器端抓 ESPN（沒有瀏覽器的跨網域限制），算好今日每場的實力值/讓分依據，
若有設定 ODDS_API_KEY 還會一併算賠率，全部寫進 data.json。

本機測試：python build_data.py        （會直接打 ESPN，需有網路）
GitHub 會每天自動跑，你不用管。
"""
import os, json, datetime as dt
import requests

LEAGUES = {
    "nba": {"path": "basketball/nba", "homeAdv": 2.5,  "shrink": 6,  "oddsSport": "basketball_nba", "injuries": True},
    "mlb": {"path": "baseball/mlb",   "homeAdv": 0.25, "shrink": 20, "oddsSport": "baseball_mlb",   "injuries": False},
}
UA = {"User-Agent": "Mozilla/5.0"}


def get(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


# ---- 從 standings 取每隊實力（每場淨分差）----
def stat_val(stats, kws):
    for s in stats or []:
        n = ((s.get("name") or "") + " " + (s.get("type") or "") + " " + (s.get("abbreviation") or "")).lower()
        if any(k in n for k in kws):
            v = s.get("value")
            if v is None:
                try: v = float(s.get("displayValue"))
                except (TypeError, ValueError): v = None
            if v is not None:
                return v
    return None


def collect_entries(node, out):
    if not isinstance(node, dict):
        return
    st = node.get("standings")
    if isinstance(st, dict) and isinstance(st.get("entries"), list):
        out.extend(st["entries"])
    if isinstance(node.get("entries"), list):
        out.extend(node["entries"])
    for k in ("children", "groups"):
        if isinstance(node.get(k), list):
            for c in node[k]:
                collect_entries(c, out)


def fetch_strength(cfg):
    data = get(f"https://site.api.espn.com/apis/v2/sports/{cfg['path']}/standings")
    entries = []; collect_entries(data, entries)
    out = {}
    for e in entries:
        abbr = (e.get("team") or {}).get("abbreviation")
        if not abbr:
            continue
        gp = stat_val(e.get("stats"), ["gamesplayed", "games played"]) or 1
        w = stat_val(e.get("stats"), ["wins"]); l = stat_val(e.get("stats"), ["losses"])
        diff = stat_val(e.get("stats"), ["differential", "pointdiff", "rundiff"])
        pf = stat_val(e.get("stats"), ["avgpointsfor", "pointspergame"])
        pa = stat_val(e.get("stats"), ["avgpointsagainst", "opp"])
        tf = stat_val(e.get("stats"), ["pointsfor", "runsscored"])
        ta = stat_val(e.get("stats"), ["pointsagainst", "runsallowed"])
        if pf is None and tf is not None: pf = tf / gp
        if pa is None and ta is not None: pa = ta / gp
        margin = (diff / gp) if diff is not None else ((pf - pa) if (pf is not None and pa is not None) else None)
        out[abbr] = {"margin": round(margin, 3) if margin is not None else None,
                     "pf": round(pf, 2) if pf is not None else None,
                     "pa": round(pa, 2) if pa is not None else None,
                     "gp": int(gp), "w": int(w) if w is not None else None, "l": int(l) if l is not None else None}
    return out


def probable(c):
    try:
        p = (c.get("probables") or [None])[0] or c.get("probable")
        if not p: return None
        a = p.get("athlete") or p
        nm = a.get("displayName") or a.get("shortName") or a.get("name")
        era = None
        for s in (p.get("statistics") or []):
            if "era" in ((s.get("name") or s.get("abbreviation") or "").lower()):
                era = s.get("displayValue")
        return {"name": nm, "era": era} if nm else None
    except Exception:
        return None


def fetch_today(cfg):
    data = get(f"https://site.api.espn.com/apis/site/v2/sports/{cfg['path']}/scoreboard")
    out = []
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            state = comp["status"]["type"]["state"]
            home = away = None
            for c in comp["competitors"]:
                t = {"abbr": c["team"]["abbreviation"], "nick": c["team"].get("name"),
                     "name": c["team"].get("displayName") or c["team"].get("name"),
                     "pitcher": probable(c)}
                if c["homeAway"] == "home": home = t
                else: away = t
            if home and away:
                out.append({"state": state, "home": home, "away": away})
        except (KeyError, IndexError):
            continue
    return out


def fetch_injuries(cfg):
    if not cfg["injuries"]:
        return {}
    try:
        data = get(f"https://site.api.espn.com/apis/site/v2/sports/{cfg['path']}/injuries")
    except Exception:
        return {}
    out = {}
    for g in (data.get("injuries") or data.get("items") or []):
        abbr = (g.get("team") or {}).get("abbreviation") or g.get("abbreviation")
        if not abbr: continue
        lst = []
        for i in (g.get("injuries") or g.get("items") or []):
            a = i.get("athlete") or {}
            nm = a.get("displayName") or a.get("shortName")
            if nm:
                lst.append({"name": nm, "status": i.get("status") or (i.get("type") or {}).get("description") or ""})
        if lst: out[abbr] = lst
    return out


def fetch_odds(cfg, key):
    """抓國際盤：歐洲區（含 Pinnacle 銳盤）的 讓分(spreads) + 大小(totals) + 勝負(h2h)。"""
    if not key:
        return {}
    try:
        data = get(f"https://api.the-odds-api.com/v4/sports/{cfg['oddsSport']}/odds"
                   f"?apiKey={key}&regions=eu&markets=h2h,spreads,totals&oddsFormat=decimal")
    except Exception as e:
        print("  賠率抓取失敗（略過）:", e)
        return {}

    def avg(a): return sum(a) / len(a)

    out = {}
    for game in data:
        home, away = game.get("home_team"), game.get("away_team")
        h2h_prices = {}
        spread_home = []   # (book, 主隊讓分點)
        totals = []        # (book, 大小分線)
        for bk in game.get("bookmakers", []):
            bkey = bk.get("key")
            for m in bk.get("markets", []):
                if m["key"] == "h2h":
                    for o in m["outcomes"]:
                        h2h_prices.setdefault(o["name"], []).append(o["price"])
                elif m["key"] == "spreads":
                    for o in m["outcomes"]:
                        if o["name"] == home and o.get("point") is not None:
                            spread_home.append((bkey, o["point"]))
                elif m["key"] == "totals":
                    for o in m["outcomes"]:
                        if o["name"] == "Over" and o.get("point") is not None:
                            totals.append((bkey, o["point"]))
        # 勝負盤去抽水
        h2h = None
        if home in h2h_prices and away in h2h_prices:
            ih, ia = 1 / avg(h2h_prices[home]), 1 / avg(h2h_prices[away])
            h2h = {"home": ih / (ih + ia), "away": ia / (ih + ia)}
        # 讓分/大小：優先採 Pinnacle，否則取各家平均
        def pick(lst):
            if not lst: return None, None
            pinn = [v for k, v in lst if k == "pinnacle"]
            if pinn: return pinn[0], "Pinnacle"
            return avg([v for _, v in lst]), "國際均盤"
        sh, sbook = pick(spread_home)
        tt, _ = pick(totals)
        out[(away, home)] = {
            "h2h": h2h,
            "spreadHome": round(sh, 1) if sh is not None else None,
            "total": round(tt, 1) if tt is not None else None,
            "book": sbook,
        }
    return out


def match_odds(odds, away_name, home_name):
    for (oa, oh), p in odds.items():
        if (oh in home_name or home_name in oh) and (oa in away_name or away_name in oa):
            return p
    return None


def build_league(lg):
    cfg = LEAGUES[lg]
    print(f"[{lg}] 抓取中…")
    strength = fetch_strength(cfg)
    games = fetch_today(cfg)
    inj = fetch_injuries(cfg)
    odds = fetch_odds(cfg, os.environ.get("ODDS_API_KEY"))
    print(f"  隊伍實力 {len(strength)} 隊、今日 {len(games)} 場、賠率 {len(odds)} 場")

    out = []
    for g in games:
        a, h = g["away"], g["home"]
        od = match_odds(odds, a["name"], h["name"])
        out.append({
            "state": g["state"], "away": a, "home": h,
            "_s": {"a": strength.get(a["abbr"], {}), "h": strength.get(h["abbr"], {})},
            "inj": {a["abbr"]: inj.get(a["abbr"], []), h["abbr"]: inj.get(h["abbr"], [])},
            "_odds": od["h2h"] if od else None,
            "market": ({"spreadHome": od["spreadHome"], "total": od["total"], "book": od["book"]}
                       if od else None),
        })
    return out


def main():
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))   # 台灣時間
    data = {"generated": now.isoformat(timespec="minutes"), "model": "power-rating-v1"}
    for lg in LEAGUES:
        try:
            data[lg] = build_league(lg)
        except Exception as e:
            print(f"[{lg}] 失敗：{e}")
            data[lg] = []
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("已寫出 data.json："
          f"NBA {len(data.get('nba', []))} 場、MLB {len(data.get('mlb', []))} 場")


if __name__ == "__main__":
    main()
