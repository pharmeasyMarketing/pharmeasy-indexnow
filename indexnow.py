#!/usr/bin/env python3
"""
PharmEasy -> IndexNow submitter.

Reads PharmEasy's sitemap tree, works out which URLs have actually changed
since the last run, and pushes only those to IndexNow (Bing/Copilot, Yandex,
Seznam, Naver, and the AI search engines that consume the protocol).

Standard library only -- no third-party dependencies on purpose.

See README.md for the runbook. Design notes live in the approved plan.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Constants / exit codes
# --------------------------------------------------------------------------- #

EXIT_OK = 0
EXIT_CIRCUIT_BREAKER = 2   # candidate set implausibly large -> aborted
EXIT_HARD_FAILURE = 3      # 403/422 -> setup is broken (key file / host)
EXIT_CONFIG = 4            # bad invocation / missing config or key

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(msg, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().replace(microsecond=0).isoformat()


def parse_lastmod(value: str | None) -> float:
    """Return a sortable/comparable epoch for a <lastmod> string.

    Returns 0.0 for anything missing or unparseable so such URLs sort oldest
    and never win a 'newer than' comparison on their own.
    """
    if not value:
        return 0.0
    s = value.strip()
    if not s:
        return 0.0
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Date-only fallback (YYYY-MM-DD)
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def leaf_id(url: str) -> str:
    """Stable filename-safe id for a leaf sitemap URL (for cache files)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Http:
    def __init__(self, user_agent: str, timeout: int, retries: int, backoff: int):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def _open(self, req: urllib.request.Request):
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                # 4xx (except 429) are not worth retrying; surface immediately.
                if e.code != 429 and 400 <= e.code < 500:
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        assert last_err is not None
        raise last_err

    def head(self, url: str) -> dict:
        """Return {'status', 'etag', 'last_modified'} without downloading body."""
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": self.user_agent,
        })
        try:
            with self._open(req) as resp:
                return {
                    "status": resp.status,
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                }
        except urllib.error.HTTPError as e:
            return {"status": e.code, "etag": None, "last_modified": None}

    def get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip",
        })
        with self._open(req) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return raw

    def post_json(self, url: str, payload: dict) -> int:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "User-Agent": self.user_agent,
            "Content-Type": "application/json; charset=utf-8",
        })
        try:
            with self._open(req) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                raise RateLimited(retry_after) from e
            return e.code


class RateLimited(Exception):
    def __init__(self, retry_after: str | None):
        super().__init__(f"429 Too Many Requests (Retry-After={retry_after})")
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Sitemap parsing / discovery
# --------------------------------------------------------------------------- #

def parse_sitemap(xml_bytes: bytes) -> tuple[str, list]:
    """Parse a sitemap document.

    Returns ("index", [child_url, ...]) for a <sitemapindex>, or
    ("urlset", [(loc, lastmod_or_None), ...]) for a <urlset>.
    """
    root = ET.fromstring(xml_bytes)
    tag = root.tag.split("}")[-1]  # strip namespace
    if tag == "sitemapindex":
        children = []
        for sm in root.findall(f"{SITEMAP_NS}sitemap"):
            loc = sm.findtext(f"{SITEMAP_NS}loc")
            if loc:
                children.append(loc.strip())
        return "index", children
    if tag == "urlset":
        urls = []
        for u in root.findall(f"{SITEMAP_NS}url"):
            loc = u.findtext(f"{SITEMAP_NS}loc")
            if not loc:
                continue
            lastmod = u.findtext(f"{SITEMAP_NS}lastmod")
            urls.append((loc.strip(), lastmod.strip() if lastmod else None))
        return "urlset", urls
    raise ValueError(f"Unexpected sitemap root element: <{tag}>")


def discover_leaves(http: Http, roots: list[str], warnings: list[str]) -> list[str]:
    """Walk sitemap indexes down to leaf (urlset) sitemaps.

    A broken/erroring index is logged as a warning and skipped rather than
    aborting the whole run.
    """
    leaves: list[str] = []
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            body = http.get(url)
            kind, items = parse_sitemap(body)
        except Exception as e:  # noqa: BLE001 - report, don't crash
            warnings.append(f"discover: {url} -> {type(e).__name__}: {e}")
            continue
        if kind == "index":
            for child in items:
                if child not in seen:
                    queue.append(child)
        else:
            leaves.append(url)
    return leaves


