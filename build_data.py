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
    "nba": {"path": "basketball/nba", "homeAdv": 2.5,  "sigma": 12.0, "shrink": 6,  "oddsSport": "basketball_nba", "injuries": True},
    "mlb": {"path": "baseball/mlb",   "homeAdv": 0.25, "sigma": 4.5,  "shrink": 20, "oddsSport": "baseball_mlb",   "injuries": False},
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
        era_num = None
        if era is not None:
            try: era_num = float(str(era).split()[0])
            except (TypeError, ValueError): era_num = None
        return {"name": nm, "era": era, "eraNum": era_num} if nm else None
    except Exception:
        return None


def fetch_today(cfg, date=None):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{cfg['path']}/scoreboard"
    if date:
        url += f"?dates={date}"
    data = get(url)
    out = []
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            state = comp["status"]["type"]["state"]
            game_date = (ev.get("date") or "")[:10]   # YYYY-MM-DD
            home = away = None
            for c in comp["competitors"]:
                t = {"abbr": c["team"]["abbreviation"], "nick": c["team"].get("name"),
                     "name": c["team"].get("displayName") or c["team"].get("name"),
                     "pitcher": probable(c)}
                if c["homeAway"] == "home": home = t
                else: away = t
            if home and away:
                if state == "post":     # 已結束的不預測
                    continue
                out.append({"id": ev.get("id"), "state": state, "home": home, "away": away,
                            "date": game_date, "start": ev.get("date")})
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


def adjust_for_pitchers(out):
    """MLB：用先發投手 ERA 修正當日球隊實力。
    基準＝今日所有先發的平均 ERA；某隊先發比平均強（ERA 低）→ 該隊 margin 往上加，
    幅度有上限避免被極端小樣本 ERA 灌爆。抓不到 ERA 的那一隊不調整。"""
    eras = []
    for item in out:
        for side in ("away", "home"):
            p = item[side].get("pitcher")
            if p and p.get("eraNum") is not None:
                eras.append(p["eraNum"])
    base = (sum(eras) / len(eras)) if len(eras) >= 4 else 4.10   # 今日先發平均，太少就用聯盟概值
    K, CAP = 0.55, 1.5                                            # 換算係數與單場上限（單位：失分/場）
    for item in out:
        for skey, side in (("a", "away"), ("h", "home")):
            p = item[side].get("pitcher")
            s = item["_s"].get(skey)
            if p and p.get("eraNum") is not None and s and s.get("margin") is not None:
                adj = max(-CAP, min(CAP, (base - p["eraNum"]) * K))
                s["margin"] = round(s["margin"] + adj, 3)
                s["pAdj"] = round(adj, 2)
                if p.get("era"):
                    p["era"] = f"{p['era']}・已計入"   # 在網站先發那行標示已納入


def build_league(lg):
    cfg = LEAGUES[lg]
    print(f"[{lg}] 抓取中…")
    strength = fetch_strength(cfg)
    games = []
    seen = set()
    for d in (et_date(0), et_date(1)):          # 今天 + 明天（美東日期）
        for g in fetch_today(cfg, d):
            k = (g["away"]["abbr"], g["home"]["abbr"], g.get("start"))   # 用開賽時間去重（系列賽不會誤殺）
            if k in seen:
                continue
            seen.add(k)
            games.append(g)
    games.sort(key=lambda g: g.get("start") or "")   # 依開賽時間排序
    inj = fetch_injuries(cfg)
    odds = fetch_odds(cfg, os.environ.get("ODDS_API_KEY"))
    print(f"  隊伍實力 {len(strength)} 隊、未開打 {len(games)} 場、賠率 {len(odds)} 場")

    out = []
    for g in games:
        a, h = g["away"], g["home"]
        od = match_odds(odds, a["name"], h["name"])
        out.append({
            "id": g.get("id"), "state": g["state"], "date": g.get("date"), "start": g.get("start"),
            "away": a, "home": h,
            "_s": {"a": strength.get(a["abbr"], {}), "h": strength.get(h["abbr"], {})},
            "inj": {a["abbr"]: inj.get(a["abbr"], []), h["abbr"]: inj.get(h["abbr"], [])},
            "_odds": od["h2h"] if od else None,
            "market": ({"spreadHome": od["spreadHome"], "total": od["total"], "book": od["book"]}
                       if od else None),
        })

    if lg == "mlb":
        adjust_for_pitchers(out)   # 先發投手要在算 pick 之前先修正好 margin
    return out


