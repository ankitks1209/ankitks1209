#!/usr/bin/env python3
"""Fetch contribution + repo data into data/contributions.json (run daily by the Action).

Needs GH_TOKEN (the Action's GITHUB_TOKEN works for public data; a classic/fine-grained
PAT in the PROFILE_TOKEN secret also counts private contributions you have made visible).
Usage: GH_TOKEN=... python scripts/fetch_data.py [login]
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PROFILE_LOGIN", "ankitks1209")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set GH_TOKEN to a GitHub token.")

REPO = "name description url stargazerCount forkCount isFork isArchived primaryLanguage{name}"
QUERY = """
query($login:String!,$from:DateTime!,$to:DateTime!){
  user(login:$login){
    name login avatarUrl(size:320)
    contributionsCollection(from:$from,to:$to){
      contributionCalendar{ totalContributions weeks{ contributionDays{ date contributionCount } } }
    }
    pinnedItems(first:6,types:REPOSITORY){ nodes{ ... on Repository{ %s } } }
    repositories(first:100,privacy:PUBLIC,ownerAffiliations:OWNER,isFork:false,
                 orderBy:{field:PUSHED_AT,direction:DESC}){ nodes{ %s } }
  }
}""" % (REPO, REPO)


def post(url, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json",
                                  "User-Agent": "profile-cards"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
res = post("https://api.github.com/graphql", {"query": QUERY, "variables": {
    "login": LOGIN, "to": now.isoformat().replace("+00:00", "Z"),
    "from": (now - dt.timedelta(days=364)).replace(hour=0, minute=0, second=0).isoformat().replace("+00:00", "Z")}})
if res.get("errors") or not res.get("data", {}).get("user"):
    sys.exit(f"GraphQL error: {res.get('errors') or 'user not found'}")
u = res["data"]["user"]


def repo(n):
    return {"name": n["name"], "description": n["description"] or "", "url": n["url"],
            "stars": n["stargazerCount"], "forks": n["forkCount"],
            "language": (n["primaryLanguage"] or {}).get("name")}


days = [{"date": d["date"], "count": d["contributionCount"]}
        for w in u["contributionsCollection"]["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
days.sort(key=lambda d: d["date"])
out = {
    "login": u["login"], "name": u["name"] or u["login"], "to": days[-1]["date"],
    "total": sum(d["count"] for d in days), "days": days,
    "pinned": [n["name"] for n in u["pinnedItems"]["nodes"] if n],
    "repos": [repo(n) for n in u["repositories"]["nodes"] if not n["isArchived"] and n["name"] != u["login"]],
}
(ROOT / "data").mkdir(exist_ok=True)
(ROOT / "data" / "contributions.json").write_text(json.dumps(out, indent=1) + "\n")

try:  # portrait for the hero card
    req = urllib.request.Request(u["avatarUrl"], headers={"User-Agent": "profile-cards"})
    with urllib.request.urlopen(req, timeout=60) as r:
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(r.headers.get_content_type(), "jpg")
        (ROOT / "assets").mkdir(exist_ok=True)
        for old in (ROOT / "assets").glob("portrait.*"):
            old.unlink()
        (ROOT / "assets" / f"portrait.{ext}").write_bytes(r.read())
except Exception as e:  # the portrait is optional
    print(f"portrait skipped: {e}", file=sys.stderr)
print(f"{len(days)} days, {out['total']} contributions, {len(out['repos'])} repos")
