# AIWriter

AIWriter is a GeoWriter integration utility for generating GEO article bundles locally and publishing saved bundles to WordPress through GeoWriter publish configs.

## What It Does

- Initializes local `.env` settings for the GeoWriter Integration API.
- Generates article bundles with Markdown, manifest metadata, raw API payloads, and downloaded images.
- Resumes an existing article bundle instead of creating a new remote document when possible.
- Lists publish configs and taxonomy before publish.
- Publishes a saved article bundle to WordPress through a GeoWriter publish config.

## Runtime Behavior

- Remote generation and publish jobs are long-running operations.
- It is normal for them to take several minutes, often around 5 minutes.
- After a command starts, wait for progress polling instead of resubmitting immediately.
- Safe requests retry automatically on transient network failures.
- Publish submission is not blindly retried, because duplicate submissions can create duplicate publish jobs.

## Project Layout

- [`scripts/aiwriter.py`](/Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py): CLI entry point.
- [`SKILL.md`](/Users/striver/workspace/sectojoy/aiwriter/SKILL.md): skill instructions for agents.
- [`references/open-platform-api.md`](/Users/striver/workspace/sectojoy/aiwriter/references/open-platform-api.md): API and bundle reference.
- [`tests/test_aiwriter.py`](/Users/striver/workspace/sectojoy/aiwriter/tests/test_aiwriter.py): unit tests.

## Setup

1. Initialize a `.env` file:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py init
```

2. Provide at least these values:

- `GW_API_BASE_URL`
- `GW_API_KEY`

Optional tuning values:

- `GW_POLL_INTERVAL_SECONDS`
- `GW_IMAGE_POLL_INTERVAL_SECONDS`
- `GW_PUBLISH_POLL_INTERVAL_SECONDS`
- `GW_REQUEST_TIMEOUT_SECONDS`
- `GW_GENERATION_TIMEOUT_SECONDS`
- `GW_PUBLISH_TIMEOUT_SECONDS`
- `GW_ARTICLES_DIR`

## Common Commands

Generate a new article bundle:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate "best hiking trails"
```

Generate and print a JSON summary:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate "best hiking trails" --format json
```

Resume an existing article bundle:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py generate --article-dir output/articles/best-hiking-trails-20260319-121732
```

List publish configs:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py configs
```

Fetch taxonomy for a publish config:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py taxonomy 12
```

Publish a saved bundle to WordPress:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py publish path/to/article-bundle --publish-config-id 12 --status publish
```

Publish with categories and tags:

```bash
python3 /Users/striver/workspace/sectojoy/aiwriter/scripts/aiwriter.py publish path/to/article-bundle --publish-config-id 12 --categories 3,8 --tags 21,34
```

## Local Bundle Contents

Each generated bundle is stored under:

```text
GW_ARTICLES_DIR/<slug>-<timestamp>/
```

Bundle files:

- `article.md`: cleaned local Markdown.
- `manifest.json`: stable article metadata and publish state.
- `document.json`: raw GeoWriter document payload.
- `generation.json`: raw generation request, progress, and completion data.
- `publish.json`: append-only publish attempts and final publish progress.
- `images/`: downloaded images referenced from `article.md`.

## Testing

Run the test suite with:

```bash
/Users/striver/workspace/sectojoy/aiwriter/scripts/run_tests.sh
```
