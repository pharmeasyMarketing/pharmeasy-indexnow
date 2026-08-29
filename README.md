# PharmEasy → IndexNow

Pushes **changed PharmEasy URLs** to [IndexNow](https://www.indexnow.org/) so
participating search engines re-crawl them within minutes instead of waiting to
re-read the sitemaps. Runs entirely on **GitHub Actions** — no PharmEasy
infrastructure beyond one static key file.

> **Coverage:** IndexNow feeds **Bing/Copilot, Yandex, Seznam, Naver** and the
> AI search engines that consume the protocol. **Google does not participate in
> IndexNow** — this does nothing for Google. Use Google Search Console for that.

---

## How it works

Every run:

1. Reads `https://pharmeasy.in/sitemap.xml` and walks it down to the **80 leaf
   sitemaps** (~250k URLs).
2. For each leaf, a cheap `HEAD` compares the **ETag** against last run — unchanged
   leaves are skipped without downloading (the big S3-hosted product shards support
   this, so most runs download very little).
3. Changed leaves are parsed and diffed against saved state using **three signals**:
   - `lastmod` moved forward → **updated**
   - URL is new → **added**
   - URL vanished from the sitemap → **removed** (IndexNow signals deletions too)
4. Candidates pass through **safety guards** (below), then get POSTed to IndexNow
   in batches.

```
sitemap.xml ─► 11 sections ─► 80 leaves ─► HEAD/ETag ─► diff ─► guards ─► IndexNow
                                             (skip unchanged)
```

### Two tiers, one workflow

| Tier | Schedule (UTC) | Scope | Why |
|------|----------------|-------|-----|
| **fast** | hourly, `:20` | blog, categories, doctors, diagnostics, general (~9k URLs) | catch new blog posts / category edits within the hour |
| **full** | `08:00, 14:00, 19:00, 22:00` | all 80 leaves (~250k URLs) | timed just after PharmEasy regenerates each section |

Runs are near-free: ETag skipping means most runs do little work. ~14 min/day.

---

## Safety guards

The point of these is to never dump the whole catalogue at IndexNow if a sitemap
regeneration resets every `lastmod`. Defaults live in [`config.json`](config.json).

| Guard | Default | Effect |
|-------|---------|--------|
| **Circuit breaker** | candidates > max(5,000, 25% of total) | abort, submit nothing, open an alert issue |
| **Per-run cap** | 10,000 | submit newest first, defer the rest to next run |
| **Debounce** | 24h | never resubmit the same URL within a day |
| **Batch size** | 5,000/POST | half the 10k protocol limit |
| **URL hygiene** | — | enforce `https://pharmeasy.in/`, drop robots.txt-disallowed paths, dedupe |
| **429 backoff** | — | honour `Retry-After`, requeue unsent URLs for next run |
| **403 / 422** | — | fail loudly — key file or host is misconfigured |

---

## One-time setup

### 1. Host the key file  *(infra — blocking)*
Serve this exact file, same way `ads.txt` is served today:

- **URL:** `https://pharmeasy.in/d1ab5f5b0720292be95648feab54b6d0.txt`
- **Content-Type:** `text/plain`
- **Body:** exactly `d1ab5f5b0720292be95648feab54b6d0` (nothing else)

Verify:
```bash
curl -sS -i https://pharmeasy.in/d1ab5f5b0720292be95648feab54b6d0.txt
```
Expect `200`, `text/plain`, body = the key. *(The key is public by design — it
proves domain ownership, it is not a secret. To rotate, see below.)*

### 2. Add the repo secret
Repo → Settings → Secrets and variables → Actions → **New repository secret**:

- Name: `INDEXNOW_KEY`
- Value: `d1ab5f5b0720292be95648feab54b6d0`

### 3. (Optional) Register in Bing Webmaster Tools
[bing.com/webmasters](https://www.bing.com/webmasters) → IndexNow → add the key,
for the submission-reporting dashboard.

### 4. Seed, then enable
Push the repo, then run **Actions → IndexNow submit → Run workflow** with
`seed = true` once. It records the baseline and **submits nothing**. After that,
scheduled runs submit only real deltas. (If you skip this, the first scheduled run
auto-seeds anyway — seeding is safe by construction.)

---

## Connecting this folder to your office GitHub repo

`.env` (gitignored) holds the connection details. It is **not** needed to run the
automation — only to push from this machine and to do local dry-runs.

```bash
cp .env.example .env      # then edit: set GITHUB_REPO, name/email, (token if HTTPS)
bash scripts/setup-remote.sh
git add -A && git commit -m "Initial IndexNow automation" && git push -u origin main
```

`setup-remote.sh` reads `.env`, sets the `origin` remote (embedding your token
locally for HTTPS pushes), and configures your commit identity. The IndexNow key
in `.env` is only used for local `--dry-run`.

---

## Operations

**Manual dry-run (no submission, no state change) — from the Actions tab**
Run workflow → pick tier → `dry_run = true`. The job summary shows the diff.

**Local dry-run**
```bash
set -a; . ./.env; set +a
python3 indexnow.py --tier fast --dry-run
```

**What each run leaves behind**
- [`state/sitemaps.json`](state/sitemaps.json) — per-leaf ETag, URL count, watermark. Committed. Read this first when debugging.
- [`state/runs.jsonl`](state/runs.jsonl) — one line per run (counts, responses, warnings). The audit trail.
- URL-level detail lives in the Actions **cache** (not git). If it is ever evicted, the next run falls back to a `lastmod` watermark and rebuilds it — logged as a warning, not a failure.

**On an alert issue** (labelled `indexnow-alert`)
- *Circuit breaker tripped* → a sitemap likely regenerated with fresh `lastmod`s. Confirm it is not a real mass update, then re-run; state was not advanced.
- *403 / 422* → the key file is missing/wrong or a host mismatch. Re-check step 1 and the `INDEXNOW_KEY` secret match.
- *N sitemaps errored* → a leaf returned an error; the run routes around it (see "Resilience" below).

**Rotate the key**
1. Generate: `openssl rand -hex 16`
2. Host the new `<key>.txt`, update the `INDEXNOW_KEY` secret, update `.env`.
3. Old and new can coexist; remove the old file after a day.

---

## Resilience to broken / new sitemaps

If a leaf sitemap returns an error (e.g. a 5xx from the CDN or WordPress), the tool
logs it, raises an alert, and **carries forward** that leaf's previous URLs rather
than treating the failure as a mass deletion — one bad leaf never sinks the run.

When a leaf later starts working (or a brand-new shard appears), it is **first-seen**
and gets **baselined silently** — recorded, nothing submitted — so recovered or newly
added sections never cause a bulk push. Subsequent changes then flow as normal deltas.

---

## Development

```bash
python3 -m unittest discover -s tests -v   # offline unit tests (no network)
python3 -m py_compile indexnow.py          # byte-compile check
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs both on every push
and PR. Pure standard library — no `pip install`, no lockfile.

- [`indexnow.py`](indexnow.py) — the whole tool
- [`config.json`](config.json) — tiers, thresholds, robots rules
- [`.github/workflows/indexnow.yml`](.github/workflows/indexnow.yml) — schedules + manual dispatch