# --------------------------------------------------------------------------- #
# URL hygiene
# --------------------------------------------------------------------------- #

def _rule_to_predicate(rule: str):
    """Compile a robots.txt-style Disallow rule into a path matcher."""
    rule = rule.rstrip("*")
    if "*" in rule:
        head, _, tail = rule.partition("*")
        return lambda p: p.startswith(head) and tail in p[len(head):]
    return lambda p: p.startswith(rule)


def build_hygiene(host: str, disallow: list[str]):
    prefix = f"https://{host}/"
    apex = f"https://{host}"
    predicates = [_rule_to_predicate(r) for r in disallow]

    def is_submittable(url: str) -> bool:
        if not (url == apex or url.startswith(prefix)):
            return False
        path = url[len(apex):] or "/"
        if not path.startswith("/"):
            path = "/" + path
        return not any(pred(path) for pred in predicates)

    return is_submittable


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #

@dataclass
class Diff:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def all_candidates(self) -> list[str]:
        return self.new + self.changed + self.removed


def compute_diff(prev_global: dict[str, str],
                 cur_seen: dict[str, str],
                 cur_all_keys: set[str]) -> Diff:
    """Three-signal diff over already-seen leaves.

    prev_global : url -> lastmod from previously-seen leaves (cache).
    cur_seen    : url -> lastmod for the current crawl of *previously-seen* leaves.
    cur_all_keys: every url present in the current crawl (incl. first-seen leaves),
                  used only so a URL that merely moved between shards is not
                  mis-reported as removed.
    """
    prev_keys = set(prev_global)
    seen_keys = set(cur_seen)

    new = sorted(seen_keys - prev_keys)
    removed = sorted(prev_keys - cur_all_keys)

    changed = []
    for url in seen_keys & prev_keys:
        if parse_lastmod(cur_seen[url]) > parse_lastmod(prev_global[url]):
            changed.append(url)
    changed.sort()
    return Diff(new=new, changed=changed, removed=removed)


def watermark_diff(cur_seen: dict[str, str],
                   leaf_watermarks: dict[str, float],
                   leaf_of: dict[str, str]) -> Diff:
    """Fallback when URL-level cache is unavailable.

    A URL counts as changed if its lastmod is newer than the last recorded
    max-lastmod for its leaf. Cannot detect removals or new-with-stale-lastmod.
    """
    changed = []
    for url, lastmod in cur_seen.items():
        wm = leaf_watermarks.get(leaf_of.get(url, ""), 0.0)
        if parse_lastmod(lastmod) > wm:
            changed.append(url)
    changed.sort()
    return Diff(changed=changed)


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def circuit_breaker_tripped(n_candidates: int, total_urls: int, limits: dict) -> bool:
    threshold = max(int(limits["circuit_breaker_min"]),
                    int(total_urls * float(limits["circuit_breaker_fraction"])))
    return n_candidates > threshold


def select_for_submission(candidates: list[str],
                          lastmod_of: dict[str, str],
                          cap: int) -> tuple[list[str], list[str]]:
    """Most-recently-modified first; return (to_submit, deferred)."""
    ordered = sorted(candidates, key=lambda u: parse_lastmod(lastmod_of.get(u)), reverse=True)
    return ordered[:cap], ordered[cap:]


def apply_debounce(urls: list[str], ledger: dict[str, float], hours: float) -> tuple[list[str], int]:
    cutoff = now_utc().timestamp() - hours * 3600
    keep, skipped = [], 0
    for u in urls:
        if ledger.get(u, 0.0) >= cutoff:
            skipped += 1
        else:
            keep.append(u)
    return keep, skipped


# --------------------------------------------------------------------------- #
# State (committed) and cache (ephemeral)
# --------------------------------------------------------------------------- #

