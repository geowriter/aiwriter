# GeoWriter Integration API Reference

## Environment Keys

- `GW_API_BASE_URL`: GeoWriter base URL, for example `https://geowriter.ai`
- `GW_API_KEY`: Integration API key with document and publish scopes
- `GW_POLL_INTERVAL_SECONDS`: delay between normal generation progress checks, default `10`
- `GW_IMAGE_POLL_INTERVAL_SECONDS`: delay during the image stage, default `20`
- `GW_PUBLISH_POLL_INTERVAL_SECONDS`: delay between publish progress checks, default `10`
- `GW_REQUEST_TIMEOUT_SECONDS`: per-request timeout
- `GW_GENERATION_TIMEOUT_SECONDS`: end-to-end generation timeout
- `GW_PUBLISH_TIMEOUT_SECONDS`: end-to-end publish timeout
- `GW_ILLUSTRATION_POLL_INTERVAL_SECONDS`: delay between illustration progress checks, default `10`
- `GW_ILLUSTRATION_TIMEOUT_SECONDS`: end-to-end illustration timeout, default `300`
- `GW_ARTICLES_DIR`: local root for article bundles

## Runtime Notes

- Generation and publish are remote long-running jobs. It is normal for them to take several minutes, often around 5 minutes.
- Once a job is started, prefer waiting for progress polling instead of resubmitting the same command immediately.
- Safe requests retry automatically on transient network failures. Publish submission is not blindly retried to avoid duplicate publish tasks.

## Authentication

All requests use:

```http
Authorization: Bearer sk-gw-...
```

Success shape:

```json
{
  "success": true,
  "data": {},
  "message": ""
}
```

## Generation Flow

1. `POST /api/v1/documents/create`
2. `GET /api/v1/documents/progress/{id}`
3. `GET /api/v1/documents/detail/{id}`

Create request:

```json
{
  "keyword": "best hiking trails",
  "language": "en",
  "country": "united-states",
  "need_image": true,
  "idempotency_key": "client-generated-key"
}
```

Create response data:

```json
{
  "document_id": "123"
}
```

Progress response data:

```json
{
  "stage": 3,
  "stage_name": "Generating outline",
  "progress": 60,
  "status": "GENERATING",
  "completed": false
}
```

Detail response data:

```json
{
  "id": "123",
  "keyword": "best hiking trails",
  "title": "Best Hiking Trails",
  "meta_description": "....",
  "body": "....",
  "status": "DRAFT",
  "created_at": "2026-03-18T01:00:00.000000Z",
  "updated_at": "2026-03-18T01:05:00.000000Z"
}
```

## Publish Discovery

List configs:

- `GET /api/v1/publish-configs/list`

Response data:

```json
{
  "configs": [
    {
      "id": 12,
      "name": "My WordPress",
      "platform": "wordpress",
      "status": "active",
      "site_url": "https://example.com",
      "settings": {
        "default_status": "draft"
      }
    }
  ]
}
```

Get taxonomy:

- `GET /api/v1/publish-configs/taxonomy/{id}`

Response data includes:

- `publish_config`
- `categories`
- `tags`
- `category_count`
- `tag_count`

## Publish Flow

1. `POST /api/v1/documents/publish/submit/{id}`
2. `GET /api/v1/documents/publish/progress/{id}`

Submit request:

```json
{
  "publish_config_id": 12,
  "options": {
    "status": "publish",
    "categories": [3, 8],
    "tags": [21, 34],
    "remove_featured_from_content": true
  }
}
```

`categories` should contain only the final category IDs you want on the WordPress post. If you include both parent and child IDs, WordPress will assign both categories.

Publish progress response data:

```json
{
  "status": "completed",
  "progress": 100,
  "completed": true,
  "published_url": "https://example.com/post",
  "error_message": null
}
```

Normalized statuses used by the skill:

- `processing`
- `completed`
- `failed`

## Illustration Flow

1. `POST /api/v1/illustration/create`
2. `GET /api/v1/illustration/progress/{id}`
3. `GET /api/v1/illustration/detail/{id}`
4. `POST /api/v1/illustration/retry/{id}` (on failure when can_retry is true)

