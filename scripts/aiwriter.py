#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import http.client
import json
import mimetypes
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = SKILL_ROOT / ".env"
ENV_EXAMPLE_PATH = SKILL_ROOT / ".env.example"
IMAGE_STAGE_NAMES = {"Generating images", "Generating image", "Image generation", "Images"}
DEFAULT_IMAGE_POLL_INTERVAL_SECONDS = 20

DEFAULTS = {
    "GW_API_BASE_URL": "https://geowriter.ai",
    "GW_API_KEY": "",
    "GW_POLL_INTERVAL_SECONDS": "10",
    "GW_IMAGE_POLL_INTERVAL_SECONDS": str(DEFAULT_IMAGE_POLL_INTERVAL_SECONDS),
    "GW_PUBLISH_POLL_INTERVAL_SECONDS": "10",
    "GW_REQUEST_TIMEOUT_SECONDS": "30",
    "GW_GENERATION_TIMEOUT_SECONDS": "900",
    "GW_PUBLISH_TIMEOUT_SECONDS": "600",
    "GW_ARTICLES_DIR": str(SKILL_ROOT / "output" / "articles"),
}

DEFAULT_DOWNLOAD_RETRY_COUNT = 3
DEFAULT_DOWNLOAD_RETRY_DELAY_SECONDS = 1.0
DEFAULT_API_RETRY_COUNT = 3
DEFAULT_API_RETRY_DELAY_SECONDS = 10.0
DEFAULT_DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)
RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    pass


