---
name: aiwriter
description: Generate GEO articles through the GeoWriter Integration API, store each article as a local bundle, and publish saved articles to WordPress through GeoWriter publish configs. Use when Codex needs `.env` setup, article generation, local Markdown cleanup with downloaded images, publish-config lookup, taxonomy lookup, or WordPress publishing.
---

# AIWriter

Use this skill for two workflows:

1. Generate an article through GeoWriter.
2. Publish a generated article bundle to WordPress through a GeoWriter publish config.

## Runtime Expectations

- Generation and publish jobs are remote long-running tasks. It is normal for them to take several minutes, often around 5 minutes.
- After a `generate` or `publish` command starts, wait for progress polling to continue instead of resubmitting the same action immediately.
- Safe requests are retried automatically on transient network failures. Publish submission is not blindly retried because duplicate submissions are risky.

## Workflow

1. Ensure the skill is configured.
   - Run:
   - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py init`
2. Generate an article bundle.
   - Run:
   - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate "best hiking trails"`
3. If the article already has a local bundle, resume against the same directory instead of creating a new one.
   - Run:
   - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate --article-dir path/to/article-bundle`
4. Discover publish configs or taxonomy when the user needs to publish.
   - Run:
   - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py configs`
   - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py taxonomy 12`
5. Publish the saved article bundle to WordPress.
   - Run:
   - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py publish path/to/article-bundle --publish-config-id 12 --status publish`
6. Be patient while the remote job runs.
   - Progress updates may continue for around 5 minutes before completion.
   - Do not restart the workflow just because the task is still polling.

## Local Bundle Layout

Every generated article is stored under `GW_ARTICLES_DIR/<slug>-<timestamp>/` where `<timestamp>` uses `YYYYMMDD-HHMMSS` (for example: `best-hiking-trails-20260319-121732`).

Bundle contents:

- `article.md`: clean local Markdown
- `manifest.json`: stable local metadata such as article key, idempotency key, document ID, publish state
- `document.json`: raw GeoWriter document detail payload
- `generation.json`: raw generation request/response/progress state
- `publish.json`: publish attempts and final publish progress
- `images/`: downloaded images referenced from `article.md` with relative paths like `images/image-1.png`

The generate flow always creates or reuses a stable `article_key`. Unless overridden, that same value is sent as `idempotency_key` to support retries and local resume behavior.

## Commands

- Initialize `.env` interactively:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py init`
- Generate a new article bundle:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate "best hiking trails"`
- Generate and emit JSON summary:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate "best hiking trails" --format json`
- Reuse an existing article bundle directory:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate --article-dir output/articles/best-hiking-trails-20260319-121732`
- Force a caller-supplied idempotency key:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate "best hiking trails" --idempotency-key retry-key-001`
- List publish configs:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py configs`
- Inspect taxonomy for a config:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py taxonomy 12`
- Publish to WordPress:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py publish path/to/article-bundle --publish-config-id 12 --status publish`
- Publish with categories and tags:
  - `python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py publish path/to/article-bundle --publish-config-id 12 --categories 3,8 --tags 21,34`

For WordPress categories, pass only the final target category IDs. Do not add parent category IDs unless you explicitly want the post assigned to multiple categories.

`create` remains as a backward-compatible alias of `generate`.

## Configuration

Required:

- `GW_API_BASE_URL`
- `GW_API_KEY`

Optional:

- `GW_POLL_INTERVAL_SECONDS`
- `GW_IMAGE_POLL_INTERVAL_SECONDS`
- `GW_PUBLISH_POLL_INTERVAL_SECONDS`
- `GW_REQUEST_TIMEOUT_SECONDS`
- `GW_GENERATION_TIMEOUT_SECONDS`
- `GW_PUBLISH_TIMEOUT_SECONDS`
- `GW_ARTICLES_DIR`

Only these keys are read from `.env`.

## References

- Read `references/open-platform-api.md` for endpoint paths, payload shape, bundle layout, and command behavior.

## Testing

- Run:
  - `/Users/striver/workspace/sectojoy/aiwriter/scripts/run_tests.sh`
