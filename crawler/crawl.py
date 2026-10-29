#!/usr/bin/env python3
"""Clash of Clans clan tracker crawler.

Pulls members, ranked league performance, regular wars and CWL wars for the
clans in clans.json and writes JSON "tabs" into docs/data/ for the Pages site.
Wars are ephemeral in the API, so every war seen is accumulated in
data/state.json and never dropped.

Env vars:
  COC_API_TOKEN  (required) developer.clashofclans.com key
  COC_API_BASE   (optional) defaults to https://api.clashofclans.com/v1
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "https://api.clashofclans.com/v1"


# --------------------------------------------------------------------------- API
class ApiError(Exception):
    def __init__(self, code, reason, path):
        super().__init__(f"{code} {reason} on {path}")
        self.code, self.reason, self.path = code, reason, path


def enc(tag):
    tag = tag if tag.startswith("#") else "#" + tag
    return urllib.parse.quote(tag, safe="")


class Api:
    def __init__(self, token, base=DEFAULT_BASE):
        self.token, self.base = token, base.rstrip("/")

    def get(self, path, params=None):
        """GET json. Returns None on 404. Raises ApiError otherwise."""
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(5):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read())
                    reason = body.get("reason", "")
                    if body.get("message"):
                        reason += f" ({body['message']})"
                except Exception:
                    reason = ""
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(e.code, reason, path)
            except (urllib.error.URLError, TimeoutError):
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(0, "network", path)


# ------------------------------------------------------------------------ helpers
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def coc_time(s):
    """'20261028T120000.000Z' -> '2026-10-28T12:00:00Z'"""
    if not s:
        return ""
    try:
        return datetime.strptime(s[:15], "%Y%m%dT%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False))


def th_sort(rows, th="th", *tie):
    """Town hall high -> low, then tie-breakers (descending numbers), then name."""
    def key(r):
        return (-(r.get(th) or 0),) + tuple(-(r.get(t) or 0) for t in tie) + ((r.get("name") or "").lower(),)
    return sorted(rows, key=key)


# ---------------------------------------------------------------- members/players
def crawl_members(api, clan_tags, players_db, errors):
    """Refresh roster. players_db: tag -> record, kept forever (status active/left)."""
    stamp = now_iso()
    active_tags = set()
    clans_meta = []
    tag_to_clan = {}

    for ctag in clan_tags:
        data = api.get(f"/clans/{enc(ctag)}")
        if not data:
            errors.append(f"clan {ctag} not found")
            continue
        clans_meta.append({
            "tag": data["tag"], "name": data.get("name", ""), "members": data.get("members", 0),
            "level": data.get("clanLevel"), "warLeague": (data.get("warLeague") or {}).get("name", ""),
            "warWins": data.get("warWins"), "isWarLogPublic": data.get("isWarLogPublic"),
        })
        for m in data.get("memberList", []):
            active_tags.add(m["tag"])
            tag_to_clan[m["tag"]] = (data["tag"], data.get("name", ""), m)

    def fetch_player(tag):
        try:
            return tag, api.get(f"/players/{enc(tag)}")
        except ApiError as e:
            errors.append(str(e))
            return tag, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(fetch_player, sorted(active_tags)))

    fresh = {}
    for tag, p in results:
        ctag, cname, m = tag_to_clan[tag]
        old = players_db.get(tag, {})
        rec = {
            "tag": tag, "name": (p or m).get("name", old.get("name", "")),
            "clanTag": ctag, "clan": cname, "role": m.get("role", ""),
            "th": (p or {}).get("townHallLevel", old.get("th", 0)),
            "expLevel": (p or m).get("expLevel"),
            "trophies": (p or m).get("trophies"),
            "bestTrophies": (p or {}).get("bestTrophies"),
            "league": ((p or {}).get("leagueTier") or m.get("leagueTier") or {}).get("name", ""),
            "warStars": (p or {}).get("warStars"),
            "attackWins": (p or {}).get("attackWins"),
            "donations": m.get("donations"), "donationsReceived": m.get("donationsReceived"),
            "warPreference": (p or {}).get("warPreference", ""),
            "capitalContributions": (p or {}).get("clanCapitalContributions"),
            "firstSeen": old.get("firstSeen", stamp), "lastSeen": stamp, "status": "active",
        }
        players_db[tag] = rec
        fresh[tag] = (rec, p)

    for tag, rec in players_db.items():
        if tag not in active_tags and rec.get("status") == "active":
            rec["status"] = "left"
    return clans_meta, fresh


# ----------------------------------------------------------------------- ranked
def scalar_fields(d):
    return {k: v for k, v in (d or {}).items() if isinstance(v, (int, float, str, bool))}


def ranked_row(rec, label, group_tag, season_id, data):
    members = data.get("members") or []
    me = next((m for m in members if m.get("tag") == rec["tag"]), {})
    atk = data.get("attackLogs") or []
    dfn = data.get("defenseLogs") or []
    a_stars = sum(e.get("stars", 0) or 0 for e in atk)
    d_stars = sum(e.get("stars", 0) or 0 for e in dfn)
    a_destr = [e.get("destructionPercentage") for e in atk if e.get("destructionPercentage") is not None]
    return {
        "clan": rec["clan"], "clanTag": rec["clanTag"], "name": rec["name"], "tag": rec["tag"],
        "th": rec["th"], "season": label, "seasonId": str(season_id), "groupTag": group_tag,
        "tier": rec.get("league", ""),
        "rankedTrophies": me.get("trophies"), "rank": me.get("rank"),
        "attacks": len(atk), "attackStars": a_stars,
        "attackAvgDestruction": round(sum(a_destr) / len(a_destr), 1) if a_destr else None,
        "threeStars": sum(1 for e in atk if e.get("stars") == 3),
        "defenses": len(dfn), "defenseStars": d_stars,
        "playerFields": scalar_fields(me),
    }


def crawl_ranked(api, fresh, errors, sample_path):
    jobs = []
    for tag, (rec, p) in fresh.items():
        if not p:
            continue
        for label, g, s in (("current", p.get("currentLeagueGroupTag"), p.get("currentLeagueSeasonId")),
                            ("previous", p.get("previousLeagueGroupTag"), p.get("previousLeagueSeasonId"))):
            if g and s:
                jobs.append((rec, label, g, s))

    def run(job):
        rec, label, g, s = job
        try:
            data = api.get(f"/leaguegroup/{urllib.parse.quote(str(g), safe='')}/{s}", {"playerTag": rec["tag"]})
        except ApiError as e:
            errors.append(str(e))
            return None
        if not data:  # 404: group not available for this player/season
            return None
        try:
            return ranked_row(rec, label, g, s, data), data
        except Exception as e:  # never let one odd response kill the crawl
            errors.append(f"ranked parse failed for {rec['tag']} ({label}): {e!r}")
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        out = [r for r in ex.map(run, jobs) if r]
    # Keep one raw response so the response shape can be inspected if fields look empty.
    if out and not Path(sample_path).exists():
        save(sample_path, out[0][1])
    return [r for r, _ in out]


# -------------------------------------------------------------------------- wars
def norm_war(w, our_tag, key, typ):
    """Normalise a war payload from our clan's point of view."""
    if w["clan"]["tag"] == our_tag:
        us, them = w["clan"], w["opponent"]
    elif w["opponent"]["tag"] == our_tag:
        us, them = w["opponent"], w["clan"]
    else:
        return None
    state = w.get("state", "")
    result = ""
    if state == "warEnded":
        a, b = us.get("stars", 0), them.get("stars", 0)
        if a == b:
            a, b = us.get("destructionPercentage", 0), them.get("destructionPercentage", 0)
        result = "win" if a > b else "loss" if a < b else "tie"
    per = w.get("attacksPerMember") or (1 if typ == "cwl" else 2)
    members = []
    for m in us.get("members", []):
        members.append({
            "tag": m["tag"], "name": m.get("name", ""), "th": m.get("townhallLevel", 0),
            "pos": m.get("mapPosition"),
            "attacks": [{"stars": a.get("stars", 0), "destruction": a.get("destructionPercentage", 0),
                         "order": a.get("order"), "defenderTag": a.get("defenderTag")}
                        for a in (m.get("attacks") or [])],
            "timesAttacked": m.get("opponentAttacks", 0),
            "defStars": (m.get("bestOpponentAttack") or {}).get("stars"),
            "defDestruction": (m.get("bestOpponentAttack") or {}).get("destructionPercentage"),
        })
    return {
        "key": key, "type": typ, "clanTag": our_tag, "clan": us.get("name", ""),
        "opponent": them.get("name", ""), "opponentTag": them.get("tag", ""),
        "state": state, "result": result, "teamSize": w.get("teamSize"), "attacksPerMember": per,
        "startTime": coc_time(w.get("startTime")), "endTime": coc_time(w.get("endTime")),
        "stars": us.get("stars"), "destruction": us.get("destructionPercentage"),
        "opponentStars": them.get("stars"), "opponentDestruction": them.get("destructionPercentage"),
        "members": members,
    }


