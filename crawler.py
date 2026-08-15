#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.31",
#   "beautifulsoup4>=4.12",
# ]
# ///
"""
42 intra "Not registered" subject.pdf crawler.

Two-source hybrid:
  * Listing of "Not registered" projects  -> 42 API (api.intra.42.fr/v2)
  * subject.pdf URL extraction + download -> HTML of projects.intra.42.fr
    (the subject.pdf link is NOT exposed by the API, so HTML parsing is required)

Re-runs are incremental: <out>/manifest.json records the CDN URL (plus sha256,
size, ETag/Last-Modified) each subject.pdf came from, and a subject is
re-downloaded when that URL no longer matches the one on the detail page.

Credentials (read from env, never hardcoded):
  INTRA_TOKEN    Bearer token for api.intra.42.fr (used for the listing step).
  INTRA_SESSION  Value of the `_intra_42_session_production` cookie from a
                 logged-in browser (fetches detail pages + downloads the PDFs).

Run with uv (auto-installs deps):
  INTRA_TOKEN=... INTRA_SESSION=... uv run crawler.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

API_BASE = "https://api.intra.42.fr"
WEB_BASE = "https://projects.intra.42.fr"
PER_PAGE = 100
# Name of the intra web session cookie (override via env if it ever changes).
SESSION_COOKIE_NAME = os.environ.get(
    "INTRA_SESSION_COOKIE_NAME", "_intra_42_session_production"
)


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    delay: float,
    max_retries: int = 5,
    **kwargs,
) -> requests.Response:
    """GET/POST with polite throttling + retry on 429/5xx and network errors."""
    kwargs.setdefault("timeout", (10, 60))  # (connect, read) seconds
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = session.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            last_exc = e
            wait = 2 ** attempt
            log(f"  ! network error on {url} ({e.__class__.__name__}) -> retry in {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            log(f"  ! {resp.status_code} on {url} -> retry in {wait:.1f}s")
            time.sleep(wait)
            continue
        time.sleep(delay)  # stay under the API rate limit (2 req/s)
        return resp
    if last_exc is not None:
        raise SystemExit(f"Giving up after {max_retries} retries on {url}: {last_exc}")
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------- #
# Step 1: listing via the 42 API
# --------------------------------------------------------------------------- #
def api_paginate(
    session: requests.Session, path: str, *, delay: float, params: dict | None = None
) -> list[dict]:
    """Follow page[number] pagination until an empty page is returned."""
    out: list[dict] = []
    page = 1
    base_params = dict(params or {})
    base_params["page[size]"] = PER_PAGE
    while True:
        base_params["page[number]"] = page
        url = urljoin(API_BASE, path)
        resp = request_with_retry(session, "GET", url, delay=delay, params=base_params)
        if resp.status_code == 401:
            raise SystemExit(
                "API returned 401 Unauthorized. Check / refresh INTRA_TOKEN "
                "(42 tokens expire after ~2h)."
            )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        out.extend(batch)
        log(f"  api {path} page {page}: +{len(batch)} (total {len(out)})")
        if len(batch) < PER_PAGE:
            break
        page += 1
    return out


def list_projects(
    api: requests.Session,
    *,
    delay: float,
    cursus_id: int | None,
    user: str | None,
    include_all: bool = False,
) -> list[dict]:
    """Return [{id, slug, name}] of projects.

    By default only projects the user is NOT registered to; with include_all=True
    the registration filter is dropped and every project is returned.

    Two auth modes:
      * App token (client_credentials): pass --user <login> and --cursus-id.
        Uses /v2/cursus/:id/projects and /v2/users/:login/projects_users
        (the /v2/me/* endpoints reject app tokens with 401).
      * User token (authorization_code): omit --user; uses /v2/me/projects(_users).
    """
    # All projects, optionally restricted to one cursus.
    if cursus_id is not None:
        all_projects = api_paginate(
            api, f"/v2/cursus/{cursus_id}/projects", delay=delay
        )
    else:
        all_projects = api_paginate(api, "/v2/me/projects", delay=delay)

    # Projects the user already has a projects_user for == "registered".
    registered_ids: set[int] = set()
    if not include_all:
        if user:
            registered = api_paginate(
                api, f"/v2/users/{user}/projects_users", delay=delay
            )
        else:
            registered = api_paginate(api, "/v2/me/projects_users", delay=delay)
        for pu in registered:
            proj = pu.get("project") or {}
            if proj.get("id") is not None:
                registered_ids.add(proj["id"])

    selected = []
    seen: set[int] = set()
    for p in all_projects:
        pid = p.get("id")
        if pid is None or pid in seen or pid in registered_ids:
            continue
        seen.add(pid)
        selected.append(
            {"id": pid, "slug": p.get("slug") or p.get("name"), "name": p.get("name")}
        )
    return selected


# --------------------------------------------------------------------------- #
# Step 2: subject.pdf extraction via HTML (the API has no pdf field)
# --------------------------------------------------------------------------- #
def extract_subject_pdf_url(html: str) -> str | None:
    """Find the subject.pdf link in a project detail page.

    Prefers en.subject.pdf, then any *.subject.pdf, then a link whose text is
    'subject.pdf'.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if href.endswith("subject.pdf") or text == "subject.pdf":
            candidates.append(href)
    if not candidates:
        return None
    for c in candidates:  # prefer English subject
        if "en.subject.pdf" in c:
            return c
    return candidates[0]


def get_pdf_url_for_slug(
    web: requests.Session, slug: str, *, delay: float
) -> str | None:
    url = f"{WEB_BASE}/projects/{slug}"
    resp = request_with_retry(web, "GET", url, delay=delay, allow_redirects=True)
    # Any redirect off projects.intra.42.fr (e.g. to auth.42.fr / keycloak) means
    # the session cookie is invalid or expired.
    if not resp.url.startswith(WEB_BASE) or "auth.42.fr" in resp.url:
        raise SystemExit(
            "Detail page redirected to login (got "
            f"{resp.url.split('?')[0]}).\n"
            f"INTRA_SESSION is wrong or expired. Copy the *{SESSION_COOKIE_NAME}* "
            "cookie value (a long string) from a logged-in browser: DevTools > "
            "Application > Cookies > https://projects.intra.42.fr."
        )
    if resp.status_code != 200:
        log(f"  ! {slug}: detail page HTTP {resp.status_code}")
        return None
    pdf = extract_subject_pdf_url(resp.text)
    return urljoin(resp.url, pdf) if pdf else None


def download_pdf(
    web: requests.Session, url: str, dest: Path, *, delay: float
) -> dict | None:
    """Download `url` to `dest`, returning its manifest record (None on failure).

    Written to a .part file first so an interrupted run never leaves a truncated
    subject.pdf that the next run would treat as complete.
    """
    resp = request_with_retry(
        web, "GET", url, delay=delay, allow_redirects=True, stream=True
    )
    if resp.status_code != 200:
        log(f"  ! download HTTP {resp.status_code}: {url}")
        return None
    ctype = resp.headers.get("Content-Type", "")
    if "pdf" not in ctype.lower():
        log(f"  ! not a pdf (Content-Type: {ctype}): {url}")
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                f.write(chunk)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {
        "url": url,
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
        "size": size,
        "sha256": digest.hexdigest(),
        "fetched_at": utcnow(),
    }


# --------------------------------------------------------------------------- #
# Manifest: what we downloaded, and from which URL
# --------------------------------------------------------------------------- #
# The subject.pdf is served from an ActiveStorage-backed CDN URL that changes
# whenever the file is re-uploaded, so a URL mismatch means the subject was
# updated upstream. Without this record the only freshness signal is "a local
# file exists", which never expires.
MANIFEST_NAME = "manifest.json"


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_manifest(out_dir: Path) -> dict[str, dict]:
    """Read out_dir/manifest.json; a missing or corrupt file just means 'nothing
    known yet' (the PDFs on disk are still adoptable via --adopt-existing)."""
    path = out_dir / MANIFEST_NAME
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f"! ignoring unreadable {path} ({e}); it will be rewritten")
        return {}
    if not isinstance(data, dict):
        log(f"! ignoring malformed {path}; it will be rewritten")
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_manifest(out_dir: Path, manifest: dict[str, dict]) -> None:
    """Atomically rewrite the manifest (called after every change so an
    interrupted run keeps what it already fetched)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / MANIFEST_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    tmp.replace(path)


def record_existing(dest: Path, url: str) -> dict:
    """Manifest record for a PDF already on disk (--adopt-existing).

    The URL is taken on trust: we cannot tell whether the local copy came from
    it, only that from now on a *change* of URL will be noticed.
    """
    digest = hashlib.sha256()
    size = 0
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return {
        "url": url,
        "etag": None,
        "last_modified": None,
        "size": size,
        "sha256": digest.hexdigest(),
        "fetched_at": None,  # unknown: adopted, not downloaded
        "adopted_at": utcnow(),
    }


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
TOKEN_CACHE = Path(__file__).with_name(".token_cache.json")


def fetch_access_token(client_id: str, client_secret: str) -> str:
    """Exchange a 42 app's client_id/secret for an access token (client_credentials),
    caching it on disk until it (nearly) expires.

    The /oauth/token endpoint is rate-limited and occasionally returns a transient
    invalid_client/429, so we retry a few times and reuse the cached token.

    Note: a client_credentials token is an *app* token; it cannot use /v2/me/*.
    Use --user/--cursus-id (the /v2/users + /v2/cursus paths) with it.
    """
    # Reuse a cached token while >120s of life remains.
    try:
        cached = json.loads(TOKEN_CACHE.read_text())
        if cached.get("client_id") == client_id and (
            cached["created_at"] + cached["expires_in"] - 120 > time.time()
        ):
            log("== Reusing cached access token ==")
            return cached["access_token"]
    except (OSError, KeyError, ValueError):
        pass

    log("== Minting app access token via client_credentials ==")
    last = ""
    for attempt in range(4):
        resp = requests.post(
            urljoin(API_BASE, "/oauth/token"),
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            data["client_id"] = client_id
            try:
                TOKEN_CACHE.write_text(json.dumps(data))
                TOKEN_CACHE.chmod(0o600)
            except OSError:
                pass
            return data["access_token"]
        last = f"{resp.status_code}: {resp.text[:200]}"
        wait = 2 ** attempt
        log(f"  ! token endpoint {resp.status_code} -> retry in {wait}s")
        time.sleep(wait)
    raise SystemExit(
        f"Failed to get access token after retries ({last}).\n"
        "If it says invalid_client, double-check INTRA_CLIENT_ID / INTRA_CLIENT_SECRET "
        "(no stray spaces); a transient 429 just means the token endpoint is throttled."
    )


def resolve_token() -> str:
    """Get an API bearer token: prefer a ready INTRA_TOKEN, else mint one
    from INTRA_CLIENT_ID + INTRA_CLIENT_SECRET."""
    token = os.environ.get("INTRA_TOKEN", "").strip()
    cid = os.environ.get("INTRA_CLIENT_ID", "").strip()
    secret = os.environ.get("INTRA_CLIENT_SECRET", "").strip()
    if token and not token.startswith("s-"):  # looks like a real access token
        return token
    if token.startswith("s-") and not secret:
        # Common mistake: client secret pasted into INTRA_TOKEN.
        log(
            "NOTE: INTRA_TOKEN looks like a client *secret* (starts with 's-'). "
            "Treating it as INTRA_CLIENT_SECRET; INTRA_CLIENT_ID is required too."
        )
        secret = token
    if cid and secret:
        return fetch_access_token(cid, secret)
    raise SystemExit(
        "No usable API credentials. Set INTRA_TOKEN to a valid access token, "
        "or set INTRA_CLIENT_ID + INTRA_CLIENT_SECRET to auto-mint one."
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_sessions(token: str, cookie: str) -> tuple[requests.Session, requests.Session]:
    api = requests.Session()
    api.headers.update(
        {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    web = requests.Session()
    # Scoped to .intra.42.fr so cdn.intra.42.fr gets it too.
    web.cookies.set(SESSION_COOKIE_NAME, cookie, domain=".intra.42.fr")
    web.headers.update({"User-Agent": "42-subject-crawler/1.0"})
    return api, web


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="subjects", help="output directory")
    ap.add_argument(
        "--delay", type=float, default=0.6, help="seconds between requests (<=2 req/s)"
    )
    ap.add_argument(
        "--cursus-id",
        type=int,
        default=(int(os.environ["INTRA_CURSUS_ID"]) if os.environ.get("INTRA_CURSUS_ID") else None),
        help="list projects of this cursus via /v2/cursus/:id/projects "
        "(required for app/client_credentials tokens; else /v2/me/projects)",
    )
    ap.add_argument(
        "--user",
        default=os.environ.get("INTRA_LOGIN") or None,
        help="42 login (or numeric id) for /v2/users/:user/projects_users "
        "(required for app tokens; env INTRA_LOGIN; else /v2/me/projects_users)",
    )
    ap.add_argument("--limit", type=int, default=None, help="process at most N projects")
    ap.add_argument(
        "--all",
        action="store_true",
        help="drop the 'not registered' filter and fetch ALL projects",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="list + resolve pdf URLs but do not download",
    )
    ap.add_argument(
        "--adopt-existing",
        action="store_true",
        help="index PDFs already on disk into the manifest instead of "
        "re-downloading them (first run after adding the manifest)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-download every subject.pdf even if the manifest says it is current",
    )
    args = ap.parse_args()

    cookie = os.environ.get("INTRA_SESSION", "").strip()
    if not cookie and not args.dry_run:
        return _fail(
            f"INTRA_SESSION env var is required ({SESSION_COOKIE_NAME} cookie)."
        )

    token = resolve_token()
    api, web = build_sessions(token, cookie)

    # App tokens (client_credentials) can't hit /v2/me/*; warn early if it looks
    # like one of those was paired with the /v2/me path.
    if args.user and args.cursus_id is None:
        log(
            "NOTE: --user is set but --cursus-id is not; project listing still "
            "uses /v2/me/projects which app tokens cannot access. "
            "Pass --cursus-id too when using an app token."
        )

    scope = "ALL" if args.all else "Not registered"
    log(f"== Step 1: listing '{scope}' projects via API ==")
    projects = list_projects(
        api,
        delay=args.delay,
        cursus_id=args.cursus_id,
        user=args.user,
        include_all=args.all,
    )
    log(f"Found {len(projects)} projects ({scope}).")
    if args.limit is not None:
        projects = projects[: args.limit]

    out_dir = Path(args.out)
    manifest = load_manifest(out_dir)
    fetched, updated, current, adopted, no_pdf, failed = [], [], [], [], [], []

    log("== Step 2: resolving + downloading subject.pdf via HTML ==")
    for i, p in enumerate(projects, 1):
        slug = p["slug"]
        log(f"[{i}/{len(projects)}] {slug}")
        try:
            pdf_url = get_pdf_url_for_slug(web, slug, delay=args.delay)
        except SystemExit:
            raise
        except Exception as e:  # network hiccup on one project shouldn't kill the run
            log(f"  ! error resolving {slug}: {e}")
            failed.append(slug)
            continue
        if not pdf_url:
            log("  - no subject.pdf on this project")
            no_pdf.append(slug)
            continue
        log(f"  -> {pdf_url}")
        if args.dry_run:
            fetched.append(slug)
            continue

        dest = out_dir / slug / "subject.pdf"
        existed = dest.exists()
        known = manifest.get(slug) if existed else None
        if known and not args.force:
            if known.get("url") == pdf_url:
                log("  = up to date, skip")
                current.append(slug)
                continue
            log(f"  ~ subject URL changed since {known.get('fetched_at') or 'adoption'}")
        elif existed and args.adopt_existing and not args.force:
            # Local file predates the manifest: record it so the *next* URL
            # change is detected, without spending a download now.
            manifest[slug] = record_existing(dest, pdf_url)
            save_manifest(out_dir, manifest)
            log("  = adopted existing file into manifest")
            adopted.append(slug)
            continue

        record = download_pdf(web, pdf_url, dest, delay=args.delay)
        if record is None:
            failed.append(slug)
            continue
        if known and known.get("sha256") == record["sha256"]:
            log("  (same content, only the URL changed)")
        manifest[slug] = record
        save_manifest(out_dir, manifest)
        log(f"  {'updated' if existed else 'saved'} {dest}")
        (updated if existed else fetched).append(slug)

    log("\n==================== SUMMARY ====================")
    log(f"  new           : {len(fetched)}")
    log(f"  updated       : {len(updated)}  {updated if updated else ''}")
    log(f"  up to date    : {len(current)}")
    if adopted:
        log(f"  adopted       : {len(adopted)}")
    log(f"  no subject.pdf: {len(no_pdf)}  {no_pdf if no_pdf else ''}")
    log(f"  failed        : {len(failed)}  {failed if failed else ''}")
    log(f"  output dir    : {out_dir.resolve()}")
    if not args.dry_run:
        log(f"  manifest      : {(out_dir / MANIFEST_NAME).resolve()}")
    return 1 if failed else 0


def _fail(msg: str) -> int:
    log(f"ERROR: {msg}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