# ============ 預測數學（與網站 index.html 算法一致）============
def et_date(offset=0):
    et = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4) + dt.timedelta(days=offset)
    return et.strftime("%Y%m%d")


def _normcdf(x):
    import math
    return 0.5 * (1 + math.erf(x / (2 ** 0.5)))


def _shrunk(s, cfg):
    if not s or s.get("margin") is None:
        return None
    gp = s.get("gp") or 1
    return s["margin"] * (gp / (gp + cfg["shrink"]))


def compute_pick(game, cfg):
    """重現網站的算法：算出勝率、預測分差、看好方、edge、讓分傾向。"""
    mA = _shrunk(game["_s"].get("a"), cfg)
    mH = _shrunk(game["_s"].get("h"), cfg)
    if mA is None or mH is None:
        return None
    pred = (mH - mA) + cfg["homeAdv"]
    p_home = _normcdf(pred / cfg["sigma"])
    pick_su = "home" if p_home >= 0.5 else "away"
    edge = edge_side = None
    od = game.get("_odds")
    if od:
        eH, eA = p_home - od["home"], (1 - p_home) - od["away"]
        edge, edge_side = (eH, "home") if eH >= eA else (eA, "away")
    spread_home = ats = None
    mk = game.get("market")
    if mk and mk.get("spreadHome") is not None:
        spread_home = mk["spreadHome"]
        ats = "home" if (pred + spread_home) >= 0 else "away"
    return {"pHome": round(p_home, 4), "predMargin": round(pred, 3), "pickSU": pick_su,
            "edge": round(edge, 4) if edge is not None else None, "edgeSide": edge_side,
            "isValue": bool(edge is not None and edge >= 0.03),
            "spreadHome": spread_home, "atsLean": ats}


# ============ 結果抓取與戰績對帳 ============
def fetch_results(cfg, date):
    """抓某天『已結束』的比賽與比分（用來跟之前的預測對帳）。"""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{cfg['path']}/scoreboard?dates={date}"
    try:
        data = get(url)
    except Exception:
        return []
    out = []
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            if comp["status"]["type"]["state"] != "post":
                continue
            hs = as_ = None
            for c in comp["competitors"]:
                sc = c.get("score")
                sc = int(sc) if sc not in (None, "") else None
                if c["homeAway"] == "home": hs = sc
                else: as_ = sc
            if hs is not None and as_ is not None:
                out.append({"id": ev.get("id"), "home_score": hs, "away_score": as_})
        except (KeyError, IndexError, ValueError):
            continue
    return out


def load_history():
    # 歷史紀錄直接存在 data.json 裡（這樣 GitHub 只要 commit data.json 即可，不用改 yml）
    if os.path.exists("data.json"):
        try:
            with open("data.json", encoding="utf-8") as f:
                return json.load(f).get("_history", {}) or {}
        except Exception:
            return {}
    return {}


def cap_history(hist, n=400):
    items = sorted(hist.items(), key=lambda kv: (kv[1].get("date") or ""), reverse=True)
    return dict(items[:n])


def log_predictions(lg, cfg, out, hist):
    """把未開打場次的預測記下來（之後比對用）。已記過的不重複。"""
    for g in out:
        if not g.get("id"):
            continue
        key = f"{lg}:{g['id']}"
        if key in hist:
            continue
        pick = compute_pick(g, cfg)
        if not pick:
            continue
        rec = {"lg": lg, "date": g.get("date"), "start": g.get("start"),
               "away": g["away"]["abbr"], "home": g["home"]["abbr"], "graded": False}
        rec.update(pick)
        hist[key] = rec