def crawl_wars(api, clan_tags, state, errors):
    wars = state.setdefault("wars", {})
    cwl = state.setdefault("cwl", {})
    skip_tags = set(state.setdefault("cwlOtherWarTags", []))

    for ctag in clan_tags:
        ctag = ctag if ctag.startswith("#") else "#" + ctag
        # ---- regular war
        try:
            w = api.get(f"/clans/{enc(ctag)}/currentwar")
        except ApiError as e:
            if e.code == 403 and "invalid" not in e.reason.lower():
                errors.append(f"{ctag}: war log is private, regular wars cannot be read")
            else:
                errors.append(str(e))
            w = None
        if w and w.get("state") in ("inWar", "warEnded"):
            st = w.get("startTime") or w.get("preparationStartTime")
            rec = norm_war(w, ctag, f"{ctag}:{st}", "war")
            if rec:
                wars[rec["key"]] = rec

        # ---- CWL
        try:
            group = api.get(f"/clans/{enc(ctag)}/currentwar/leaguegroup")
        except ApiError as e:
            errors.append(str(e))
            group = None
        if not group:
            continue
        season = group.get("season", "")
        for rnd in group.get("rounds", []):
            for wt in rnd.get("warTags", []):
                if wt == "#0" or wt in skip_tags:
                    continue
                key = f"{wt}:{ctag}"
                if key in cwl and cwl[key]["state"] == "warEnded":
                    continue
                try:
                    w = api.get(f"/clanwarleagues/wars/{enc(wt)}")
                except ApiError as e:
                    errors.append(str(e))
                    continue
                if not w:
                    continue
                rec = norm_war(w, ctag, key, "cwl")
                if rec is None:
                    skip_tags.add(wt)
                elif rec["state"] in ("inWar", "warEnded"):
                    rec["season"] = season
                    cwl[key] = rec
    state["cwlOtherWarTags"] = sorted(skip_tags)


