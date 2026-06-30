# 42 subject.pdf crawler

> 日本語版: [README.ja.md](README.ja.md)

A crawler that bulk-downloads the `subject.pdf` of **"Not registered"** projects on the 42 intra.

## How it works (hybrid design)

| Step | Data source | Why |
|---|---|---|
| 1. List Not-registered projects | **42 API** (`api.intra.42.fr/v2`) | Structured and reliable |
| 2. Extract subject.pdf URL + download | **HTML** (`projects.intra.42.fr`) | The PDF link is not exposed by the API; it only exists in the detail page HTML |

Because of this, **two kinds of credentials** are required.

## Required credentials (passed via env vars, never hardcoded)

| Variable | Meaning | How to get it |
|---|---|---|
| `INTRA_TOKEN` | Bearer token for api.intra.42.fr | Token of a 42 OAuth app (expires after ~2h) |
| `INTRA_LOGIN` | Your 42 login (e.g. `saraki`) | App tokens can't use `/v2/me`, so `/v2/users/<login>` is used instead |
| `INTRA_CURSUS_ID` | Target cursus id (e.g. `21`=42cursus, `1`=old 42) | Lists projects via `/v2/cursus/:id/projects` |
| `INTRA_SESSION` | Value of the `_intra_42_session_production` cookie | From a logged-in browser: DevTools → Application → Cookies → `https://projects.intra.42.fr` → `_intra_42_session_production` |

### Behavior by token type
- **App token** (`client_credentials`) … `/v2/me/*` returns 401. With `INTRA_LOGIN` and `INTRA_CURSUS_ID` (or `--user` / `--cursus-id`) set, it uses `/v2/cursus/:id/projects` and `/v2/users/:login/projects_users`.
- **User token** (`authorization_code`) … Omit `INTRA_LOGIN`/`INTRA_CURSUS_ID` to use `/v2/me/projects` and `/v2/me/projects_users`.

## Usage (uv recommended; dependencies resolved automatically)

```bash
# First, a sanity check that only lists projects and resolves PDF URLs (no download)
INTRA_TOKEN=xxxxx INTRA_SESSION=yyyyy uv run crawler.py --dry-run

# Real run (saves to subjects/<slug>/subject.pdf)
INTRA_TOKEN=xxxxx INTRA_SESSION=yyyyy uv run crawler.py --out subjects
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--out DIR` | `subjects` | Output directory |
| `--delay SEC` | `0.6` | Interval between requests (the API limit is 2 req/s) |
| `--cursus-id N` | none | Restrict the listing to a specific cursus (default is `/v2/me/projects`) |
| `--user LOGIN` | none | 42 login for `/v2/users/:user/projects_users` (default is `/v2/me/projects_users`) |
| `--all` | off | Drop the "not registered" filter and fetch ALL projects |
| `--limit N` | none | Process only the first N projects (for trial runs) |
| `--dry-run` | off | List + resolve PDF URLs only. No download (no cookie needed) |

## Behavior notes
- An existing `subject.pdf` is skipped (re-runs only fetch the delta).
- Projects without a `subject.pdf`, and failed projects, are listed in the final summary.
- Explicit errors are raised on: 401 (API) → `INTRA_TOKEN` expired; redirect to sign-in → `INTRA_SESSION` expired.
- Rate limiting (429/5xx) and network errors are retried automatically with exponential backoff.

## Notes on assumptions
The code assumes the response shapes of `/v2/me/projects` (`id`/`slug`/`name`) and `/v2/me/projects_users` (`project.id`).
If your token type cannot use `/v2/me/...`, set `--cursus-id` to switch to the `/v2/cursus/:id/projects` path.