Create request:

```json
{
  "markdown": "# Title\n\nBody text...",
  "cover": true,
  "cover_style": "cinematic",
  "max_images": 3,
  "image_styles": ["photorealistic"],
  "platform": "general",
  "aspect_ratio": "16:9",
  "resolution": "1k",
  "idempotency_key": "client-generated-key"
}
```

Create parameters:

- `markdown` (required): article Markdown content, 1–50,000 characters.
- `cover` (optional, default `true`): generate a cover image.
- `cover_style` (optional, default `"cinematic"`): cover image style. Valid: `tech`, `minimal`, `editorial`, `cinematic`, `flat`, `abstract`, `sketch`, `watercolor`, `isometric`, `ink`, `comic`, `retro`. Invalid values silently fall back to `"cinematic"`.
- `max_images` (optional, default `3`): max body images to generate, 0–5. Set to `0` to generate cover only.
- `image_styles` (optional): image style presets array. Each entry must be one of the valid style values above. Invalid entries are silently dropped.
- `platform` (optional, default `"general"`): target platform.
- `aspect_ratio` (optional, default `"16:9"`): image aspect ratio. Valid: `16:9`, `1:1`, `3:2`, `4:3`, `3:4`, `9:16`, `2:1`, `1:2`, `21:9`, `9:21`. Invalid values silently fall back to `"16:9"`.
- `resolution` (optional, default `"1k"`): image resolution. Valid: `1k`, `2k`. Invalid values silently fall back to `"1k"`.
- `idempotency_key` (optional): client submit token for duplicate-submit guard (Redis-backed).
```

Create response data:

```json
{
  "id": "illust-123",
  "status": "PENDING",
  "created_at": "2026-06-07T10:00:00Z"
}
```

Progress response data:

```json
{
  "id": "illust-123",
  "task_type": "illustration",
  "status": "PROCESSING",
  "progress": 50,
  "error_message": null,
  "can_retry": false,
  "created_at": "2026-06-07T10:00:00Z"
}
```

Status values: `PENDING` (progress 0), `PROCESSING` (progress 50), `COMPLETED` (progress 100), `FAILED` (progress 0).

Detail response data:

```json
{
  "id": "illust-123",
  "status": "COMPLETED",
  "input": {"markdown": "...", "options": {...}},
  "output": {
    "success": true,
    "markdown": "# Title\n\n![Cover](https://cdn...)\n\nBody...",
    "images": [
      {"role": "cover", "url": "...", "alt": "...", "image_type": "cover", "style": "cinematic", "position": 0, "resolution": "1k", "aspect_ratio": "16:9"},
      {"role": "body", "url": "...", "alt": "...", "image_type": "illustration", ...}
    ],
    "stats": {"images_generated": 3, "duration_ms": 12500}
  },
  "error_message": null,
  "retry_count": 0,
  "can_retry": false
}
```

Retry response data:

```json
{"id": "illust-123", "status": "PENDING", "retry_count": 1}
```

## Local Bundle Behavior

Every article bundle is stored under:

```text
GW_ARTICLES_DIR/<slug>-<timestamp>/
```

Files:

- `article.md`: clean local Markdown
- `manifest.json`: local metadata, ids, publish state, stable keys
- `document.json`: raw document detail payload
- `generation.json`: raw generation request/create/progress payloads
- `publish.json`: append-only publish attempt log
- `illustrated.md`: illustrated Markdown with cover and body images (after illustrate)
- `illustration.json`: raw illustration request/response/progress payloads
- `images/`: downloaded images; `article.md` uses relative paths like `images/image-1.png`

If the bundle already exists and `manifest.json` contains `document_id`, `generate --article-dir ...` resumes from that document instead of creating a new one.

## Command Summary

- `init`: interactive `.env` setup
- `generate`: create or resume an article bundle
- `create`: alias of `generate`
- `illustrate`: add illustrations to a markdown article
- `configs`: list publish configs
- `taxonomy <id>`: inspect taxonomy
- `publish <article-dir>`: publish a saved article bundle