def war_rows(war_map, players_db):
    rows = []
    for w in war_map.values():
        ended = w["state"] == "warEnded"
        for m in w["members"]:
            used = len(m["attacks"])
            stars = sum(a["stars"] for a in m["attacks"])
            avg_d = round(sum(a["destruction"] for a in m["attacks"]) / used, 1) if used else None
            rows.append({
                "date": w["startTime"], "season": w.get("season", ""), "clan": w["clan"], "clanTag": w["clanTag"],
                "opponent": w["opponent"], "state": w["state"], "result": w["result"],
                "name": m["name"], "tag": m["tag"], "th": m["th"], "pos": m["pos"],
                "attacksUsed": used, "attacksAvailable": w["attacksPerMember"],
                "missed": (w["attacksPerMember"] - used) if ended else None,
                "stars": stars, "avgDestruction": avg_d,
                "attackDetail": ", ".join(f'{a["stars"]}★ {a["destruction"]}%' for a in m["attacks"]),
                "timesAttacked": m["timesAttacked"], "defStars": m["defStars"], "defDestruction": m["defDestruction"],
            })
    def digits(s):
        return int("".join(ch for ch in (s or "") if ch.isdigit()) or 0)

    # town hall high -> low, newest war first, then name
    rows.sort(key=lambda r: (-(r["th"] or 0), -digits(r["date"]), r["name"].lower()))
    return rows