class State:
    """state/sitemaps.json -- small, human-readable, committed to git."""

    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {"version": 1, "leaves": {}, "lastRun": {}}
        self.leaves = self.data.setdefault("leaves", {})

    def seen(self, leaf_url: str) -> bool:
        return leaf_url in self.leaves

    def etag(self, leaf_url: str) -> str | None:
        return self.leaves.get(leaf_url, {}).get("etag")

    def watermark(self, leaf_url: str) -> float:
        return parse_lastmod(self.leaves.get(leaf_url, {}).get("maxLastmod"))

    def update_leaf(self, leaf_url: str, etag, last_modified, url_count, max_lastmod, status):
        self.leaves[leaf_url] = {
            "etag": etag,
            "lastModified": last_modified,
            "urlCount": url_count,
            "maxLastmod": max_lastmod,
            "lastSeen": iso_now(),
            "status": status,
        }

    def save(self, last_run: dict) -> None:
        self.data["lastRun"] = last_run
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)


class Cache:
    """URL-level state in actions/cache. Never committed. Missing == degraded."""

    def __init__(self, root: str):
        self.root = root
        self.urls_dir = os.path.join(root, "urls")
        self.debounce_path = os.path.join(root, "debounce.tsv.gz")

    def has_leaf(self, leaf_url: str) -> bool:
        return os.path.exists(self._leaf_path(leaf_url))

    def _leaf_path(self, leaf_url: str) -> str:
        return os.path.join(self.urls_dir, leaf_id(leaf_url) + ".tsv.gz")

    def load_leaf(self, leaf_url: str) -> dict[str, str]:
        path = self._leaf_path(leaf_url)
        if not os.path.exists(path):
            return {}
        out = {}
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                loc, _, lastmod = line.rstrip("\n").partition("\t")
                if loc:
                    out[loc] = lastmod
        return out

    def save_leaf(self, leaf_url: str, url_map: dict[str, str]) -> None:
        os.makedirs(self.urls_dir, exist_ok=True)
        path = self._leaf_path(leaf_url)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for loc, lastmod in url_map.items():
                f.write(f"{loc}\t{lastmod or ''}\n")
        os.replace(tmp, path)

    def load_debounce(self) -> dict[str, float]:
        if not os.path.exists(self.debounce_path):
            return {}
        out = {}
        with gzip.open(self.debounce_path, "rt", encoding="utf-8") as f:
            for line in f:
                url, _, ts = line.rstrip("\n").partition("\t")
                if url:
                    try:
                        out[url] = float(ts)
                    except ValueError:
                        continue
        return out

    def save_debounce(self, ledger: dict[str, float], keep_hours: float) -> None:
        os.makedirs(self.root, exist_ok=True)
        cutoff = now_utc().timestamp() - keep_hours * 3600
        tmp = self.debounce_path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for url, ts in ledger.items():
                if ts >= cutoff:  # prune stale entries so the ledger stays small
                    f.write(f"{url}\t{ts}\n")
        os.replace(tmp, self.debounce_path)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def write_summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    log(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text)


