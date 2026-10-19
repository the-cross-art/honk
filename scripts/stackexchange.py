"""Pull real Kubernetes failure reports from the official Stack Exchange API.

Uses https://api.stackexchange.com (free: 300 requests/day per IP, 10,000 with a
key from stackapps.com in $STACKEXCHANGE_KEY). It obeys the API's `backoff`
field, waits between calls, and caches every response, so a rerun costs nothing.

Stack Overflow content is CC BY-SA 4.0. The cache stays out of git
(`scenarios/.cache/`); scenarios link to the source and paraphrase it.

    python scripts/stackexchange.py            # fetch every query below
    python scripts/stackexchange.py --list     # show what is cached
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.stackexchange.com/2.3"
CACHE = Path(__file__).resolve().parent.parent / "scenarios" / ".cache"
PAUSE_SECONDS = 1.0

QUERIES: dict[str, str] = {
    "oom": "OOMKilled",
    "exit-137": "exit code 137",
    "liveness": "liveness probe failed restart",
    "readiness": "readiness probe failed",
    "config-error": "CreateContainerConfigError",
    "image-pull": "ImagePullBackOff",
    "exec-format": "exec format error",
    "insufficient-cpu": "Insufficient cpu",
    "pvc-unbound": "unbound immediate PersistentVolumeClaims",
    "evicted": "Evicted DiskPressure",
    "init-crash": "Init:CrashLoopBackOff",
    "rollout-undo": "rollout undo",
    "cpu-throttling": "cpu throttling",
    "crashloop": "CrashLoopBackOff",
}

_next_call = 0.0


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    global _next_call
    params = {**params, "site": "stackoverflow"}
    if key := os.environ.get("STACKEXCHANGE_KEY"):
        params["key"] = key
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    cached = CACHE / "raw" / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
    if cached.exists():
        return dict(json.loads(cached.read_text()))

    wait = _next_call - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip",
                                               "User-Agent": "honk-scenarios/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    data = json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)
    if "error_id" in data:
        raise SystemExit(f"API error {data['error_id']}: {data.get('error_message')}")
    _next_call = time.monotonic() + PAUSE_SECONDS + float(data.get("backoff") or 0)
    if data.get("quota_remaining", 1) < 10:
        raise SystemExit(f"quota nearly spent ({data['quota_remaining']} left); stop for today")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data))
    return dict(data)


def fetch(per_query: int) -> None:
    out = CACHE / "questions.jsonl"
    CACHE.mkdir(parents=True, exist_ok=True)
    seen: set[int] = set()
    with out.open("w", encoding="utf-8") as fh:
        for topic, q in QUERIES.items():
            res = _get("/search/advanced", {
                "order": "desc", "sort": "votes", "tagged": "kubernetes", "q": q,
                "accepted": "True", "pagesize": per_query, "filter": "withbody"})
            questions = [it for it in res.get("items", []) if it["question_id"] not in seen]
            ids = [str(it["accepted_answer_id"]) for it in questions
                   if it.get("accepted_answer_id")]
            answers: dict[int, Any] = {}
            if ids:
                ans = _get(f"/answers/{';'.join(ids)}", {"filter": "withbody"})
                answers = {a["answer_id"]: a for a in ans.get("items", [])}
            for it in questions:
                seen.add(it["question_id"])
                accepted = answers.get(it.get("accepted_answer_id"), {})
                fh.write(json.dumps({
                    "topic": topic, "link": it["link"], "score": it["score"],
                    "title": html.unescape(it["title"]), "question": it.get("body", ""),
                    "accepted_answer": accepted.get("body", ""),
                    "answer_score": accepted.get("score"),
                }) + "\n")
            print(f"{topic:<17} {len(questions)} questions", flush=True)
    print(f"wrote {out}")


def show() -> None:
    path = CACHE / "questions.jsonl"
    if not path.exists():
        sys.exit("nothing cached yet")
    for line in path.read_text().splitlines():
        r = json.loads(line)
        print(f"{r['topic']:<17} {r['score']:>4}  {r['title'][:70]}  {r['link']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--per-query", type=int, default=6)
    args = parser.parse_args()
    show() if args.list else fetch(args.per_query)