def grade_predictions(lg, cfg, hist):
    """抓昨天/今天的結果，幫還沒對帳的預測打分。"""
    results = {}
    for d in (et_date(-1), et_date(0)):
        for r in fetch_results(cfg, d):
            if r.get("id"):
                results[f"{lg}:{r['id']}"] = r
    graded_now = 0
    for key, h in hist.items():
        if h.get("graded") or key not in results:
            continue
        r = results[key]
        hs, as_ = r["home_score"], r["away_score"]
        winner = "home" if hs > as_ else "away"
        h["homeScore"], h["awayScore"], h["winner"] = hs, as_, winner
        h["suHit"] = (h.get("pickSU") == winner)
        # 讓分對帳
        if h.get("spreadHome") is not None and h.get("atsLean"):
            cover = (hs - as_) + h["spreadHome"]
            if abs(cover) < 1e-9:
                h["atsHit"] = None            # 剛好打和（push）
            else:
                home_covered = cover > 0
                h["atsHit"] = (h["atsLean"] == "home") == home_covered
        else:
            h["atsHit"] = None
        # 值得下注對帳
        h["valueHit"] = (h["edgeSide"] == winner) if (h.get("isValue") and h.get("edgeSide")) else None
        h["graded"] = True
        graded_now += 1
    if graded_now:
        print(f"  [{lg}] 對帳了 {graded_now} 場結果")


def summarize(hist):
    su = [0, 0]; val = [0, 0]; ats = [0, 0]
    for h in hist.values():
        if not h.get("graded"):
            continue
        if h.get("suHit") is True: su[0] += 1
        elif h.get("suHit") is False: su[1] += 1
        if h.get("valueHit") is True: val[0] += 1
        elif h.get("valueHit") is False: val[1] += 1
        if h.get("atsHit") is True: ats[0] += 1
        elif h.get("atsHit") is False: ats[1] += 1
    return {"su": su, "value": val, "ats": ats}


def recent_graded(hist, n=12):
    graded = [h for h in hist.values() if h.get("graded")]
    graded.sort(key=lambda h: (h.get("date") or "", h.get("home") or ""), reverse=True)
    keys = ("lg", "date", "start", "away", "home", "awayScore", "homeScore", "winner",
            "pickSU", "pHome", "suHit", "isValue", "edge", "edgeSide", "valueHit",
            "spreadHome", "atsLean", "atsHit")
    return [{k: h.get(k) for k in keys} for h in graded[:n]]



def main():
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))   # 台灣時間
    hist = load_history()
    data = {"generated": now.isoformat(timespec="minutes"), "model": "power-rating+pitcher-v2"}
    for lg in LEAGUES:
        cfg = LEAGUES[lg]
        try:
            grade_predictions(lg, cfg, hist)     # 1) 先對帳昨天/今天已結束的
            out = build_league(lg)               # 2) 抓今天+明天未開打的
            log_predictions(lg, cfg, out, hist)  # 3) 把新預測記下來
            data[lg] = out
        except Exception as e:
            print(f"[{lg}] 失敗：{e}")
            data[lg] = []
    hist = cap_history(hist)
    data["stats"] = summarize(hist)
    data["recent"] = recent_graded(hist, 60)
    data["_history"] = hist
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    s = data["stats"]
    print(f"已寫出 data.json：NBA {len(data.get('nba', []))} 場、MLB {len(data.get('mlb', []))} 場")
    print(f"戰績：看好方 {s['su'][0]}-{s['su'][1]}、值得下注 {s['value'][0]}-{s['value'][1]}、讓分 {s['ats'][0]}-{s['ats'][1]}")


if __name__ == "__main__":
    main()
