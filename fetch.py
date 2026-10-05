#!/usr/bin/env python3
"""
Loxley Reddit fetcher via the Arctic Shift archive (arctic-shift.photon-reddit.com).
Runs daily on GitHub Actions. No keys needed.

- First run: every thread in urls.txt (backfill).
- Every run: searches SUBS for posts mentioning ComEd in the last 7 days, and
  re-pulls threads under 30 days old for new comments.
- Writes only NEW comments to data/runs/<date>/<thread_id>.json and lists each
  file in data/index.json. The Claude skill reads index.json and processes files.
- If Arctic Shift returns something unexpected, a sample goes to data/debug/.
"""
import json, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

API = "https://arctic-shift.photon-reddit.com/api"
SUBS = ["ChicagoSuburbs", "AskChicago", "chicago", "chicagoapartments",
        "evanston", "oakpark", "Naperville", "ComEd", "illinois"]
TERMS = ["comed", "electric bill"]
TRACK_DAYS = 30
MAX_COMMENTS = 150
UA = "loxley-research/0.4 (contact: james@bayou.energy)"
SIGNAL = re.compile(r"\$\s?\d|kwh|supplier|hourly|meter|bill|solar|budget|comed|\?", re.I)

DATA = Path("data")
S = requests.Session()
S.headers["User-Agent"] = UA

def get(path, params):
    last = None
    for i in range(4):
        try:
            r = S.get(API + path, params=params, timeout=60)
            if r.status_code == 200:
                time.sleep(1)
                return r.json()
            last = f"{r.status_code} {r.text[:200]}"
            if r.status_code in (400, 404):
                break
        except Exception as e:
            last = str(e)
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"{path} {params}: {last}")

def debug(name, obj):
    d = DATA / "debug"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(obj, indent=1)[:200000])

def ts(sec):
    return datetime.fromtimestamp(float(sec), tz=timezone.utc).strftime("%Y-%m-%d")

def items(resp):
    if isinstance(resp, dict):
        resp = resp.get("data", resp)
    return resp if isinstance(resp, list) else []

def get_post(tid):
    res = items(get("/posts/ids", {"ids": tid}))
    if not res:
        raise RuntimeError("post not in archive")
    return res[0].get("data", res[0])

def get_comments(tid):
    resp = get("/comments/tree", {"link_id": f"t3_{tid}", "limit": 9999})
    out, seen = [], set()
    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        d = node.get("data", node) if node.get("kind") in ("t1", None) or "data" in node else node
        if isinstance(d, dict) and d.get("id") and "body" in d and d["id"] not in seen:
            seen.add(d["id"]); out.append(d)
        for key in ("replies", "children"):
            r = d.get(key) if isinstance(d, dict) else None
            if isinstance(r, dict):
                walk(r.get("data", {}).get("children", r.get("children", [])))
            elif isinstance(r, list):
                walk(r)
    walk(items(resp))
    if not out and items(resp):
        debug(f"tree_{tid}", resp)
    return out

def search_posts(sub, term, after):
    for field in ("query", "title"):
        try:
            return items(get("/posts/search", {"subreddit": sub, field: term, "after": after,
                                               "limit": 100, "sort": "desc"}))
        except RuntimeError as e:
            err = e
    raise err

def build(tid, post, raw_comments):
    sub = post.get("subreddit", "")
    plink = post.get("permalink") or f"/r/{sub}/comments/{tid}/"
    thread = {"id": tid, "title": post.get("title", ""), "subreddit": sub,
              "author": post.get("author", ""), "date": ts(post["created_utc"]),
              "age_days": round((time.time() - float(post["created_utc"])) / 86400, 1),
              "score": post.get("score", 0), "num_comments": post.get("num_comments", 0),
              "closed": bool(post.get("archived") or post.get("locked")),
              "flair": post.get("author_flair_text") or "",
              "selftext": (post.get("selftext") or "")[:3000],
              "link": "https://www.reddit.com" + plink}
    comments = []
    for d in raw_comments:
        if d.get("author") in (None, "[deleted]", "AutoModerator") or d.get("body") in ("[deleted]", "[removed]"):
            continue
        clink = d.get("permalink") or f"/r/{sub}/comments/{tid}/comment/{d['id']}/"
        comments.append({"id": d["id"], "author": d["author"], "date": ts(d["created_utc"]),
                         "score": d.get("score", 0), "is_op": d["author"] == thread["author"],
                         "body": (d.get("body") or "").strip()[:700],
                         "link": "https://www.reddit.com" + clink})
    return thread, comments

def main():
    DATA.mkdir(exist_ok=True)
    sp, ip = DATA / "state.json", DATA / "index.json"
    state = json.loads(sp.read_text()) if sp.exists() else {"threads": {}}
    index = json.loads(ip.read_text()) if ip.exists() else {"files": [], "runs": []}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_dir = DATA / "runs" / today
    errors, targets = [], set()

    for l in open("urls.txt"):
        m = re.search(r"/comments/([a-z0-9]+)", l)
        if m and not l.startswith("#") and m.group(1) not in state["threads"]:
            targets.add(m.group(1))

    after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    for sub in SUBS:
        for term in TERMS:
            try:
                for p in search_posts(sub, term, after):
                    p = p.get("data", p)
                    text = f"{p.get('title','')} {p.get('selftext','')}".lower()
                    if p.get("id") and ("comed" in text or ("bill" in text and "electric" in text)):
                        if p["id"] not in state["threads"]:
                            targets.add(p["id"])
            except Exception as e:
                errors.append(f"search r/{sub} '{term}': {str(e)[:150]}")

    for tid, t in state["threads"].items():
        age = (datetime.now() - datetime.strptime(t["date"], "%Y-%m-%d")).days
        if age <= TRACK_DAYS and not t.get("closed"):
            targets.add(tid)

    written = 0
    for tid in sorted(targets):
        try:
            post = get_post(tid)
            thread, comments = build(tid, post, get_comments(tid))
        except Exception as e:
            errors.append(f"fetch {tid}: {str(e)[:150]}"); continue
        seen = set(state["threads"].get(tid, {}).get("seen", []))
        new = [c for c in comments if c["id"] not in seen]
        is_new_thread = tid not in state["threads"]
        state["threads"][tid] = {"date": thread["date"], "closed": thread["closed"], "title": thread["title"],
                                 "seen": sorted(seen | {c["id"] for c in comments})}
        if not new and not is_new_thread:
            continue
        f = run_dir / f"{tid}.json"
        if f.exists():
            prev = json.loads(f.read_text())
            new = prev["comments"] + new
            index["files"] = [x for x in index["files"] if x["file"] != str(f)]
        keep = sorted(new, key=lambda c: (not c["is_op"], not SIGNAL.search(c["body"]), -c["score"]))[:MAX_COMMENTS]
        run_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"thread": thread, "new_thread": is_new_thread,
                                 "new_comment_count": len(new), "comments": keep}, indent=1))
        index["files"].append({"file": str(f), "run": today, "thread_id": tid, "title": thread["title"],
                               "new_thread": is_new_thread, "comments": len(keep),
                               "fresh": thread["age_days"] < 7 and not thread["closed"]})
        written += 1
    index["runs"].append({"run": today, "threads_checked": len(targets), "files": written, "errors": errors})
    index["runs"] = index["runs"][-60:]
    sp.write_text(json.dumps(state))
    ip.write_text(json.dumps(index, indent=1))
    print(json.dumps(index["runs"][-1], indent=1))

if __name__ == "__main__":
    main()