def parse_http_error_message(exc: error.HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        error_payload = json.loads(body)
        return error_payload.get("message") or body or f"HTTP {exc.code}"
    except json.JSONDecodeError:
        return body or str(exc) or f"HTTP {exc.code}"


def format_request_error(method: str, url: str, detail: str, *, attempts: int = 1) -> str:
    message = f"{method.upper()} {url} failed"
    if attempts > 1:
        message += f" after {attempts} attempts"
    return f"{message}: {detail}"


def log_request_retry(method: str, url: str, *, attempt: int, retries: int, detail: str, retry_delay: float) -> None:
    print(
        (
            f"[aiwriter] {method.upper()} {url} failed "
            f"(attempt {attempt}/{retries}): {detail}. Retrying in {retry_delay:g}s."
        ),
        file=sys.stderr,
    )


def log_long_running_notice(operation: str) -> None:
    print(
        (
            f"[aiwriter] {operation} may take several minutes, often around 5 minutes. "
            "Waiting for progress updates; do not resubmit unless the timeout is reached."
        ),
        file=sys.stderr,
    )


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')

    return values


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    return parse_env_text(path.read_text(encoding="utf-8"))


def merge_env_files(paths: list[Path]) -> dict[str, str]:
    merged: dict[str, str] = {}

    for path in paths:
        merged.update(load_env_file(path))

    return merged


def parse_key_value_overrides(items: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}

    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid override: {item!r}. Expected KEY=VALUE.")

        key, value = item.split("=", 1)
        overrides[key.strip()] = value.strip()

    return overrides


def build_env_content(values: dict[str, str]) -> str:
    lines = [
        "# AIWriter / GeoWriter Integration API credentials",
        f"GW_API_BASE_URL={values['GW_API_BASE_URL']}",
        f"GW_API_KEY={values['GW_API_KEY']}",
        "",
        "# Request tuning",
        f"GW_POLL_INTERVAL_SECONDS={values['GW_POLL_INTERVAL_SECONDS']}",
        f"GW_IMAGE_POLL_INTERVAL_SECONDS={values['GW_IMAGE_POLL_INTERVAL_SECONDS']}",
        f"GW_PUBLISH_POLL_INTERVAL_SECONDS={values['GW_PUBLISH_POLL_INTERVAL_SECONDS']}",
        f"GW_REQUEST_TIMEOUT_SECONDS={values['GW_REQUEST_TIMEOUT_SECONDS']}",
        f"GW_GENERATION_TIMEOUT_SECONDS={values['GW_GENERATION_TIMEOUT_SECONDS']}",
        f"GW_PUBLISH_TIMEOUT_SECONDS={values['GW_PUBLISH_TIMEOUT_SECONDS']}",
        "",
        "# Local article bundle root",
        f"GW_ARTICLES_DIR={values['GW_ARTICLES_DIR']}",
        "",
    ]

    return "\n".join(lines)


def initialize_env_file(env_path: Path, values: dict[str, str], *, force: bool = False) -> Path:
    if env_path.exists() and not force:
        raise FileExistsError(f"{env_path} already exists. Use --force to overwrite it.")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(build_env_content(values), encoding="utf-8")
    return env_path


def prompt_value(prompt: str, default: str | None = None, *, secret: bool = False) -> str:
    label = f"{prompt} [{default}]" if default else prompt

    while True:
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        if value:
            return value.strip()
        if default is not None:
            return default
        print("A value is required.", file=sys.stderr)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "article"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def infer_datetime(document: dict[str, Any] | None = None) -> datetime:
    document = document or {}
    for key in ("created_at", "updated_at"):
        raw_value = document.get(key)
        if not raw_value or not isinstance(raw_value, str):
            continue

        normalized = raw_value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            continue

    return datetime.now()


def build_article_dir(
    *,
    keyword: str,
    article_key: str,
    articles_dir: Path,
    timestamp: datetime | None = None,
) -> Path:
    timestamp = timestamp or datetime.now()
    slug = slugify(keyword)
    dirname = f"{slug}-{timestamp:%Y%m%d-%H%M%S}"
    return articles_dir / dirname


def bundle_paths(article_dir: Path) -> dict[str, Path]:
    return {
        "manifest": article_dir / "manifest.json",
        "markdown": article_dir / "article.md",
        "document": article_dir / "document.json",
        "generation": article_dir / "generation.json",
        "publish": article_dir / "publish.json",
        "images": article_dir / "images",
    }


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    return json.loads(path.read_text(encoding="utf-8"))


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def parse_csv_ints(value: str | None) -> list[int]:
    if not value:
        return []

    items: list[int] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        items.append(int(item))

    return items


def resolve_settings(args: argparse.Namespace) -> dict[str, Any]:
    env_paths = [Path(path).expanduser() for path in getattr(args, "env", [str(DEFAULT_ENV_PATH)])]
    env_values = DEFAULTS | merge_env_files(env_paths)
    env_values.update(parse_key_value_overrides(getattr(args, "set", [])))

    if getattr(args, "base_url", None):
        env_values["GW_API_BASE_URL"] = args.base_url
    if getattr(args, "api_key", None):
        env_values["GW_API_KEY"] = args.api_key
    if getattr(args, "poll_interval", None) is not None:
        env_values["GW_POLL_INTERVAL_SECONDS"] = str(args.poll_interval)
    if getattr(args, "image_poll_interval", None) is not None:
        env_values["GW_IMAGE_POLL_INTERVAL_SECONDS"] = str(args.image_poll_interval)
    if getattr(args, "publish_poll_interval", None) is not None:
        env_values["GW_PUBLISH_POLL_INTERVAL_SECONDS"] = str(args.publish_poll_interval)
    if getattr(args, "request_timeout", None) is not None:
        env_values["GW_REQUEST_TIMEOUT_SECONDS"] = str(args.request_timeout)
    if getattr(args, "generation_timeout", None) is not None:
        env_values["GW_GENERATION_TIMEOUT_SECONDS"] = str(args.generation_timeout)
    if getattr(args, "publish_timeout", None) is not None:
        env_values["GW_PUBLISH_TIMEOUT_SECONDS"] = str(args.publish_timeout)
    if getattr(args, "articles_dir", None):
        env_values["GW_ARTICLES_DIR"] = args.articles_dir

    if not env_values["GW_API_BASE_URL"]:
        raise ValueError("GW_API_BASE_URL is required.")
    if not env_values["GW_API_KEY"]:
        raise ValueError("GW_API_KEY is required.")

    return {
        "base_url": env_values["GW_API_BASE_URL"].rstrip("/"),
        "api_key": env_values["GW_API_KEY"],
        "poll_interval": int(env_values["GW_POLL_INTERVAL_SECONDS"]),
        "image_poll_interval": int(env_values["GW_IMAGE_POLL_INTERVAL_SECONDS"]),
        "publish_poll_interval": int(env_values["GW_PUBLISH_POLL_INTERVAL_SECONDS"]),
        "request_timeout": int(env_values["GW_REQUEST_TIMEOUT_SECONDS"]),
        "generation_timeout": int(env_values["GW_GENERATION_TIMEOUT_SECONDS"]),
        "publish_timeout": int(env_values["GW_PUBLISH_TIMEOUT_SECONDS"]),
        "articles_dir": Path(env_values["GW_ARTICLES_DIR"]).expanduser(),
    }


def request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    retries: int = DEFAULT_API_RETRY_COUNT,
    retry_delay: float = DEFAULT_API_RETRY_DELAY_SECONDS,
    retryable: bool | None = None,
) -> dict[str, Any]:
    data: bytes | None = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url=url, method=method.upper(), headers=headers, data=data)
    retryable = method.upper() == "GET" if retryable is None else retryable
    max_attempts = retries if retryable else 1

    for attempt in range(1, max_attempts + 1):
        try:
            with request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = parse_http_error_message(exc)
            if retryable and exc.code in RETRYABLE_HTTP_STATUS_CODES and attempt < max_attempts:
                log_request_retry(
                    method,
                    url,
                    attempt=attempt,
                    retries=max_attempts,
                    detail=f"HTTP {exc.code}: {detail}",
                    retry_delay=retry_delay,
                )
                time.sleep(retry_delay)
                continue
            raise ApiError(
                format_request_error(
                    method,
                    url,
                    detail,
                    attempts=attempt,
                )
            ) from exc
        except (
            error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionResetError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as exc:
            detail = str(exc) or exc.__class__.__name__
            if retryable and attempt < max_attempts:
                log_request_retry(
                    method,
                    url,
                    attempt=attempt,
                    retries=max_attempts,
                    detail=detail,
                    retry_delay=retry_delay,
                )
                time.sleep(retry_delay)
                continue
            raise ApiError(
                format_request_error(
                    method,
                    url,
                    detail,
                    attempts=attempt,
                )
            ) from exc

        try:
            response_payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ApiError(f"{method.upper()} {url} returned non-JSON response.") from exc

        if not response_payload.get("success", False):
            raise ApiError(response_payload.get("message") or f"{method.upper()} {url} failed.")

        return response_payload.get("data") or {}

    raise ApiError(format_request_error(method, url, "request exhausted without a response", attempts=max_attempts))


def build_download_headers(url: str) -> dict[str, str]:
    parsed = parse.urlparse(url)
    headers = {
        "User-Agent": DEFAULT_DOWNLOAD_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }

    if parsed.netloc.endswith("imgcdn.geowriter.ai"):
        headers["Referer"] = "https://geowriter.ai/"
        headers["Origin"] = "https://geowriter.ai"

    return headers


def download_binary(
    url: str,
    *,
    timeout: int = 30,
    retries: int = DEFAULT_DOWNLOAD_RETRY_COUNT,
    retry_delay: float = DEFAULT_DOWNLOAD_RETRY_DELAY_SECONDS,
) -> tuple[bytes, str | None]:
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        req = request.Request(url=url, method="GET", headers=build_download_headers(url))

        try:
            with request.urlopen(req, timeout=timeout) as response:
                return response.read(), response.headers.get_content_type()
        except http.client.IncompleteRead as exc:
            last_error = exc
        except error.HTTPError as exc:
            last_error = exc
            if exc.code not in {403, 408, 429, 500, 502, 503, 504}:
                raise ApiError(f"GET {url} failed: HTTP {exc.code}") from exc
        except error.URLError as exc:
            last_error = exc

        if attempt < retries:
            time.sleep(retry_delay * attempt)

    if isinstance(last_error, error.HTTPError):
        raise ApiError(f"GET {url} failed after {retries} attempts: HTTP {last_error.code}") from last_error
    if isinstance(last_error, error.URLError):
        raise ApiError(f"GET {url} failed after {retries} attempts: {last_error}") from last_error
    if isinstance(last_error, http.client.IncompleteRead):
        raise ApiError(f"GET {url} failed after {retries} attempts: incomplete response body") from last_error
    raise ApiError(f"GET {url} failed after {retries} attempts.")


def create_document(
    *,
    base_url: str,
    api_key: str,
    keyword: str,
    language: str,
    country: str,
    need_image: bool,
    idempotency_key: str,
    request_timeout: int,
) -> dict[str, Any]:
    return request_json(
        "POST",
        f"{base_url}/api/v1/documents/create",
        api_key=api_key,
        payload={
            "keyword": keyword,
            "language": language,
            "country": country,
            "need_image": need_image,
            "idempotency_key": idempotency_key,
        },
        timeout=request_timeout,
        retryable=True,
    )


def get_document_progress(*, base_url: str, api_key: str, document_id: str, request_timeout: int) -> dict[str, Any]:
    return request_json(
        "GET",
        f"{base_url}/api/v1/documents/progress/{document_id}",
        api_key=api_key,
        timeout=request_timeout,
    )


def get_document(*, base_url: str, api_key: str, document_id: str, request_timeout: int) -> dict[str, Any]:
    return request_json(
        "GET",
        f"{base_url}/api/v1/documents/detail/{document_id}",
        api_key=api_key,
        timeout=request_timeout,
    )


def list_publish_configs(*, base_url: str, api_key: str, request_timeout: int) -> dict[str, Any]:
    return request_json(
        "GET",
        f"{base_url}/api/v1/publish-configs/list",
        api_key=api_key,
        timeout=request_timeout,
    )


def get_publish_taxonomy(
    *,
    base_url: str,
    api_key: str,
    publish_config_id: int,
    request_timeout: int,
) -> dict[str, Any]:
    return request_json(
        "GET",
        f"{base_url}/api/v1/publish-configs/taxonomy/{publish_config_id}",
        api_key=api_key,
        timeout=request_timeout,
    )


def submit_publish_task(
    *,
    base_url: str,
    api_key: str,
    document_id: str,
    publish_config_id: int,
    options: dict[str, Any],
    request_timeout: int,
) -> dict[str, Any]:
    return request_json(
        "POST",
        f"{base_url}/api/v1/documents/publish/submit/{document_id}",
        api_key=api_key,
        payload={
            "publish_config_id": publish_config_id,
            "options": options,
        },
        timeout=request_timeout,
    )


def get_publish_progress(*, base_url: str, api_key: str, document_id: str, request_timeout: int) -> dict[str, Any]:
    return request_json(
        "GET",
        f"{base_url}/api/v1/documents/publish/progress/{document_id}",
        api_key=api_key,
        timeout=request_timeout,
    )


def resolve_poll_interval(progress: dict[str, Any], default_poll_interval: int, image_poll_interval: int) -> int:
    stage_name = str(progress.get("stage_name") or "")
    stage = progress.get("stage")

    if stage == 5 or stage_name in IMAGE_STAGE_NAMES:
        return image_poll_interval

    return default_poll_interval


def wait_for_document(
    *,
    base_url: str,
    api_key: str,
    document_id: str,
    poll_interval: int,
    image_poll_interval: int,
    request_timeout: int,
    generation_timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = time.time()
    log_long_running_notice("Document generation")

    while True:
        progress = get_document_progress(
            base_url=base_url,
            api_key=api_key,
            document_id=document_id,
            request_timeout=request_timeout,
        )

        stage_name = progress.get("stage_name") or "Waiting"
        percent = progress.get("progress", 0)
        status = progress.get("status", "UNKNOWN")
        print(f"[aiwriter] generate {percent}% | {status} | {stage_name}", file=sys.stderr)

        if progress.get("completed") or status != "GENERATING":
            document = get_document(
                base_url=base_url,
                api_key=api_key,
                document_id=document_id,
                request_timeout=request_timeout,
            )
            return progress, document

        if (time.time() - started_at) >= generation_timeout:
            raise TimeoutError(f"Timed out after {generation_timeout} seconds waiting for document {document_id}.")

        time.sleep(resolve_poll_interval(progress, poll_interval, image_poll_interval))


def wait_for_publish(
    *,
    base_url: str,
    api_key: str,
    document_id: str,
    poll_interval: int,
    request_timeout: int,
    publish_timeout: int,
) -> dict[str, Any]:
    started_at = time.time()
    log_long_running_notice("Publishing")

    while True:
        try:
            progress = get_publish_progress(
                base_url=base_url,
                api_key=api_key,
                document_id=document_id,
                request_timeout=request_timeout,
            )
        except ApiError as exc:
            if "Publish record not found." not in str(exc):
                raise
            progress = {
                "status": "processing",
                "progress": 0,
                "completed": False,
                "published_url": None,
                "error_message": None,
            }

        status = progress.get("status", "processing")
        percent = progress.get("progress", 0)
        print(f"[aiwriter] publish {percent}% | {status}", file=sys.stderr)

        if progress.get("completed") or status in {"completed", "failed"}:
            return progress

        if (time.time() - started_at) >= publish_timeout:
            raise TimeoutError(f"Timed out after {publish_timeout} seconds waiting for publish {document_id}.")

        time.sleep(poll_interval)


def format_document_markdown(document: dict[str, Any]) -> str:
    title = (document.get("title") or "Untitled").strip()
    excerpt = (document.get("excerpt") or "").strip()
    body = (document.get("body") or "").rstrip()

    parts = [f"# {title}", ""]

    if excerpt:
        parts.extend([excerpt, ""])

    if body:
        parts.append(body)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def infer_image_extension(url: str, content_type: str | None) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix:
        return suffix

    guessed = mimetypes.guess_extension(content_type or "")
    if guessed:
        return guessed

    return ".bin"


def localize_markdown_images(
    markdown: str,
    *,
    images_dir: Path,
    request_timeout: int,
) -> tuple[str, list[str]]:
    pattern = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>https?://[^)\s]+)(?P<tail>[^)]*)\)")
    failures: list[str] = []
    cache: dict[str, str] = {}
    images_dir.mkdir(parents=True, exist_ok=True)

    def replace(match: re.Match[str]) -> str:
        image_url = match.group("url")
        tail = match.group("tail") or ""

        if image_url in cache:
            return f"![{match.group('alt')}]({cache[image_url]}{tail})"

        try:
            data, content_type = download_binary(image_url, timeout=request_timeout)
        except ApiError:
            failures.append(image_url)
            return match.group(0)

        extension = infer_image_extension(image_url, content_type)
        filename = f"image-{len(cache) + 1}{extension}"
        image_path = images_dir / filename
        image_path.write_bytes(data)
        relative_path = f"images/{filename}"
        cache[image_url] = relative_path
        return f"![{match.group('alt')}]({relative_path}{tail})"

    return pattern.sub(replace, markdown), failures


def prepare_article_bundle(args: argparse.Namespace, settings: dict[str, Any]) -> tuple[Path, dict[str, Any], dict[str, Path]]:
    existing_manifest: dict[str, Any] = {}
    article_dir: Path | None = None

    if args.article_dir:
        article_dir = Path(args.article_dir).expanduser()
        existing_manifest = load_json_file(bundle_paths(article_dir)["manifest"], {})

    keyword = args.keyword or existing_manifest.get("keyword")
    if not keyword:
        raise ValueError("keyword is required for a new article bundle.")

    article_key = args.article_key or existing_manifest.get("article_key") or str(uuid.uuid4())
    idempotency_key = args.idempotency_key or existing_manifest.get("idempotency_key") or article_key
    timestamp = infer_datetime(existing_manifest)

    if article_dir is None:
        article_dir = build_article_dir(
            keyword=keyword,
            article_key=article_key,
            articles_dir=settings["articles_dir"],
            timestamp=timestamp,
        )

    article_dir.mkdir(parents=True, exist_ok=True)
    paths = bundle_paths(article_dir)

    need_image = args.need_image if args.need_image is not None else existing_manifest.get("need_image", True)
    download_images = (
        args.download_images
        if args.download_images is not None
        else existing_manifest.get("download_images", True)
    )
    language = args.language or existing_manifest.get("language") or "en"
    country = args.country or existing_manifest.get("country") or "united-states"

    manifest = {
        **existing_manifest,
        "article_key": article_key,
        "idempotency_key": idempotency_key,
        "keyword": keyword,
        "language": language,
        "country": country,
        "need_image": need_image,
        "download_images": download_images,
        "article_dir": str(article_dir),
        "created_at": existing_manifest.get("created_at") or utc_now_iso(),
        "updated_at": utc_now_iso(),
        "files": {
            "markdown": paths["markdown"].name,
            "document": paths["document"].name,
            "generation": paths["generation"].name,
            "publish": paths["publish"].name,
            "images_dir": paths["images"].name,
        },
        "publish": existing_manifest.get("publish") or {
            "status": "not_started",
            "published_url": None,
            "error_message": None,
            "publish_config_id": None,
        },
    }
    write_json_file(paths["manifest"], manifest)
    return article_dir, manifest, paths


def sync_generated_bundle(
    *,
    article_dir: Path,
    manifest: dict[str, Any],
    paths: dict[str, Path],
    document: dict[str, Any],
    progress: dict[str, Any],
    generation_state: dict[str, Any],
    request_timeout: int,
) -> dict[str, Any]:
    markdown = format_document_markdown(document)
    image_failures: list[str] = []
    if manifest.get("download_images", True):
        markdown, image_failures = localize_markdown_images(
            markdown,
            images_dir=paths["images"],
            request_timeout=request_timeout,
        )

    write_text_file(paths["markdown"], markdown)
    write_json_file(paths["document"], document)

    generation_state["final_progress"] = progress
    generation_state["document"] = document
    generation_state["image_failures"] = image_failures
    generation_state["completed_at"] = utc_now_iso()
    write_json_file(paths["generation"], generation_state)

    manifest.update(
        {
            "document_id": str(document.get("id") or manifest.get("document_id") or ""),
            "title": document.get("title"),
            "document_status": document.get("status") or progress.get("status"),
            "document_created_at": document.get("created_at"),
            "document_updated_at": document.get("updated_at"),
            "generated_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "image_failures": image_failures,
        }
    )
    write_json_file(paths["manifest"], manifest)

    return {
        "article_dir": str(article_dir),
        "markdown_path": str(paths["markdown"]),
        "manifest_path": str(paths["manifest"]),
        "document_path": str(paths["document"]),
        "generation_path": str(paths["generation"]),
        "document_id": manifest["document_id"],
        "idempotency_key": manifest["idempotency_key"],
        "status": manifest["document_status"],
        "title": manifest.get("title"),
        "image_failures": image_failures,
    }


def cmd_init(args: argparse.Namespace) -> int:
    example_values = DEFAULTS | load_env_file(ENV_EXAMPLE_PATH)
    env_path = Path(args.env).expanduser()

    values = {
        "GW_API_BASE_URL": args.base_url or prompt_value("GeoWriter base URL", example_values["GW_API_BASE_URL"]),
        "GW_API_KEY": args.api_key or prompt_value("GeoWriter API key", None, secret=True),
        "GW_POLL_INTERVAL_SECONDS": str(
            args.poll_interval or prompt_value("Generation poll interval seconds", example_values["GW_POLL_INTERVAL_SECONDS"])
        ),
        "GW_IMAGE_POLL_INTERVAL_SECONDS": str(
            args.image_poll_interval
            or prompt_value("Image-stage poll interval seconds", example_values["GW_IMAGE_POLL_INTERVAL_SECONDS"])
        ),
        "GW_PUBLISH_POLL_INTERVAL_SECONDS": str(
            args.publish_poll_interval
            or prompt_value("Publish poll interval seconds", example_values["GW_PUBLISH_POLL_INTERVAL_SECONDS"])
        ),
        "GW_REQUEST_TIMEOUT_SECONDS": str(
            args.request_timeout or prompt_value("Request timeout seconds", example_values["GW_REQUEST_TIMEOUT_SECONDS"])
        ),
        "GW_GENERATION_TIMEOUT_SECONDS": str(
            args.generation_timeout
            or prompt_value("Generation timeout seconds", example_values["GW_GENERATION_TIMEOUT_SECONDS"])
        ),
        "GW_PUBLISH_TIMEOUT_SECONDS": str(
            args.publish_timeout or prompt_value("Publish timeout seconds", example_values["GW_PUBLISH_TIMEOUT_SECONDS"])
        ),
        "GW_ARTICLES_DIR": args.articles_dir or prompt_value("Article bundle root", example_values["GW_ARTICLES_DIR"]),
    }

    initialize_env_file(env_path, values, force=args.force)
    print(f"Initialized {env_path}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    article_dir, manifest, paths = prepare_article_bundle(args, settings)

    generation_state = load_json_file(paths["generation"], {})
    generation_state["request"] = {
        "keyword": manifest["keyword"],
        "language": manifest["language"],
        "country": manifest["country"],
        "need_image": manifest["need_image"],
        "idempotency_key": manifest["idempotency_key"],
    }

    document_id = manifest.get("document_id")
    if document_id:
        progress = get_document_progress(
            base_url=settings["base_url"],
            api_key=settings["api_key"],
            document_id=str(document_id),
            request_timeout=settings["request_timeout"],
        )
        if progress.get("completed") or progress.get("status") != "GENERATING":
            document = get_document(
                base_url=settings["base_url"],
                api_key=settings["api_key"],
                document_id=str(document_id),
                request_timeout=settings["request_timeout"],
            )
        else:
            progress, document = wait_for_document(
                base_url=settings["base_url"],
                api_key=settings["api_key"],
                document_id=str(document_id),
                poll_interval=settings["poll_interval"],
                image_poll_interval=settings["image_poll_interval"],
                request_timeout=settings["request_timeout"],
                generation_timeout=settings["generation_timeout"],
            )
    else:
        created = create_document(
            base_url=settings["base_url"],
            api_key=settings["api_key"],
            keyword=manifest["keyword"],
            language=manifest["language"],
            country=manifest["country"],
            need_image=bool(manifest["need_image"]),
            idempotency_key=manifest["idempotency_key"],
            request_timeout=settings["request_timeout"],
        )
        document_id = str(created["document_id"])
        manifest["document_id"] = document_id
        manifest["updated_at"] = utc_now_iso()
        write_json_file(paths["manifest"], manifest)

        generation_state["create_response"] = created
        write_json_file(paths["generation"], generation_state)

        progress, document = wait_for_document(
            base_url=settings["base_url"],
            api_key=settings["api_key"],
            document_id=document_id,
            poll_interval=settings["poll_interval"],
            image_poll_interval=settings["image_poll_interval"],
            request_timeout=settings["request_timeout"],
            generation_timeout=settings["generation_timeout"],
        )

    summary = sync_generated_bundle(
        article_dir=article_dir,
        manifest=manifest,
        paths=paths,
        document=document,
        progress=progress,
        generation_state=generation_state,
        request_timeout=settings["request_timeout"],
    )

    if args.format == "json":
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=args.json_indent) + "\n")
    else:
        sys.stdout.write(f"{summary['markdown_path']}\n")

    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    article_dir = Path(args.article_dir).expanduser()
    paths = bundle_paths(article_dir)
    manifest = load_json_file(paths["manifest"], {})
    if not manifest:
        raise ValueError(f"Article bundle not found: {article_dir}")

    document_id = manifest.get("document_id")
    if not document_id:
        document_payload = load_json_file(paths["document"], {})
        document_id = document_payload.get("id")
    if not document_id:
        raise ValueError("document_id is missing. Run generate first.")

    publish_config_id = args.publish_config_id or manifest.get("publish", {}).get("publish_config_id")
    if not publish_config_id:
        raise ValueError("publish_config_id is required. Use the configs command to discover available configs.")

    options: dict[str, Any] = {}
    if args.status:
        options["status"] = args.status
    categories = parse_csv_ints(args.categories)
    if categories:
        options["categories"] = categories
    tags = parse_csv_ints(args.tags)
    if tags:
        options["tags"] = tags
    if args.remove_featured_from_content is not None:
        options["remove_featured_from_content"] = args.remove_featured_from_content

    submit_response = submit_publish_task(
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        document_id=str(document_id),
        publish_config_id=int(publish_config_id),
        options=options,
        request_timeout=settings["request_timeout"],
    )
    progress = wait_for_publish(
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        document_id=str(document_id),
        poll_interval=settings["publish_poll_interval"],
        request_timeout=settings["request_timeout"],
        publish_timeout=settings["publish_timeout"],
    )

    publish_log = load_json_file(paths["publish"], {"attempts": []})
    publish_log.setdefault("attempts", []).append(
        {
            "submitted_at": utc_now_iso(),
            "request": {
                "document_id": str(document_id),
                "publish_config_id": int(publish_config_id),
                "options": options,
            },
            "submit_response": submit_response,
            "final_progress": progress,
        }
    )
    write_json_file(paths["publish"], publish_log)

    manifest["publish"] = {
        "status": progress.get("status"),
        "published_url": progress.get("published_url"),
        "error_message": progress.get("error_message"),
        "publish_config_id": int(publish_config_id),
        "last_submitted_at": utc_now_iso(),
    }
    manifest["updated_at"] = utc_now_iso()
    write_json_file(paths["manifest"], manifest)

    summary = {
        "article_dir": str(article_dir),
        "document_id": str(document_id),
        "publish_config_id": int(publish_config_id),
        "status": progress.get("status"),
        "published_url": progress.get("published_url"),
        "error_message": progress.get("error_message"),
        "publish_path": str(paths["publish"]),
        "manifest_path": str(paths["manifest"]),
    }

    if args.format == "json":
        sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=args.json_indent) + "\n")
    else:
        sys.stdout.write(f"{summary['status']} {summary['published_url'] or ''}\n".rstrip() + "\n")

    if progress.get("status") == "failed":
        return 1

    return 0