def war_summary(rows):
    by = {}
    for r in rows:
        if r["state"] != "warEnded":
            continue
        s = by.setdefault(r["tag"], {
            "name": r["name"], "tag": r["tag"], "clan": r["clan"], "th": r["th"], "_last": r["date"],
            "wars": 0, "attacksUsed": 0, "attacksAvailable": 0, "missed": 0, "stars": 0, "threeStars": 0, "_destr": [],
        })
        if r["date"] >= s["_last"]:
            s.update(name=r["name"], clan=r["clan"], th=r["th"], _last=r["date"])
        s["wars"] += 1
        s["attacksUsed"] += r["attacksUsed"]
        s["attacksAvailable"] += r["attacksAvailable"]
        s["missed"] += r["missed"] or 0
        s["stars"] += r["stars"]
        s["threeStars"] += r["attackDetail"].count("3★")
        if r["avgDestruction"] is not None:
            s["_destr"].extend([r["avgDestruction"]] * r["attacksUsed"])
    out = []
    for s in by.values():
        d = s.pop("_destr")
        s.pop("_last")
        s["avgStarsPerAttack"] = round(s["stars"] / s["attacksUsed"], 2) if s["attacksUsed"] else None
        s["avgDestruction"] = round(sum(d) / len(d), 1) if d else None
        out.append(s)
    return th_sort(out, "th", "stars")


# -------------------------------------------------------------------------- main
def run(api, clan_tags, out_dir, state_path):
    errors = []
    out_dir = Path(out_dir)
    state = load(state_path, {})
    players_db = state.setdefault("players", {})

    clans_meta, fresh = crawl_members(api, clan_tags, players_db, errors)
    if not clans_meta:
        raise SystemExit("No clan could be fetched; refusing to overwrite data. Errors: " + "; ".join(errors))

    ranked = crawl_ranked(api, fresh, errors, out_dir / "ranked_sample.json")
    crawl_wars(api, clan_tags, state, errors)

    members = th_sort(list(players_db.values()), "th", "trophies")
    wars = war_rows({k: v for k, v in state["wars"].items()}, players_db)
    cwl = war_rows(state["cwl"], players_db)

    save(out_dir / "members.json", members)
    save(out_dir / "ranked.json", th_sort(ranked, "th", "rankedTrophies"))
    save(out_dir / "wars.json", wars)
    save(out_dir / "cwl.json", cwl)
    save(out_dir / "war_summary.json", war_summary(wars))
    save(out_dir / "cwl_summary.json", war_summary(cwl))
    save(out_dir / "meta.json", {"updatedAt": now_iso(), "clans": clans_meta, "errors": sorted(set(errors))[:50]})
    save(state_path, state)
    return errors


def main():
    token = os.environ.get("COC_API_TOKEN")
    if not token:
        sys.exit("COC_API_TOKEN is not set")
    api = Api(token, os.environ.get("COC_API_BASE", DEFAULT_BASE))
    print("Using API base:", api.base, "| token length:", len(token))
    clans = json.loads((ROOT / "clans.json").read_text())
    errors = run(api, clans, ROOT / "docs" / "data", ROOT / "data" / "state.json")
    for e in sorted(set(errors)):
        print("warning:", e, file=sys.stderr)
    print("done")


if __name__ == "__main__":
    main()
