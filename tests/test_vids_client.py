import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.request import Request

from curl_cffi.requests.exceptions import RequestException

from src.services.vids.client import (
    DOCS_ORIGIN,
    DocumentImageSourceError,
    VidsClient,
    VidsProtocolError,
    VidsTransportError,
    audio_results,
    build_image_request,
    build_text_request,
    build_video_request,
    cookie_jar,
    cookie_rows,
    document_image_shapes,
    image_results,
    image_video_feature,
    original_image_url,
    parse_document_session,
    parse_flags,
    parse_model_chunk,
    protocol_error,
    sapisid_hash,
    video_feature,
    video_results,
)
from tests.test_vids_video_input import mp4


class VidsClientTests(unittest.TestCase):
    def test_protocol_error_keeps_rpc_enums_and_discards_raw_message_metadata(self):
        response = SimpleNamespace(
            status_code=400,
            content=b"{}",
            json=lambda: {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "private https://upstream.example token=secret",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "REQUEST_REFUSED",
                            "metadata": {"secret": "private"},
                        }
                    ],
                }
            },
        )
        error = protocol_error("generate", response)
        self.assertEqual(error.upstream_status, "INVALID_ARGUMENT")
        self.assertEqual(error.upstream_reason, "REQUEST_REFUSED")
        self.assertEqual(str(error), "Vids generate: HTTP 400")
        for reason in ("https://private.example", "private-cookie", "A" * 65, [], None):
            response.json = lambda: {
                "error": {
                    "status": "private-cookie",
                    "details": [
                        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}
                    ],
                }
            }
            error = protocol_error("generate", response)
            self.assertIsNone(error.upstream_status)
            self.assertIsNone(error.upstream_reason)

    def test_protojson_rpc_error_preserves_language_reason_without_private_message(self):
        body = [
            3,
            "private message https://private.example",
            [
                [
                    "type.googleapis.com/google.rpc.ErrorInfo",
                    ["LANGUAGE_NOT_SUPPORTED", "espresso-pa.googleapis.com", {"key": "private"}],
                ]
            ],
        ]
        response = SimpleNamespace(status_code=400, content=b"[]", json=lambda: body)
        error = protocol_error("generate", response)
        self.assertEqual(error.upstream_status, "INVALID_ARGUMENT")
        self.assertEqual(error.upstream_reason, "LANGUAGE_NOT_SUPPORTED")
        self.assertNotIn("private", str(error))
        body[2][0][1][0] = "private-cookie"
        self.assertIsNone(protocol_error("generate", response).upstream_reason)

    def test_malformed_error_json_preserves_known_http_failure(self):
        for content in (b"not json", b"[" * 2000 + b"0" + b"]" * 2000):
            with self.subTest(content_bytes=len(content)):
                response = SimpleNamespace(
                    status_code=400, content=content, json=lambda: json.loads(content)
                )
                error = protocol_error("generate", response)
                self.assertEqual(error.status, 400)
                self.assertEqual(error.operation, "generate")
                self.assertIsNone(error.upstream_status)
                self.assertIsNone(error.upstream_reason)

    def test_document_session_parser_checks_revision_and_rejects_executable_literals(self):
        for literal in ("'DOC_SESSION'", '"DOC_SESSION"'):
            self.assertEqual(
                parse_document_session(
                    f"DOCS_warmStartDocumentLoader.startLoad(3.0, {literal}, 0, 0);", 3
                ),
                "DOC_SESSION",
            )
        for html in (
            "DOCS_warmStartDocumentLoader.startLoad(2.0,'private-value');",
            "DOCS_warmStartDocumentLoader.startLoad(3.0, getSession());",
            "DOCS_warmStartDocumentLoader.startLoad(3.0, 'bad\\'value');",
            "DOCS_warmStartDocumentLoader.startLoad(3.0, 'DOC_SESSION'+getSession());",
            "DOCS_warmStartDocumentLoader.startLoad(3.0,'a');"
            "DOCS_warmStartDocumentLoader.startLoad(3.0,'b');",
            "no loader",
        ):
            with self.assertRaises(ValueError) as caught:
                parse_document_session(html, 3)
            self.assertNotIn("private-value", str(caught.exception))

    def test_document_image_edit_refreshes_saved_source_and_matches_full_context(self):
        expected = json.loads(
            (Path(__file__).parent / "fixtures" / "vids-image-edit-context.json").read_text(
                encoding="utf-8"
            )
        )["request"]
        document = expected[2][8][3][0][0][7][2][0]
        shape_id = json.loads(document[3])[5][1][0]
        with patch("src.services.vids.client.AsyncSession"):
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.session_id = expected[2][4]
            client._request = AsyncMock(return_value=["result"])
            client.settings = AsyncMock()
            client.document_image_url = AsyncMock(
                return_value="https://docs.google.com/videos-images-rt/synthetic"
            )

            async def refresh():
                client.flags = {"docs-ci": document[0]}
                client.document_session_id = document[5]
                client.model_chunk = {
                    "revision": document[2],
                    "chunk": [
                        [
                            3,
                            shape_id,
                            3,
                            [1, 0, 0, 1, 0, 0],
                            [49, expected[3][11][2]],
                            expected[2][5][0][3],
                        ]
                    ],
                }

            client.bootstrap = AsyncMock(side_effect=refresh)
            result = asyncio.run(client.edit_document_image(shape_id, expected[3][2]))
            self.assertEqual(result, ["result"])
            self.assertEqual(client._request.call_args.args[2], expected)
            client.bootstrap.assert_awaited_once()
            client.document_image_url.assert_awaited_once_with(expected[3][11][2])
            client.document_image_url.side_effect = ValueError("private-placeholder")
            with self.assertRaises(DocumentImageSourceError) as caught:
                asyncio.run(client.edit_document_image(shape_id, expected[3][2]))
            self.assertNotIn("private-placeholder", str(caught.exception))
            self.assertEqual(client._request.await_count, 1)
            with self.assertRaises(DocumentImageSourceError):
                asyncio.run(client.edit_document_image("missing", expected[3][2]))
            self.assertEqual(client._request.await_count, 1)

            async def rotate_document():
                client.model_chunk = {"chunk": [], "revision": 4}
                client.document_session_id = "NEW_LOAD_SESSION"

            client.settings = AsyncMock(side_effect=rotate_document)
            with self.assertRaises(DocumentImageSourceError):
                asyncio.run(client.edit_document_image(shape_id, expected[3][2]))
            self.assertEqual(client._request.await_count, 1)

    def test_document_initializer_preserves_quoted_brackets_and_revision(self):
        model = {
            "chunk": [[3, 'shape"[}]', 3, [1, 0, 0, 1, 0, 0], [49, 'image"[}]'], "scene"]],
            "revision": 2,
        }
        html = (
            "<script>var modelChunk = "
            + json.dumps(model)
            + ";loadModelChunk(modelChunk);</script>"
        )
        self.assertEqual(parse_model_chunk(html), model)
        with self.assertRaises(ValueError):
            parse_model_chunk(html + html)
        with self.assertRaises(ValueError):
            parse_model_chunk(
                html + "DOCS_warmStartDocumentLoader.loadModelChunk({});"
                "DOCS_warmStartDocumentLoader.loadModelChunk({});"
            )
        self.assertEqual(
            document_image_shapes(parse_model_chunk(html)),
            [{"shape_id": 'shape"[}]', "cosmo_id": 'image"[}]', "scene_id": "scene"}],
        )

    def test_document_initializer_rejects_missing_malformed_and_invalid_schema(self):
        for html in (
            "loadModelChunk(modelChunk);",
            'var modelChunk={"chunk":["private-value"],"revision":2',
            'var modelChunk={"chunk":{},"revision":2};',
            'var modelChunk={"chunk":[],"revision":true};',
            'var modelChunk={"chunk":[],"revision":-1};',
            'var modelChunk={"chunk":[["private-value"]],"revision":2};',
            'var modelChunk={"chunk":[[]],"revision":2};',
        ):
            with self.subTest(html=html), self.assertRaises(ValueError) as caught:
                parse_model_chunk(html)
            self.assertNotIn("private-value", str(caught.exception))

    def test_document_image_shapes_only_reads_typed_top_level_inserts_and_pairs(self):
        image = [3, "shape", 3, [1, 0, 0, 1, 0, 0], [8, 1024, 9, 1024, 49, "cosmo"], "scene"]
        model = {
            "chunk": [
                [3, "non-image", 108, [1, 0, 0, 1, 0, 0], [49, "unrelated"], "scene"],
                [3, "no-resource", 3, [1, 0, 0, 1, 0, 0], [8, 1024], "scene"],
                [9, [image], [49, "unrelated"]],
                image,
            ],
            "revision": 2,
        }
        before = json.dumps(model)
        self.assertEqual(
            document_image_shapes(model),
            [{"shape_id": "shape", "cosmo_id": "cosmo", "scene_id": "scene"}],
        )
        self.assertEqual(json.dumps(model), before)
        for mutation in (
            [0, ["shape"]],
            [5, ["shape"], [], [49, "replacement"]],
            [59, "shape"],
        ):
            with self.assertRaises(ValueError):
                document_image_shapes({"chunk": [image, mutation], "revision": 3})
        for properties in ([49], ["49", "private-value"], [49, []], [49, ""]):
            malformed = [3, "shape", 3, [1, 0, 0, 1, 0, 0], properties, "scene"]
            with self.subTest(properties=properties), self.assertRaises(ValueError) as caught:
                document_image_shapes({"chunk": [malformed]})
            self.assertNotIn("private-value", str(caught.exception))

    def test_deleted_document_image_cannot_reach_generation(self):
        with patch("src.services.vids.client.AsyncSession"):
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
        client.model_chunk = {
            "revision": 4,
            "chunk": [
                [3, "deleted-shape", 3, [1, 0, 0, 1, 0, 0], [49, "private-cosmo"], "scene"],
                [0, ["deleted-shape"]],
            ],
        }
        client.document_session_id = "LOAD_CONTEXT"
        client.bootstrap = AsyncMock()
        client.settings = AsyncMock()
        client.document_image_url = AsyncMock()
        client._request = AsyncMock()
        with self.assertRaises(ValueError):
            asyncio.run(client.edit_document_image("deleted-shape", "change color"))
        client.document_image_url.assert_not_awaited()
        client._request.assert_not_awaited()

    def test_bootstrap_retains_optional_document_chunk_and_clears_stale_state(self):
        flags = {
            "docs-gaopd": "appsgenaiserver-pa.clients6.google.com",
            "espresso_api_key": "key",
            "docs-est": "token",
        }
        prefix = "_docs_flag_initialData=" + json.dumps(flags) + ";"
        model = {"chunk": [[1, [100, 100], [100, 100]]], "revision": 2}
        with patch("src.services.vids.client.AsyncSession") as session:
            response = SimpleNamespace(
                status_code=200, text=prefix + "var modelChunk=" + json.dumps(model) + ";"
            )
            session.return_value.get = AsyncMock(return_value=response)
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            asyncio.run(client.bootstrap())
            self.assertEqual(client.model_chunk, model)
            response.text += "DOCS_warmStartDocumentLoader.startLoad(2.0,'LOAD_SESSION',0,0);"
            asyncio.run(client.bootstrap())
            self.assertEqual(client.document_session_id, "LOAD_SESSION")
            response.text = prefix
            asyncio.run(client.bootstrap())
            self.assertIsNone(client.model_chunk)
            self.assertIsNone(client.document_session_id)
            response.text = prefix + "var modelChunk=" + json.dumps(model) + ";"
            asyncio.run(client.bootstrap())
            response.text = prefix + 'var modelChunk={"chunk":"private-value"};'
            with self.assertRaises(ValueError) as caught:
                asyncio.run(client.bootstrap())
            self.assertNotIn("private-value", str(caught.exception))
            self.assertIsNone(client.model_chunk)
            self.assertFalse(client.initialization_valid)

    def test_bootstrap_checkpoint_observes_validated_new_identity(self):
        with patch("src.services.vids.client.AsyncSession") as session:
            session.return_value.get = AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    text='_docs_flag_initialData={"docs-gaopd":"appsgenaiserver-pa.clients6.google.com",'
                    '"espresso_api_key":"key","docs-est":"token","docs-hue":"new-account"};',
                )
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"docs-hue": "old-account"}
            identities = []
            client.on_cookies_changed = lambda: identities.append(client.flags["docs-hue"])
            asyncio.run(client.bootstrap())
            self.assertEqual(identities, ["new-account"])
            self.assertTrue(client.initialization_valid)
            session.return_value.get.return_value.text = "uninitialized"
            with self.assertRaises(ValueError):
                asyncio.run(client.bootstrap())
            self.assertEqual(identities, ["new-account"])
            self.assertFalse(client.initialization_valid)

    def test_cookie_snapshot_keeps_scope_and_http_rotations(self):
        jar = cookie_jar(
            [
                {
                    "name": "SAPISID",
                    "value": "initial",
                    "domain": ".google.com",
                    "secure": True,
                    "httpOnly": True,
                    "expires": 4102444800,
                },
                {
                    "name": "SAPISID",
                    "value": "host",
                    "domain": "docs.google.com",
                    "path": "/videos",
                    "secure": True,
                },
                {"name": "expired", "value": "old", "domain": ".google.com", "expires": 1},
            ]
        )
        signed = next(c for c in jar if c.domain == ".google.com")
        signed.value = "rotated"
        signed._rest = {"http_only": "True"}
        rows = cookie_rows(jar)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["value"], "rotated")
        self.assertTrue(rows[0]["httpOnly"])
        self.assertFalse(rows[0]["hostOnly"])
        self.assertEqual(rows[1]["value"], "host")
        self.assertTrue(rows[1]["hostOnly"])
        self.assertTrue(rows[1]["session"])
        self.assertEqual(cookie_rows(cookie_jar(rows)), rows)

    def test_every_http_response_checkpoints_cookies_and_paid_guard_precedes_post(self):
        with patch("src.services.vids.client.AsyncSession") as session:
            session.return_value.cookies.jar = cookie_jar(
                [{"name": "SAPISID", "value": "test", "domain": ".google.com"}]
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"espresso_api_key": "key", "docs-est": "token"}
            client.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
            session.return_value.request = AsyncMock(
                return_value=SimpleNamespace(status_code=200, json=lambda: ["received"])
            )
            calls = []
            client.on_cookies_changed = lambda: calls.append("checkpoint")
            self.assertEqual(asyncio.run(client.quota()), ["received"])
            self.assertEqual(calls, ["checkpoint"])
            client.before_generate = lambda: (_ for _ in ()).throw(
                ValueError("storage unavailable")
            )
            with self.assertRaises(ValueError):
                asyncio.run(client._request("POST", "/v1/genai/generate", []))
            self.assertEqual(session.return_value.request.await_count, 1)
            client.before_generate = None
            self.assertEqual(
                asyncio.run(client._request("POST", "/v1/genai/generate", [])), ["received"]
            )
            self.assertEqual(calls, ["checkpoint", "checkpoint"])

    def test_video_edit_combines_source_video_and_uploaded_image_references(self):
        with patch("src.services.vids.client.AsyncSession"):
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"flix-evg1p2ui": 1}
            client.settings = AsyncMock()
            client._request = AsyncMock(return_value=[])
            source = {"corpus": "co", "lookup_schema": "sc", "unique_key": "uk", "duration": 3}
            asyncio.run(
                client.submit_video(
                    source, "Apply the reference", mode="edit", identifiers=["blob"]
                )
            )
            body = client._request.call_args.args[2]
            self.assertEqual(body[0], 377)
            refs = body[2][25]
            self.assertEqual([r[0][7][0] for r in refs], ["source_video", "image_1"])
            self.assertEqual(refs[1][0][7][1][11][8], [None, None, None, "blob", 1])
            with self.assertRaises(ValueError):
                asyncio.run(
                    client.submit_video(source, "Continue", mode="extend", identifiers=["blob"])
                )

    def test_image_edit_preserves_resource_reference_and_does_not_fake_cosmo_id(self):
        with patch("src.services.vids.client.AsyncSession"):
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"flix-evg1p2ui": 1}
            client.settings = AsyncMock()
            client._request = AsyncMock(return_value=["result"])
            reference = [None] * 6 + [
                ["key", "https://lh3.googleusercontent.com/image", 1024, 1024]
            ]
            asyncio.run(client.edit_image({"reference": reference}, "Change the color"))
            body = client._request.call_args.args[2]
            self.assertEqual(body[0], 205)
            self.assertEqual(body[3][2], "Change the color")
            self.assertIs(body[3][11], reference)
            self.assertIsNone(body[3][11][2])
            self.assertEqual(body[4], [[0, 4, 0]])
            asyncio.run(client.edit_image({"cosmo_id": "uploaded-cosmo"}, "Change the color"))
            self.assertEqual(
                client._request.call_args.args[2][3][11], [None, None, "uploaded-cosmo"]
            )

    def test_image_edit_matches_official_builder_fixture(self):
        expected = json.loads(
            (Path(__file__).parent / "fixtures" / "vids-image-edit-builder.json").read_text(
                encoding="utf-8"
            )
        )
        with patch("src.services.vids.client.AsyncSession"):
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"flix-evg1p2ui": 1}
            client.session_id = expected[2][4]
            client.settings = AsyncMock()
            client._request = AsyncMock(return_value=[])
            asyncio.run(client.edit_image({"cosmo_id": expected[3][11][2]}, expected[3][2]))
            actual = client._request.call_args.args[2]
            # The isolated official builder omits document resources and request UUID.
            for index in (8, 23, 31):
                actual[2][index] = None
            self.assertEqual(actual, expected)

    def test_document_associated_upload_returns_cosmo_without_scene_mutation(self):
        with (
            patch("src.services.vids.client.AsyncSession") as session,
            tempfile.TemporaryDirectory() as directory,
        ):
            from pathlib import Path

            path = Path(directory) / "image.jpg"
            path.write_bytes(b"\xff\xd8\xffsynthetic")
            response = {
                "sessionStatus": {
                    "additionalInfo": {
                        "uploader_service.GoogleRupioAdditionalInfo": {
                            "completionInfo": {
                                "status": "SUCCESS",
                                "customerSpecificInfo": {"id": "cosmo-id"},
                            }
                        }
                    }
                }
            }
            session.return_value.post = AsyncMock(
                return_value=SimpleNamespace(status_code=200, json=lambda: response)
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {
                "scotty_upload_url": "/upload/videos/resumable",
                "info_params": {"token": "synthetic", "ouid": "synthetic"},
            }
            self.assertEqual(
                asyncio.run(client.upload_image(path, mime_type="image/jpeg")), "cosmo-id"
            )
            args, options = session.return_value.post.call_args
            self.assertEqual(args[0], "https://docs.google.com/upload/videos/resumable")
            self.assertIn(b'"containingCosmoId"', options["data"])
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(session.return_value.post.await_count, 1)

    def test_document_image_lookup_uses_scoped_docs_query_and_cookie_checkpoint(self):
        with patch("src.services.vids.client.AsyncSession") as session:
            url = "https://docs.google.com/videos-images-rt/SYNTHETIC_SIGNATURE=s2048"
            session.return_value.post = AsyncMock(
                return_value=SimpleNamespace(status_code=200, text=json.dumps({"r0": url}))
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [], auth_user=2)
            client.flags = {
                "info_params": {"token": "synthetic", "ouid": "owner", "extra": "scoped"}
            }
            checkpoints = []
            client.on_cookies_changed = lambda: checkpoints.append(True)
            self.assertEqual(asyncio.run(client.document_image_url("cosmo")), url)
            args, options = session.return_value.post.call_args
            self.assertEqual(args[0], "https://docs.google.com/videos/renderdata")
            self.assertEqual(
                options["params"],
                {
                    "token": "synthetic",
                    "ouid": "owner",
                    "extra": "scoped",
                    "id": "doc",
                    "authuser": 2,
                },
            )
            self.assertEqual(
                json.loads(options["data"]["renderOps"]), {"r0": ["image", {"cosmoId": "cosmo"}]}
            )
            self.assertEqual(
                options["headers"], {"Origin": DOCS_ORIGIN, "Referer": client.editor_url}
            )
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(checkpoints, [True])
            self.assertEqual(
                client.flags["info_params"],
                {"token": "synthetic", "ouid": "owner", "extra": "scoped"},
            )
            session.return_value.post.return_value.text = ")]}'\n" + json.dumps({"r0": url})
            self.assertEqual(asyncio.run(client.document_image_url("cosmo")), url)

    def test_document_image_lookup_rejects_untrusted_urls_and_suppresses_private_errors(self):
        with patch("src.services.vids.client.AsyncSession") as session:
            response = SimpleNamespace(status_code=200, text="")
            session.return_value.post = AsyncMock(return_value=response)
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"info_params": {"token": "synthetic", "ouid": "owner"}}
            for value in (
                "private-value",
                {},
                {"r0": None},
                {"r0": "https://attacker.example/private-value"},
                {"r0": "https://docs.google.com/other/private-value"},
                {"r0": "https://docs.google.com/videos-images-rt/../private-value"},
                {"r0": "https://docs.google.com/videos-images-rt/%2e%2e%2fprivate-value"},
                {"r0": "https://docs.google.com:private-value/videos-images-rt/image"},
                {"r0": "https://private-value@docs.google.com/videos-images-rt/image"},
            ):
                response.text = value if isinstance(value, str) else json.dumps(value)
                with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                    asyncio.run(client.document_image_url("cosmo"))
                self.assertNotIn("private-value", str(caught.exception))
            session.return_value.post.reset_mock()
            for identifier in (None, "", "bad id", "bad\0id", "x" * 4097):
                with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                    asyncio.run(client.document_image_url(identifier))
            session.return_value.post.assert_not_awaited()
            session.return_value.post.side_effect = RequestException("https://private-value")
            with self.assertRaises(VidsTransportError) as caught:
                asyncio.run(client.document_image_url("cosmo"))
            self.assertNotIn("private-value", str(caught.exception))
            session.return_value.post.side_effect = None
            response.status_code = 403
            with self.assertRaises(VidsProtocolError) as caught:
                asyncio.run(client.document_image_url("cosmo"))
            self.assertEqual(str(caught.exception), "Vids document-image: HTTP 403")

    def test_document_image_download_allows_only_precise_docs_image_paths(self):
        with (
            patch("src.services.vids.client.AsyncSession") as session,
            tempfile.TemporaryDirectory() as directory,
        ):
            session.return_value.get = AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200, headers={"Content-Type": "image/jpeg"}, content=b"image"
                )
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            for suffix in ("rt", "qw", "xx"):
                url = f"https://docs.google.com/videos-images-{suffix}/SYNTHETIC_SIGNATURE=s2048"
                output = asyncio.run(
                    client.download_image(
                        {"url": url, "width": 1024, "height": 1024}, directory + "/image.jpg"
                    )
                )
                self.assertEqual(output.read_bytes(), b"image")
            self.assertEqual(session.return_value.get.await_count, 3)
            self.assertEqual(
                session.return_value.get.call_args.kwargs["headers"], {"Referer": DOCS_ORIGIN + "/"}
            )
            for url in (
                "https://docs.google.com/videos-images-rt/../other",
                "https://docs.google.com/videos-images-rt/%2e%2e%2fother",
                "https://docs.google.com/other/image",
                "http://docs.google.com/videos-images-rt/image",
                "https://docs.google.com:private-value/videos-images-rt/image",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                    asyncio.run(client._download_other(url, directory + "/image.jpg", "image/"))
                self.assertNotIn("private-value", str(caught.exception))
            self.assertEqual(session.return_value.get.await_count, 3)
            session.return_value.get.return_value = SimpleNamespace(
                status_code=302, headers={"Location": "https://docs.google.com/other/image"}
            )
            with self.assertRaises(ValueError):
                asyncio.run(
                    client._download_other(
                        "https://docs.google.com/videos-images-rt/image",
                        directory + "/image.jpg",
                        "image/",
                    )
                )
            self.assertEqual(session.return_value.get.await_count, 4)

    def test_image_audio_parsers_and_original_size_preserve_query(self):
        image = [
            "key",
            "https://lh3.googleusercontent.com/image=w512-h512?token=synthetic",
            1024,
            1024,
        ]
        image_node = [None] * 11 + [[None] * 6 + [image]]
        self.assertEqual(image_results([image_node])[0]["width"], 1024)
        self.assertEqual(
            original_image_url(image[1], 1024, 1024),
            "https://lh3.googleusercontent.com/image=w1024-h1024?token=synthetic",
        )
        audio = [None, "audio/wav", None, "https://contribution-rt.usercontent.google.com/audio"]
        audio_node = [None] * 29 + [[audio]]
        self.assertEqual(audio_results([audio_node])[0]["mime_type"], "audio/wav")

    def test_image_redirects_validate_each_host_before_sending_cookies(self):
        with (
            patch("src.services.vids.client.AsyncSession") as session,
            tempfile.TemporaryDirectory() as directory,
        ):
            session.return_value.get = AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        status_code=302, headers={"Location": "https://attacker.example/image"}
                    ),
                ]
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            with self.assertRaises(ValueError):
                asyncio.run(
                    client.download_image(
                        {
                            "url": "https://lh3.googleusercontent.com/image",
                            "width": 1024,
                            "height": 1024,
                        },
                        directory + "/image.jpg",
                    )
                )
            self.assertEqual(session.return_value.get.await_count, 1)

    def test_video_and_image_modes_reuse_resources_with_correct_fields(self):
        flags = {"flix-evga": 1, "flix-evg1p2ui": 1}
        video = {"corpus": "co", "lookup_schema": "sc", "unique_key": "uk", "duration": 3}
        edited = build_video_request("doc", flags, "Make it blue", video, mode="edit")
        self.assertEqual(edited[0], 377)
        self.assertEqual(edited[4][15][6:9], [[], ["3"], 3])
        self.assertEqual(edited[2][25][0][0][7][0], "source_video")
        extended = build_video_request("doc", flags, "Continue", video, mode="extend")
        self.assertEqual(extended[0], 378)
        self.assertEqual(extended[4][15][6:8], [None, None])
        animated = build_image_request(
            build_text_request("doc", flags, "move"), "move", ["blob"], mode="animate"
        )
        self.assertEqual(animated[0], 375)
        self.assertEqual(animated[2][25][0][0][7][1][11][8], [None, None, None, "blob", 1])
        with self.assertRaises(ValueError):
            build_image_request(animated, "move", ["one", "two"], mode="animate")
        for flag, pair in (
            ("flix-evgvl", (258, 257)),
            ("flix-eofpsq", (387, 388)),
            ("flix-eof", (368, 369)),
        ):
            self.assertEqual(image_video_feature({flag: 1}, "animate"), pair[0])
            self.assertEqual(image_video_feature({flag: 1}, "ingredients"), pair[1])
        self.assertEqual(image_video_feature(flags, "ingredients"), 376)

    def test_temporary_upload_validates_destination_and_finalizes_once(self):
        with (
            patch("src.services.vids.client.AsyncSession") as session,
            tempfile.TemporaryDirectory() as directory,
        ):
            from pathlib import Path

            source = Path(directory) / "input.png"
            source.write_bytes(b"synthetic-image")
            session.return_value.post = AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    headers={"X-Goog-Upload-URL": "https://docs.google.com/upload?id=synthetic"},
                )
            )
            session.return_value.put = AsyncMock(
                return_value=SimpleNamespace(status_code=200, json=lambda: ["blob-id"])
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"docs-tbue": 1, "docs-tbuu": "/upload/temporaryblob/videos"}
            self.assertEqual(asyncio.run(client.upload_temporary(source)), "blob-id")
            self.assertEqual(session.return_value.put.await_count, 1)
            self.assertEqual(
                session.return_value.put.call_args.kwargs["headers"]["X-Goog-Upload-Command"],
                "upload, finalize",
            )
            session.return_value.post.return_value.headers["X-Goog-Upload-URL"] = (
                "https://attacker.example/upload"
            )
            with self.assertRaises(ValueError):
                asyncio.run(client.upload_temporary(source))
            self.assertEqual(session.return_value.put.await_count, 1)

    def test_cookie_scope_and_xd3_signature_are_separate(self):
        rows = [
            {"name": "SAPISID", "value": "synthetic", "domain": ".google.com", "secure": True},
            {"name": "OSID", "value": "docs-only", "domain": "docs.google.com", "secure": True},
            {"name": "SAPISID", "value": "foreign", "domain": "attacker.example"},
            {"name": "SAPISID", "value": "wrong-host", "domain": "google.com"},
        ]
        jar = cookie_jar(rows)
        target = Request("https://appsgenaiserver-pa.clients6.google.com/v1/genai/quotaSummary")
        jar.add_cookie_header(target)
        self.assertEqual(target.get_header("Cookie"), "SAPISID=synthetic")
        with patch("src.services.vids.client.AsyncSession") as session:
            session.return_value.cookies.jar = jar
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", rows)
            client.flags = {"docs-est": "synthetic-server-token"}
            client.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
            with patch("src.services.vids.client.time.time", return_value=1700000000):
                headers = client._headers()
        self.assertEqual(headers["X-Origin"], DOCS_ORIGIN)
        self.assertEqual(headers["Referer"], client.service_origin + "/")
        self.assertEqual(headers["Origin"], client.service_origin)
        self.assertEqual(
            headers["Authorization"],
            "SAPISIDHASH 1700000000_5895de99b216f2279b2e24e0e668a832a1759f16",
        )

    def test_untrusted_service_and_editor_are_rejected(self):
        with patch("src.services.vids.client.AsyncSession") as session:
            response = SimpleNamespace(
                status_code=200,
                text='_docs_flag_initialData={"docs-gaopd":"attacker.example"};',
            )

            async def fake_get(*args, **kwargs):
                return response

            session.return_value.get = fake_get
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            with self.assertRaises(ValueError):
                asyncio.run(client.bootstrap())
            with self.assertRaises(ValueError):
                VidsClient("https://docs.google.com.attacker.example/videos/d/doc/edit", [])

    def test_paid_submission_does_not_retry_unknown_acceptance(self):
        with patch("src.services.vids.client.AsyncSession") as session:
            calls = []

            async def fail_request(*args, **kwargs):
                calls.append((args, kwargs))
                raise TimeoutError("possibly accepted")

            session.return_value.request = fail_request
            session.return_value.cookies.jar = cookie_jar(
                [{"name": "SAPISID", "value": "test", "domain": ".google.com"}]
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {
                "espresso_api_key": "test-key",
                "docs-est": "test-token",
                "flix-evga": 1,
                "flix-evg1p2ui": 1,
            }
            client.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
            client.settings_response = [None, [0]]
            with self.assertRaises(VidsTransportError) as caught:
                asyncio.run(client.submit_text("A landscape"))
            self.assertNotIn("possibly accepted", str(caught.exception))
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][0][1].endswith("/v1/genai/generate"))

    def test_only_read_queries_refresh_http_configuration_after_401(self):
        with patch("src.services.vids.client.AsyncSession") as session:
            session.return_value.cookies.jar = cookie_jar(
                [{"name": "SAPISID", "value": "test", "domain": ".google.com"}]
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {"espresso_api_key": "key", "docs-est": "token"}
            client.service_origin = "https://appsgenaiserver-pa.clients6.google.com"
            client.bootstrap = AsyncMock()
            session.return_value.request = AsyncMock(
                side_effect=[
                    SimpleNamespace(status_code=401),
                    SimpleNamespace(status_code=200, json=lambda: ["ok"]),
                ]
            )
            self.assertEqual(asyncio.run(client.quota()), ["ok"])
            self.assertEqual(client.bootstrap.await_count, 1)
            session.return_value.request = AsyncMock(return_value=SimpleNamespace(status_code=401))
            with self.assertRaises(VidsProtocolError):
                asyncio.run(client._request("POST", "/v1/genai/generate", []))
            self.assertEqual(client.bootstrap.await_count, 1)
            self.assertEqual(session.return_value.request.await_count, 1)

    def test_static_request_and_protocol_error_do_not_claim_verified_generation(self):
        flags = parse_flags(
            'before _docs_flag_initialData = {"flix-evga":1,"flix-evg1p2ui":1,'
            '"docs-api-keys":{"espresso_api_key":"nested-key"}};'
            'keys={"espresso_api_key":"synthetic-key"};'
        )
        self.assertEqual(flags["espresso_api_key"], "nested-key")
        self.assertEqual(video_feature({"flix-eofpsq": "false", "flix-eof": "0"}), 104)
        request = build_text_request("doc", flags, "A landscape", session_id="s", request_id="r")
        self.assertEqual(video_feature(flags), 374)
        self.assertEqual(request[2][0], 9)
        self.assertEqual(request[4][15], [0, 12, None, 0, 1, None, None, None, 3])
        portrait = build_text_request("doc", flags, "A boat", seconds=4, orientation="portrait")
        self.assertEqual(portrait[4][15], [0, 12, None, 0, 2, None, None, None, 4])
        self.assertIsNone(request[2][7])
        self.assertEqual(request[2][4:7], ["s", None, "r"])
        request = build_text_request(
            "doc",
            {**flags, "flix-evge": 1},
            "A landscape",
            conversation_id="a" * 32,
            conversation_time_ms=1700000000123,
        )
        self.assertEqual(request[5], [1, None, [[None, "1", 1189]]])
        self.assertEqual(request[2][28:31], ["a" * 32, ["1700000000", 123000000], 0])
        self.assertEqual(json.loads(json.dumps(request))[3], [None, None, "A landscape"])
        self.assertEqual(str(VidsProtocolError("generate", 401)), "Vids generate: HTTP 401")
        with self.assertRaises(ValueError):
            build_text_request("doc", flags, "A landscape", seconds=True)
        self.assertTrue(sapisid_hash("synthetic", 1700000000).startswith("SAPISIDHASH "))

    def test_video_resource_parsing_deduplicates_and_ignores_unrelated_urls(self):
        clip = [
            "corpus",
            None,
            "schema",
            "key",
            "https://contribution-rt.usercontent.google.com/v",
            1280,
            720,
            ["3", 8000000],
        ]
        node = [None] * 25 + [[None, None, clip]]
        data = ["https://unrelated.example/", node, [node]]
        videos = video_results(data)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["duration"], 3.008)
        self.assertEqual(videos[0]["unique_key"], "key")
        clip[7] = ["unexpected-private-value"]
        with self.assertRaises(ValueError) as caught:
            video_results(data)
        self.assertNotIn("unexpected-private-value", str(caught.exception))

    def test_media_download_uses_scoped_session_without_generation_credentials(self):
        with (
            patch("src.services.vids.client.AsyncSession") as session,
            tempfile.TemporaryDirectory() as directory,
        ):
            session.return_value.get = AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    content=mp4(3),
                )
            )
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            video = {"url": "https://contribution-rt.usercontent.google.com/v"}
            output = asyncio.run(client.download_video(video, directory + "/video.mp4"))
            self.assertTrue(output.exists())
            options = session.return_value.get.call_args.kwargs
            self.assertFalse(options["allow_redirects"])
            self.assertNotIn("Authorization", options["headers"])
            self.assertNotIn("X-Server-Token", options["headers"])
            with self.assertRaises(ValueError):
                asyncio.run(client.download_video({"url": "https://attacker.example/v"}, output))


if __name__ == "__main__":
    unittest.main()