def append_run_log(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


STATUS_EMOJI = {"ok": "✅", "seed": "🌱", "warn": "⚠️", "aborted": "⛔", "failed": "❌"}


def _run_status(rec: dict) -> str:
    """Derive a status label from a run-log record (tolerant of old records)."""
    s = rec.get("status")
    if s:
        return s
    if rec.get("aborted"):
        return "aborted"
    if rec.get("mode") == "seed":
        return "seed"
    if rec.get("errored_leaves"):
        return "warn"
    return "ok"


def _fmt_utc(iso: str) -> str:
    # "2026-08-29T17:22:03+00:00" -> "2026-08-29 17:22 UTC"
    t = iso.replace("T", " ")
    return (t[:16] + " UTC") if len(t) >= 16 else (iso or "?")


def render_runlog(runs_file: str, runlog_path: str, window: int = 200) -> None:
    """Render a human-readable Markdown view of the most recent runs.

    Derived entirely from runs.jsonl (the machine-readable source of truth), so
    the two never drift. Newest first, with a one-line health banner on top.
    """
    if not os.path.exists(runs_file):
        return
    records = []
    with open(runs_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not records:
        return

    recent = list(reversed(records[-window:]))  # newest first
    latest = recent[0]
    ls = _run_status(latest)
    banner = (f"_Last run: {_fmt_utc(latest.get('at', ''))} — "
              f"{STATUS_EMOJI.get(ls, '')} {ls.upper()} · "
              f"{latest.get('tier', '?')} tier · "
              f"submitted {latest.get('submitted', 0)}, "
              f"candidates {latest.get('candidates', 0)}._")

    out = [
        "# IndexNow run log",
        "",
        banner,
        "",
        f"Most recent {len(recent)} run(s), newest first. Times are UTC. "
        "Full machine-readable history: [`runs.jsonl`](runs.jsonl).",
        "",
        "| UTC time | Tier | Mode | Status | Cand. | Submitted | Deferred | Warn |",
        "|----------|------|------|--------|------:|----------:|---------:|-----:|",
    ]
    for rec in recent:
        s = _run_status(rec)
        out.append(
            f"| {_fmt_utc(rec.get('at', ''))} | {rec.get('tier', '?')} "
            f"| {rec.get('mode', '?')} | {STATUS_EMOJI.get(s, '')} {s} "
            f"| {rec.get('candidates', 0)} | {rec.get('submitted', 0)} "
            f"| {rec.get('deferred', 0)} | {rec.get('warnings', 0)} |")
    text = "\n".join(out) + "\n"
    os.makedirs(os.path.dirname(runlog_path) or ".", exist_ok=True)
    tmp = runlog_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, runlog_path)


def emit_alert(message: str) -> None:
    """Leave a marker the workflow turns into a GitHub issue."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("alert=true\n")
            f.write(f"alert_message={message}\n")
    log(f"ALERT: {message}")


# --------------------------------------------------------------------------- #
# Crawl
# --------------------------------------------------------------------------- #

@dataclass
class Crawl:
    cur_leaf_maps: dict[str, dict[str, str]]  # leaf_url -> {url: lastmod}
    etags: dict[str, str | None]              # leaf_url -> ETag seen this run
    last_modified: dict[str, str | None]      # leaf_url -> Last-Modified header
    parsed: list[str]
    skipped: list[str]
    errored: list[str]
    warnings: list[str]


def crawl_leaves(http: Http, leaves: list[str], state: State, cache: Cache) -> Crawl:
    cur: dict[str, dict[str, str]] = {}
    etags: dict[str, str | None] = {}
    last_modified: dict[str, str | None] = {}
    parsed, skipped, errored, warnings = [], [], [], []
    for leaf in leaves:
        head = http.head(leaf)
        etag = head.get("etag")
        # Persist the HEAD-time ETag: next run's skip check compares against a
        # future HEAD, so this is the value that must match.
        etags[leaf] = etag
        last_modified[leaf] = head.get("last_modified")
        prev_etag = state.etag(leaf)
        # Skip re-download only when ETag matches AND we still hold its URLs.
        if etag and prev_etag and etag == prev_etag and cache.has_leaf(leaf):
            cur[leaf] = cache.load_leaf(leaf)
            skipped.append(leaf)
            continue
        try:
            body = http.get(leaf)
            kind, items = parse_sitemap(body)
            if kind != "urlset":
                warnings.append(f"crawl: {leaf} resolved to <{kind}>, expected urlset")
                errored.append(leaf)
                continue
            cur[leaf] = {loc: (lm or "") for loc, lm in items}
            parsed.append(leaf)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"crawl: {leaf} -> {type(e).__name__}: {e}")
            errored.append(leaf)
            etags[leaf] = prev_etag  # don't let a failed fetch overwrite good ETag
            # Carry forward previous URLs so a transient error is not read as
            # 250k removals.
            if cache.has_leaf(leaf):
                cur[leaf] = cache.load_leaf(leaf)
    return Crawl(cur, etags, last_modified, parsed, skipped, errored, warnings)


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #

def submit(http: Http, cfg: dict, key: str, urls: list[str]) -> tuple[list[dict], int | None, int]:
    """POST urls in batches.

    Returns (batch_results, retry_after_seconds|None, n_accepted). n_accepted is
    the count of leading URLs durably accepted (200/202) by the primary endpoint,
    so callers can requeue exactly what did not get through on a 429.
    """
    primary = cfg["endpoints"]["primary"]
    extras = list(cfg["endpoints"].get("extra", []))
    batch_size = int(cfg["limits"]["batch_size"])
    pause = float(cfg["limits"]["batch_pause_seconds"])
    key_location = cfg["key_location_template"].format(key=key)
    results: list[dict] = []
    n_accepted = 0
    total = len(urls)
    for i in range(0, total, batch_size):
        batch = urls[i:i + batch_size]
        payload = {
            "host": cfg["host"],
            "key": key,
            "keyLocation": key_location,
            "urlList": batch,
        }
        try:
            status = http.post_json(primary, payload)
        except RateLimited as e:
            results.append({"endpoint": primary, "count": len(batch), "status": 429})
            return results, _retry_after_seconds(e.retry_after), n_accepted
        results.append({"endpoint": primary, "count": len(batch), "status": status})
        if status in (403, 422):
            return results, None, n_accepted  # hard failure: stop immediately
        if status in (200, 202):
            n_accepted += len(batch)
        # Mirror to any extra endpoints best-effort; they don't affect accounting.
        for endpoint in extras:
            try:
                s2 = http.post_json(endpoint, payload)
                results.append({"endpoint": endpoint, "count": len(batch), "status": s2})
            except RateLimited:
                pass
        if i + batch_size < total:
            time.sleep(pause)
    return results, None, n_accepted


def _retry_after_seconds(value: str | None) -> int:
    if not value:
        return 300
    try:
        return int(value)
    except ValueError:
        return 300


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_globals(crawl: Crawl, state: State, cache: Cache):
    """Assemble prev/cur global views, honouring per-leaf 'first seen' seeding."""
    prev_global: dict[str, str] = {}
    cur_seen: dict[str, str] = {}
    cur_all_keys: set[str] = set()
    leaf_of: dict[str, str] = {}
    first_seen_leaves: list[str] = []

    for leaf, url_map in crawl.cur_leaf_maps.items():
        cur_all_keys.update(url_map)
        for u in url_map:
            leaf_of[u] = leaf
        if state.seen(leaf):
            cur_seen.update(url_map)
            for u, lm in cache.load_leaf(leaf).items():
                prev_global[u] = lm
        else:
            first_seen_leaves.append(leaf)
    return prev_global, cur_seen, cur_all_keys, leaf_of, first_seen_leaves


def run(args) -> int:
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    tier = args.tier
    if tier not in cfg["tiers"]:
        log(f"Unknown tier: {tier}")
        return EXIT_CONFIG

    key = os.environ.get(cfg["key_env"], "").strip()
    submitting = not (args.dry_run or args.seed)
    if submitting and not key:
        log(f"Missing {cfg['key_env']} -- cannot submit. Set the secret or use --dry-run.")
        return EXIT_CONFIG

    limits = cfg["limits"]
    http = Http(cfg["user_agent"], int(limits["http_timeout_seconds"]),
                int(limits["http_retries"]), int(limits["http_backoff_seconds"]))
    state = State(args.state_file)
    cache = Cache(args.cache_dir)
    is_submittable = build_hygiene(cfg["host"], cfg.get("robots_disallow", []))

    warnings: list[str] = []
    roots = cfg["tiers"][tier]["roots"]
    log(f"Discovering leaf sitemaps for tier '{tier}' ({len(roots)} root(s))...")
    leaves = discover_leaves(http, roots, warnings)
    log(f"Found {len(leaves)} leaf sitemap(s).")

    crawl = crawl_leaves(http, leaves, state, cache)
    warnings.extend(crawl.warnings)
    total_urls = sum(len(m) for m in crawl.cur_leaf_maps.values())

    prev_global, cur_seen, cur_all_keys, leaf_of, first_seen = build_globals(crawl, state, cache)

    # -- decide mode & compute candidates ---------------------------------- #
    forced_seed = args.seed or not state.leaves  # empty committed state => first run
    cache_available = any(cache.has_leaf(l) for l in crawl.cur_leaf_maps if state.seen(l))
    mode = "seed" if forced_seed else ("normal" if cache_available else "watermark")

    if mode == "seed":
        diff = Diff()
    elif mode == "normal":
        diff = compute_diff(prev_global, cur_seen, cur_all_keys)
    else:
        warnings.append("URL-level cache unavailable -> watermark fallback "
                        "(removals and stale-lastmod additions not detected).")
        watermarks = {l: state.watermark(l) for l in crawl.cur_leaf_maps if state.seen(l)}
        diff = watermark_diff(cur_seen, watermarks, leaf_of)

    # -- hygiene + dedupe --------------------------------------------------- #
    lastmod_of = {u: lm for m in crawl.cur_leaf_maps.values() for u, lm in m.items()}
    raw_candidates = diff.all_candidates()
    seen_set: set[str] = set()
    candidates: list[str] = []
    dropped_hygiene = 0
    for u in raw_candidates:
        if u in seen_set:
            continue
        seen_set.add(u)
        if is_submittable(u):
            candidates.append(u)
        else:
            dropped_hygiene += 1

    # -- circuit breaker ---------------------------------------------------- #
    if mode != "seed" and circuit_breaker_tripped(len(candidates), total_urls, limits):
        threshold = max(int(limits["circuit_breaker_min"]),
                        int(total_urls * float(limits["circuit_breaker_fraction"])))
        msg = (f"Circuit breaker: {len(candidates)} candidates > {threshold} "
               f"(tier={tier}, total={total_urls}). Aborted without submitting -- "
               f"this looks like a sitemap regeneration, not real content change.")
        emit_alert(msg)
        write_summary(["# IndexNow: ABORTED", "", msg])
        if not args.dry_run:
            append_run_log(args.runs_file, {
                "at": iso_now(), "tier": tier, "mode": mode, "status": "aborted",
                "aborted": True, "candidates": len(candidates), "submitted": 0,
                "deferred": 0, "warnings": len(warnings),
                "threshold": threshold, "total_urls": total_urls,
            })
            render_runlog(args.runs_file, args.runlog_file)
        return EXIT_CIRCUIT_BREAKER

    # -- debounce ----------------------------------------------------------- #
    ledger = cache.load_debounce()
    deduped, debounced = apply_debounce(candidates, ledger, float(limits["debounce_hours"]))

    # -- per-run cap -------------------------------------------------------- #
    to_submit, deferred = select_for_submission(deduped, lastmod_of, int(limits["per_run_cap"]))

    # -- submit ------------------------------------------------------------- #
    batch_results: list[dict] = []
    retry_after = None
    hard_failure = False
    n_accepted = 0
    if submitting and to_submit:
        batch_results, retry_after, n_accepted = submit(http, cfg, key, to_submit)
        hard_failure = any(r["status"] in (403, 422) for r in batch_results)
        if not hard_failure:
            ts = now_utc().timestamp()
            for u in to_submit[:n_accepted]:  # only debounce what actually landed
                ledger[u] = ts
    submitted_count = n_accepted if submitting else 0

    # -- persist state (skip entirely on dry-run so it stays repeatable) ---- #
    if not args.dry_run and not hard_failure:
        deferred_set = set(deferred)
        if retry_after is not None:
            # 429 mid-run: requeue every URL that did not land so it retries next run.
            deferred_set.update(to_submit[n_accepted:])
        for leaf, url_map in crawl.cur_leaf_maps.items():
            if leaf in crawl.errored and not url_map:
                continue
            # Requeue: keep un-submitted changed URLs at their OLD lastmod and
            # drop un-submitted new URLs, so they resurface next run.
            if deferred_set and state.seen(leaf):
                persisted = dict(url_map)
                for u in list(persisted):
                    if u in deferred_set:
                        if u in prev_global:
                            persisted[u] = prev_global[u]      # revert changed
                        else:
                            del persisted[u]                    # hide new
                cache.save_leaf(leaf, persisted)
            else:
                cache.save_leaf(leaf, url_map)
            max_lastmod = max((lm for lm in url_map.values()),
                              key=parse_lastmod, default="") if url_map else ""
            head_status = "ok" if leaf not in crawl.errored else "error"
            state.update_leaf(leaf, crawl.etags.get(leaf), crawl.last_modified.get(leaf),
                              len(url_map), max_lastmod, head_status)
        cache.save_debounce(ledger, float(limits["debounce_hours"]) * 2)
        state.save({
            "tier": tier, "at": iso_now(), "mode": mode,
            "submitted": submitted_count,
            "candidates": len(candidates), "deferred": len(deferred_set),
        })

    # -- report ------------------------------------------------------------- #
    status_line = "DRY RUN" if args.dry_run else ("SEED" if mode == "seed" else "OK")
    summary = [
        f"# IndexNow: {status_line}",
        "",
        f"- **Tier**: {tier}  ", f"- **Mode**: {mode}  ",
        f"- **Leaves**: {len(leaves)} (parsed {len(crawl.parsed)}, "
        f"skipped-unchanged {len(crawl.skipped)}, errored {len(crawl.errored)})  ",
        f"- **Total URLs seen**: {total_urls:,}  ",
        f"- **Candidates**: {len(candidates)} "
        f"(new {len(diff.new)}, changed {len(diff.changed)}, removed {len(diff.removed)})  ",
        f"- **Dropped by hygiene**: {dropped_hygiene}  ",
        f"- **Debounced (last {limits['debounce_hours']}h)**: {debounced}  ",
        f"- **Submitted**: {submitted_count}"
        f"{' (dry-run: 0)' if args.dry_run else ''}  ",
        f"- **Deferred to next run (cap {limits['per_run_cap']})**: {len(deferred)}  ",
    ]
    if batch_results:
        codes = ", ".join(f"{r['count']}→{r['status']}" for r in batch_results)
        summary.append(f"- **Batch responses**: {codes}  ")
    if retry_after is not None:
        summary.append(f"- **Rate limited**: re-queued, Retry-After={retry_after}s  ")
    if warnings:
        summary.append("")
        summary.append("### Warnings")
        for w in warnings[:20]:
            summary.append(f"- {w}")
    if to_submit:
        summary.append("")
        summary.append("<details><summary>Sample of submitted URLs</summary>\n")
        for u in to_submit[:15]:
            summary.append(f"- {u}")
        summary.append("\n</details>")
    write_summary(summary)

    run_status = ("failed" if hard_failure else
                  "seed" if mode == "seed" else
                  "warn" if crawl.errored else "ok")
    if not args.dry_run:
        append_run_log(args.runs_file, {
            "at": iso_now(), "tier": tier, "mode": mode, "status": run_status,
            "leaves": len(leaves), "total_urls": total_urls,
            "new": len(diff.new), "changed": len(diff.changed), "removed": len(diff.removed),
            "candidates": len(candidates), "dropped_hygiene": dropped_hygiene,
            "debounced": debounced, "submitted": submitted_count,
            "deferred": len(deferred) + (len(to_submit) - n_accepted if retry_after else 0),
            "warnings": len(warnings), "errored_leaves": crawl.errored,
            "batch_results": batch_results, "retry_after": retry_after,
        })
        render_runlog(args.runs_file, args.runlog_file)

    if hard_failure:
        emit_alert("IndexNow returned 403/422 -- key file or host mismatch. "
                   "Check https://{host}/{key}.txt is live and matches the secret."
                   .format(host=cfg["host"], key=key or "<key>"))
        return EXIT_HARD_FAILURE
    if crawl.errored:
        # Not fatal, but surface it (e.g. blog/post-sitemap.xml returning 500).
        emit_alert(f"{len(crawl.errored)} sitemap(s) errored during crawl: "
                   + ", ".join(crawl.errored[:5]))
    return EXIT_OK


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Submit changed PharmEasy URLs to IndexNow.")
    p.add_argument("--tier", default="fast", help="which tier to sweep (see config.json)")
    p.add_argument("--config", default="config.json")
    p.add_argument("--state-file", default=os.path.join("state", "sitemaps.json"))
    p.add_argument("--runs-file", default=os.path.join("state", "runs.jsonl"))
    p.add_argument("--runlog-file", default=os.path.join("state", "RUNLOG.md"),
                   help="human-readable Markdown run log (derived from runs-file)")
    p.add_argument("--cache-dir", default=os.environ.get("INDEXNOW_CACHE_DIR", "cache"))
    p.add_argument("--dry-run", action="store_true",
                   help="compute and print the diff; submit nothing; write no state")
    p.add_argument("--seed", action="store_true",
                   help="record baseline state and submit nothing")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