def cmd_configs(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    payload = list_publish_configs(
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        request_timeout=settings["request_timeout"],
    )
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=args.json_indent) + "\n")
    return 0


def cmd_taxonomy(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    payload = get_publish_taxonomy(
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        publish_config_id=args.publish_config_id,
        request_timeout=settings["request_timeout"],
    )
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=args.json_indent) + "\n")
    return 0


def add_common_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", action="append", default=[str(DEFAULT_ENV_PATH)], help="Env file path. Repeat to merge multiple files.")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="Inline environment override. Repeatable.")
    parser.add_argument("--base-url", help="Override GW_API_BASE_URL.")
    parser.add_argument("--api-key", help="Override GW_API_KEY.")
    parser.add_argument("--poll-interval", type=int, help="Override GW_POLL_INTERVAL_SECONDS.")
    parser.add_argument("--image-poll-interval", type=int, help="Override GW_IMAGE_POLL_INTERVAL_SECONDS.")
    parser.add_argument("--publish-poll-interval", type=int, help="Override GW_PUBLISH_POLL_INTERVAL_SECONDS.")
    parser.add_argument("--request-timeout", type=int, help="Override GW_REQUEST_TIMEOUT_SECONDS.")
    parser.add_argument("--generation-timeout", type=int, help="Override GW_GENERATION_TIMEOUT_SECONDS.")
    parser.add_argument("--publish-timeout", type=int, help="Override GW_PUBLISH_TIMEOUT_SECONDS.")
    parser.add_argument("--articles-dir", help="Override GW_ARTICLES_DIR.")


