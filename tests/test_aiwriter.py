from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "aiwriter.py"
SPEC = importlib.util.spec_from_file_location("aiwriter", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class AIWriterTest(unittest.TestCase):
    def test_parse_env_text(self) -> None:
        values = MODULE.parse_env_text(
            """
            # comment
            GW_API_BASE_URL=https://example.com
            GW_API_KEY="sk-gw-test"
            INVALID
            """
        )

        self.assertEqual(values["GW_API_BASE_URL"], "https://example.com")
        self.assertEqual(values["GW_API_KEY"], "sk-gw-test")

    def test_initialize_env_file_writes_all_new_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            MODULE.initialize_env_file(
                env_path,
                {
                    "GW_API_BASE_URL": "https://example.com",
                    "GW_API_KEY": "sk-gw-test",
                    "GW_POLL_INTERVAL_SECONDS": "10",
                    "GW_IMAGE_POLL_INTERVAL_SECONDS": "20",
                    "GW_PUBLISH_POLL_INTERVAL_SECONDS": "11",
                    "GW_REQUEST_TIMEOUT_SECONDS": "30",
                    "GW_GENERATION_TIMEOUT_SECONDS": "900",
                    "GW_PUBLISH_TIMEOUT_SECONDS": "600",
                    "GW_ARTICLES_DIR": "/tmp/articles",
                },
            )

            content = env_path.read_text(encoding="utf-8")
            self.assertIn("GW_API_BASE_URL=https://example.com", content)
            self.assertIn("GW_API_KEY=sk-gw-test", content)
            self.assertIn("GW_PUBLISH_POLL_INTERVAL_SECONDS=11", content)
            self.assertIn("GW_PUBLISH_TIMEOUT_SECONDS=600", content)
            self.assertIn("GW_ARTICLES_DIR=/tmp/articles", content)

    def test_build_article_dir_uses_slug_and_timestamp(self) -> None:
        path = MODULE.build_article_dir(
            keyword="Best Hiking Trails",
            article_key="article-key-123",
            articles_dir=Path("/tmp/articles"),
            timestamp=MODULE.datetime(2026, 3, 18, 9, 0, 0),
        )

        self.assertEqual(
            path,
            Path("/tmp/articles/best-hiking-trails-20260318-090000"),
        )

    def test_localize_markdown_images_downloads_relative_images_and_keeps_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            images_dir = Path(tmp_dir) / "images"
            markdown = "\n".join(
                [
                    "![Hero](https://cdn.example.com/hero.png)",
                    "![Repeat](https://cdn.example.com/hero.png)",
                    "![Broken](https://cdn.example.com/missing.jpg)",
                ]
            )

            def fake_download(url: str, *, timeout: int) -> tuple[bytes, str | None]:
                if url.endswith("missing.jpg"):
                    raise MODULE.ApiError("boom")
                return b"png", "image/png"

            with mock.patch.object(MODULE, "download_binary", side_effect=fake_download):
                localized, failures = MODULE.localize_markdown_images(
                    markdown,
                    images_dir=images_dir,
                    request_timeout=30,
                )

            self.assertIn("images/image-1.png", localized)
            self.assertEqual(localized.count("images/image-1.png"), 2)
            self.assertIn("https://cdn.example.com/missing.jpg", localized)
            self.assertEqual(failures, ["https://cdn.example.com/missing.jpg"])
            self.assertTrue((images_dir / "image-1.png").exists())

    def test_build_download_headers_adds_geowriter_cdn_headers(self) -> None:
        headers = MODULE.build_download_headers(
            "https://imgcdn.geowriter.ai/public/images/2026/03/demo.png?token=1"
        )

        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Origin"], "https://geowriter.ai")
        self.assertEqual(headers["Referer"], "https://geowriter.ai/")
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_download_binary_retries_after_incomplete_read(self) -> None:
        url = "https://imgcdn.geowriter.ai/demo.png"
        response = mock.MagicMock()
        response.headers.get_content_type.return_value = "image/png"
        response.read.return_value = b"image-data"
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with mock.patch.object(
            MODULE.request,
            "urlopen",
            side_effect=[
                MODULE.http.client.IncompleteRead(b"partial"),
                response,
            ],
        ) as urlopen:
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                data, content_type = MODULE.download_binary(url, timeout=30)

        self.assertEqual(data, b"image-data")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleep.call_args_list, [mock.call(1.0)])

    def test_download_binary_retries_403_then_succeeds(self) -> None:
        url = "https://imgcdn.geowriter.ai/demo.png"
        response = mock.MagicMock()
        response.headers.get_content_type.return_value = "image/png"
        response.read.return_value = b"image-data"
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        http_error = urllib.error.HTTPError(url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(b""))

        with mock.patch.object(
            MODULE.request,
            "urlopen",
            side_effect=[
                http_error,
                response,
            ],
        ) as urlopen:
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                data, content_type = MODULE.download_binary(url, timeout=30)

        self.assertEqual(data, b"image-data")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleep.call_args_list, [mock.call(1.0)])
        http_error.close()

    def test_resolve_settings_prefers_cli_over_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "GW_API_BASE_URL=https://from-env.example.com",
                        "GW_API_KEY=sk-gw-env",
                        "GW_POLL_INTERVAL_SECONDS=7",
                        "GW_IMAGE_POLL_INTERVAL_SECONDS=19",
                        "GW_PUBLISH_POLL_INTERVAL_SECONDS=12",
                        "GW_REQUEST_TIMEOUT_SECONDS=33",
                        "GW_GENERATION_TIMEOUT_SECONDS=999",
                        "GW_PUBLISH_TIMEOUT_SECONDS=888",
                        "GW_ARTICLES_DIR=/env/articles",
                    ]
                ),
                encoding="utf-8",
            )

            args = types.SimpleNamespace(
                env=[str(env_path)],
                set=["GW_POLL_INTERVAL_SECONDS=9"],
                base_url="https://from-cli.example.com",
                api_key="sk-gw-cli",
                poll_interval=None,
                image_poll_interval=22,
                publish_poll_interval=13,
                request_timeout=44,
                generation_timeout=None,
                publish_timeout=77,
                articles_dir="/cli/articles",
            )

            settings = MODULE.resolve_settings(args)

        self.assertEqual(settings["base_url"], "https://from-cli.example.com")
        self.assertEqual(settings["api_key"], "sk-gw-cli")
        self.assertEqual(settings["poll_interval"], 9)
        self.assertEqual(settings["image_poll_interval"], 22)
        self.assertEqual(settings["publish_poll_interval"], 13)
        self.assertEqual(settings["request_timeout"], 44)
        self.assertEqual(settings["generation_timeout"], 999)
        self.assertEqual(settings["publish_timeout"], 77)
        self.assertEqual(settings["articles_dir"], Path("/cli/articles"))

    def test_wait_for_document_uses_image_poll_interval(self) -> None:
        progress_payloads = [
            {
                "stage": 3,
                "stage_name": "Generating outline",
                "progress": 60,
                "status": "GENERATING",
                "completed": False,
            },
            {
                "stage": 5,
                "stage_name": "Generating images",
                "progress": 90,
                "status": "GENERATING",
                "completed": False,
            },
            {
                "stage": 5,
                "stage_name": "Completed",
                "progress": 100,
                "status": "DRAFT",
                "completed": True,
            },
        ]

        with mock.patch.object(MODULE, "get_document_progress", side_effect=progress_payloads):
            with mock.patch.object(MODULE, "get_document", return_value={"id": "doc-1"}):
                with mock.patch.object(MODULE.time, "sleep") as sleep:
                    progress, document = MODULE.wait_for_document(
                        base_url="https://example.com",
                        api_key="sk-gw-test",
                        document_id="doc-1",
                        poll_interval=10,
                        image_poll_interval=20,
                        request_timeout=10,
                        generation_timeout=60,
                    )

        self.assertEqual(progress["status"], "DRAFT")
        self.assertEqual(document["id"], "doc-1")
        self.assertEqual(sleep.call_args_list, [mock.call(10), mock.call(20)])

    def test_wait_for_publish_retries_when_record_not_ready(self) -> None:
        with mock.patch.object(
            MODULE,
            "get_publish_progress",
            side_effect=[
                MODULE.ApiError("Publish record not found."),
                {
                    "status": "completed",
                    "progress": 100,
                    "completed": True,
                    "published_url": "https://example.com/post",
                    "error_message": None,
                },
            ],
        ):
            with mock.patch.object(MODULE.time, "sleep") as sleep:
                progress = MODULE.wait_for_publish(
                    base_url="https://example.com",
                    api_key="sk-gw-test",
                    document_id="doc-1",
                    poll_interval=10,
                    request_timeout=30,
                    publish_timeout=60,
                )

        self.assertEqual(progress["status"], "completed")
        self.assertEqual(sleep.call_args_list, [mock.call(10)])

    def test_prepare_article_bundle_creates_manifest_with_expected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = types.SimpleNamespace(
                article_dir=str(Path(tmp_dir) / "bundle"),
                keyword="best hiking trails",
                article_key="article-key",
                idempotency_key="idem-key",
                language="en",
                country="united-states",
                need_image=True,
                download_images=True,
            )
            settings = {"articles_dir": Path(tmp_dir)}

            article_dir, manifest, paths = MODULE.prepare_article_bundle(args, settings)

            self.assertEqual(article_dir, Path(tmp_dir) / "bundle")
            self.assertEqual(manifest["article_key"], "article-key")
            self.assertEqual(manifest["idempotency_key"], "idem-key")
            self.assertEqual(manifest["files"]["markdown"], "article.md")
            self.assertTrue(paths["manifest"].exists())

    def test_cmd_generate_creates_complete_article_bundle(self) -> None:
        document = {
            "id": "doc-1",
            "title": "Best Hiking Trails",
            "body": "![Hero](https://cdn.example.com/hero.png)\n\nBody",
            "status": "DRAFT",
            "created_at": "2026-03-18T09:00:00+08:00",
            "updated_at": "2026-03-18T09:05:00+08:00",
        }
        progress = {
            "stage": 5,
            "stage_name": "Completed",
            "progress": 100,
            "status": "DRAFT",
            "completed": True,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            article_dir = Path(tmp_dir) / "bundle"
            args = types.SimpleNamespace(
                keyword="best hiking trails",
                article_dir=str(article_dir),
                article_key=None,
                idempotency_key=None,
                language="en",
                country="united-states",
                need_image=True,
                download_images=True,
                format="json",
                json_indent=2,
                env=[],
                set=[],
                base_url=None,
                api_key=None,
                poll_interval=None,
                image_poll_interval=None,
                publish_poll_interval=None,
                request_timeout=None,
                generation_timeout=None,
                publish_timeout=None,
                articles_dir=str(Path(tmp_dir) / "articles-root"),
            )

            settings = {
                "base_url": "https://example.com",
                "api_key": "sk-gw-test",
                "poll_interval": 10,
                "image_poll_interval": 20,
                "publish_poll_interval": 10,
                "request_timeout": 30,
                "generation_timeout": 900,
                "publish_timeout": 600,
                "articles_dir": Path(tmp_dir) / "articles-root",
            }

            stdout = io.StringIO()
            with mock.patch.object(MODULE, "resolve_settings", return_value=settings):
                with mock.patch.object(MODULE.uuid, "uuid4", return_value="generated-key"):
                    with mock.patch.object(MODULE, "create_document", return_value={"document_id": "doc-1"}):
                        with mock.patch.object(MODULE, "wait_for_document", return_value=(progress, document)):
                            with mock.patch.object(MODULE, "download_binary", return_value=(b"png", "image/png")):
                                with redirect_stdout(stdout):
                                    exit_code = MODULE.cmd_generate(args)

            self.assertEqual(exit_code, 0)

            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["document_id"], "doc-1")
            self.assertEqual(summary["idempotency_key"], "generated-key")

            markdown_path = article_dir / "article.md"
            manifest_path = article_dir / "manifest.json"
            document_path = article_dir / "document.json"
            generation_path = article_dir / "generation.json"
            image_path = article_dir / "images" / "image-1.png"

            self.assertTrue(markdown_path.exists())
            self.assertTrue(manifest_path.exists())
            self.assertTrue(document_path.exists())
            self.assertTrue(generation_path.exists())
            self.assertTrue(image_path.exists())

            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("images/image-1.png", markdown)
            self.assertNotIn("https://cdn.example.com/hero.png", markdown)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["document_id"], "doc-1")
            self.assertEqual(manifest["title"], "Best Hiking Trails")
            self.assertEqual(manifest["document_status"], "DRAFT")

            generation = json.loads(generation_path.read_text(encoding="utf-8"))
            self.assertEqual(generation["create_response"]["document_id"], "doc-1")
            self.assertEqual(generation["document"]["title"], "Best Hiking Trails")

    def test_cmd_generate_resumes_existing_bundle_without_recreating_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            article_dir = Path(tmp_dir) / "bundle"
            article_dir.mkdir(parents=True)
            manifest = {
                "article_key": "article-key",
                "idempotency_key": "idem-key",
                "keyword": "best hiking trails",
                "language": "en",
                "country": "united-states",
                "need_image": True,
                "download_images": False,
                "document_id": "doc-1",
                "publish": {"status": "not_started", "published_url": None, "error_message": None, "publish_config_id": None},
                "created_at": "2026-03-18T09:00:00Z",
                "updated_at": "2026-03-18T09:00:00Z",
                "files": {
                    "markdown": "article.md",
                    "document": "document.json",
                    "generation": "generation.json",
                    "publish": "publish.json",
                    "images_dir": "images",
                },
            }
            (article_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            args = types.SimpleNamespace(
                keyword=None,
                article_dir=str(article_dir),
                article_key=None,
                idempotency_key=None,
                language=None,
                country=None,
                need_image=None,
                download_images=None,
                format="path",
                json_indent=2,
                env=[],
                set=[],
                base_url=None,
                api_key=None,
                poll_interval=None,
                image_poll_interval=None,
                publish_poll_interval=None,
                request_timeout=None,
                generation_timeout=None,
                publish_timeout=None,
                articles_dir=str(Path(tmp_dir) / "articles-root"),
            )
            settings = {
                "base_url": "https://example.com",
                "api_key": "sk-gw-test",
                "poll_interval": 10,
                "image_poll_interval": 20,
                "publish_poll_interval": 10,
                "request_timeout": 30,
                "generation_timeout": 900,
                "publish_timeout": 600,
                "articles_dir": Path(tmp_dir) / "articles-root",
            }
            progress = {
                "stage": 5,
                "stage_name": "Completed",
                "progress": 100,
                "status": "DRAFT",
                "completed": True,
            }
            document = {
                "id": "doc-1",
                "title": "Best Hiking Trails",
                "body": "Body",
                "status": "DRAFT",
                "created_at": "2026-03-18T09:00:00+08:00",
                "updated_at": "2026-03-18T09:05:00+08:00",
            }

            stdout = io.StringIO()
            with mock.patch.object(MODULE, "resolve_settings", return_value=settings):
                with mock.patch.object(MODULE, "get_document_progress", return_value=progress):
                    with mock.patch.object(MODULE, "get_document", return_value=document):
                        with mock.patch.object(MODULE, "create_document") as create_document:
                            with redirect_stdout(stdout):
                                exit_code = MODULE.cmd_generate(args)

            self.assertEqual(exit_code, 0)
            create_document.assert_not_called()
            self.assertEqual(stdout.getvalue().strip(), str(article_dir / "article.md"))
            self.assertTrue((article_dir / "article.md").exists())

    def test_cmd_configs_returns_json_payload(self) -> None:
        args = types.SimpleNamespace(
            json_indent=2,
            env=[],
            set=[],
            base_url=None,
            api_key=None,
            poll_interval=None,
            image_poll_interval=None,
            publish_poll_interval=None,
            request_timeout=None,
            generation_timeout=None,
            publish_timeout=None,
            articles_dir=None,
        )
        settings = {
            "base_url": "https://example.com",
            "api_key": "sk-gw-test",
            "request_timeout": 30,
        }

        stdout = io.StringIO()
        with mock.patch.object(MODULE, "resolve_settings", return_value=settings):
            with mock.patch.object(MODULE, "list_publish_configs", return_value={"configs": [{"id": 7}]}):
                with redirect_stdout(stdout):
                    exit_code = MODULE.cmd_configs(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"configs": [{"id": 7}]})

    def test_cmd_taxonomy_returns_json_payload(self) -> None:
        args = types.SimpleNamespace(
            publish_config_id=7,
            json_indent=2,
            env=[],
            set=[],
            base_url=None,
            api_key=None,
            poll_interval=None,
            image_poll_interval=None,
            publish_poll_interval=None,
            request_timeout=None,
            generation_timeout=None,
            publish_timeout=None,
            articles_dir=None,
        )
        settings = {
            "base_url": "https://example.com",
            "api_key": "sk-gw-test",
            "request_timeout": 30,
        }

        stdout = io.StringIO()
        with mock.patch.object(MODULE, "resolve_settings", return_value=settings):
            with mock.patch.object(MODULE, "get_publish_taxonomy", return_value={"category_count": 3, "tag_count": 4}):
                with redirect_stdout(stdout):
                    exit_code = MODULE.cmd_taxonomy(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"category_count": 3, "tag_count": 4})

    def test_cmd_publish_submits_waits_and_updates_publish_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            article_dir = Path(tmp_dir) / "bundle"
            article_dir.mkdir(parents=True)
            manifest = {
                "article_key": "article-key",
                "idempotency_key": "idem-key",
                "keyword": "best hiking trails",
                "language": "en",
                "country": "united-states",
                "document_id": "doc-1",
                "publish": {"status": "not_started", "published_url": None, "error_message": None, "publish_config_id": None},
                "created_at": "2026-03-18T09:00:00Z",
                "updated_at": "2026-03-18T09:00:00Z",
                "files": {
                    "markdown": "article.md",
                    "document": "document.json",
                    "generation": "generation.json",
                    "publish": "publish.json",
                    "images_dir": "images",
                },
            }
            (article_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            args = types.SimpleNamespace(
                article_dir=str(article_dir),
                publish_config_id=7,
                status="draft",
                categories="1,2",
                tags="9,11",
                remove_featured_from_content=True,
                format="json",
                json_indent=2,
                env=[],
                set=[],
                base_url=None,
                api_key=None,
                poll_interval=None,
                image_poll_interval=None,
                publish_poll_interval=None,
                request_timeout=None,
                generation_timeout=None,
                publish_timeout=None,
                articles_dir=None,
            )
            settings = {
                "base_url": "https://example.com",
                "api_key": "sk-gw-test",
                "publish_poll_interval": 10,
                "request_timeout": 30,
                "publish_timeout": 600,
            }
            final_progress = {
                "status": "completed",
                "progress": 100,
                "completed": True,
                "published_url": "https://wp.example.com/post",
                "error_message": None,
            }

            stdout = io.StringIO()
            with mock.patch.object(MODULE, "resolve_settings", return_value=settings):
                with mock.patch.object(MODULE, "submit_publish_task", return_value={"document_id": "doc-1"}):
                    with mock.patch.object(MODULE, "wait_for_publish", return_value=final_progress):
                        with redirect_stdout(stdout):
                            exit_code = MODULE.cmd_publish(args)

            self.assertEqual(exit_code, 0)

            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["published_url"], "https://wp.example.com/post")

            publish_log = json.loads((article_dir / "publish.json").read_text(encoding="utf-8"))
            attempt = publish_log["attempts"][0]
            self.assertEqual(attempt["request"]["publish_config_id"], 7)
            self.assertEqual(attempt["request"]["options"]["categories"], [1, 2])
            self.assertEqual(attempt["request"]["options"]["tags"], [9, 11])
            self.assertTrue(attempt["request"]["options"]["remove_featured_from_content"])

            updated_manifest = json.loads((article_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(updated_manifest["publish"]["status"], "completed")
            self.assertEqual(updated_manifest["publish"]["published_url"], "https://wp.example.com/post")
            self.assertEqual(updated_manifest["publish"]["publish_config_id"], 7)

    def test_cmd_publish_returns_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            article_dir = Path(tmp_dir) / "bundle"
            article_dir.mkdir(parents=True)
            manifest = {
                "document_id": "doc-1",
                "publish": {"status": "not_started", "published_url": None, "error_message": None, "publish_config_id": None},
                "files": {
                    "markdown": "article.md",
                    "document": "document.json",
                    "generation": "generation.json",
                    "publish": "publish.json",
                    "images_dir": "images",
                },
            }
            (article_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            args = types.SimpleNamespace(
                article_dir=str(article_dir),
                publish_config_id=7,
                status=None,
                categories=None,
                tags=None,
                remove_featured_from_content=None,
                format="text",
                json_indent=2,
                env=[],
                set=[],
                base_url=None,
                api_key=None,
                poll_interval=None,
                image_poll_interval=None,
                publish_poll_interval=None,
                request_timeout=None,
                generation_timeout=None,
                publish_timeout=None,
                articles_dir=None,
            )
            settings = {
                "base_url": "https://example.com",
                "api_key": "sk-gw-test",
                "publish_poll_interval": 10,
                "request_timeout": 30,
                "publish_timeout": 600,
            }
            final_progress = {
                "status": "failed",
                "progress": 0,
                "completed": True,
                "published_url": None,
                "error_message": "bad request",
            }

            stdout = io.StringIO()
            with mock.patch.object(MODULE, "resolve_settings", return_value=settings):
                with mock.patch.object(MODULE, "submit_publish_task", return_value={"document_id": "doc-1"}):
                    with mock.patch.object(MODULE, "wait_for_publish", return_value=final_progress):
                        with redirect_stdout(stdout):
                            exit_code = MODULE.cmd_publish(args)

            self.assertEqual(exit_code, 1)

    def test_create_alias_uses_generate_handler(self) -> None:
        parser = MODULE.build_parser()
        args = parser.parse_args(["create", "best hiking trails"])

        self.assertIs(args.func, MODULE.cmd_generate)


if __name__ == "__main__":
    unittest.main()