def add_generate_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str, help_text: str) -> None:
    generate_parser = subparsers.add_parser(name, help=help_text)
    generate_parser.add_argument("keyword", nargs="?", help="Article keyword. Optional when reusing --article-dir.")
    generate_parser.add_argument("--article-dir", help="Existing or desired local article bundle directory.")
    generate_parser.add_argument("--article-key", help="Stable local article key. Defaults to a UUID.")
    generate_parser.add_argument("--idempotency-key", help="Stable request idempotency key. Defaults to the article key.")
    generate_parser.add_argument("--language", help="Target language code. Default: en")
    generate_parser.add_argument("--country", help="Target country slug. Default: united-states")
    generate_parser.add_argument("--need-image", default=None, action=argparse.BooleanOptionalAction, help="Enable or disable generated images.")
    generate_parser.add_argument("--download-images", default=None, action=argparse.BooleanOptionalAction, help="Download remote images into images/.")
    generate_parser.add_argument("--format", choices=["path", "json"], default="path", help="CLI output format.")
    generate_parser.add_argument("--json-indent", type=int, default=2, help="Indent for JSON output.")
    add_common_runtime_options(generate_parser)
    generate_parser.set_defaults(func=cmd_generate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create and publish GeoWriter articles through the integration API. "
            "Remote generation and publish steps can take several minutes, often around five minutes."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Interactively initialize a .env file.")
    init_parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="Destination .env path.")
    init_parser.add_argument("--base-url", help="GeoWriter base URL.")
    init_parser.add_argument("--api-key", help="GeoWriter API key.")
    init_parser.add_argument("--poll-interval", type=int, help="Generation poll interval seconds.")
    init_parser.add_argument("--image-poll-interval", type=int, help="Image-stage poll interval seconds.")
    init_parser.add_argument("--publish-poll-interval", type=int, help="Publish poll interval seconds.")
    init_parser.add_argument("--request-timeout", type=int, help="Per-request timeout seconds.")
    init_parser.add_argument("--generation-timeout", type=int, help="End-to-end generation timeout seconds.")
    init_parser.add_argument("--publish-timeout", type=int, help="End-to-end publish timeout seconds.")
    init_parser.add_argument("--articles-dir", help="Default local article bundle root.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing .env file.")
    init_parser.set_defaults(func=cmd_init)

    add_generate_parser(subparsers, "generate", "Generate a GEO article and store it as a local article bundle.")
    add_generate_parser(subparsers, "create", "Backward-compatible alias of generate.")

    publish_parser = subparsers.add_parser("publish", help="Publish a generated article bundle to WordPress.")
    publish_parser.add_argument("article_dir", help="Article bundle directory created by generate.")
    publish_parser.add_argument("--publish-config-id", type=int, help="Publish config ID returned by the configs command.")
    publish_parser.add_argument("--status", choices=["publish", "draft"], help="Target WordPress status.")
    publish_parser.add_argument("--categories", help="Comma-separated WordPress category IDs.")
    publish_parser.add_argument("--tags", help="Comma-separated WordPress tag IDs.")
    publish_parser.add_argument("--remove-featured-from-content", default=None, action=argparse.BooleanOptionalAction, help="Remove the featured image from the post body.")
    publish_parser.add_argument("--format", choices=["text", "json"], default="text", help="CLI output format.")
    publish_parser.add_argument("--json-indent", type=int, default=2, help="Indent for JSON output.")
    add_common_runtime_options(publish_parser)
    publish_parser.set_defaults(func=cmd_publish)

    configs_parser = subparsers.add_parser("configs", help="List publish configs available to the current API key.")
    configs_parser.add_argument("--json-indent", type=int, default=2, help="Indent for JSON output.")
    add_common_runtime_options(configs_parser)
    configs_parser.set_defaults(func=cmd_configs)

    taxonomy_parser = subparsers.add_parser("taxonomy", help="Fetch taxonomy for a publish config.")
    taxonomy_parser.add_argument("publish_config_id", type=int, help="Publish config ID.")
    taxonomy_parser.add_argument("--json-indent", type=int, default=2, help="Indent for JSON output.")
    add_common_runtime_options(taxonomy_parser)
    taxonomy_parser.set_defaults(func=cmd_taxonomy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except (ApiError, FileExistsError, TimeoutError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
