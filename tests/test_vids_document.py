import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from curl_cffi.requests.exceptions import RequestException

from src.services.vids.client import (
    DOCS_ORIGIN,
    DocumentImageSourceError,
    VidsClient,
    VidsProtocolError,
    VidsTransportError,
)
from src.services.vids.document import (
    document_scenes,
    image_insert_commands,
    parse_save_config,
    saved_revision,
)


def document():
    return {
        "revision": 3,
        "chunk": [
            [1, [365760, 205740], [302400, 427680]],
            [12, "scene", 0, 0, [16, 5000]],
            [3, "old-shape", 3, [1, 0, 0, 1, 0, 0], [49, "old-cosmo"], "scene"],
        ],
    }


class VidsDocumentTests(unittest.TestCase):
    def test_save_configuration_uses_document_prefix_and_separate_save_sid(self):
        html = (
            "xhrPrefix: '/videos/d/doc', urlPrefix: '/videos/d/doc', sid: 'SAVE_SESSION';"
            "DOCS_warmStartDocumentLoader.startLoad(3.0,'LOAD_CONTEXT',0,0);"
        )
        self.assertEqual(
            parse_save_config(html, "doc"),
            {"path": "/videos/d/doc/save", "sid": "SAVE_SESSION"},
        )
        self.assertEqual(
            parse_save_config(html.replace("xhrPrefix: '/videos/d/doc'", "xhrPrefix: ''"), "doc"),
            {"path": "/videos/d/doc/save", "sid": "SAVE_SESSION"},
        )

    def test_save_configuration_rejects_duplicates_missing_fields_and_other_documents(self):
        good = "xhrPrefix: '/videos/d/doc', urlPrefix: '/videos/d/doc', sid: 'SAVE_SESSION'"
        for html in (
            "no config",
            good.replace("sid: 'SAVE_SESSION'", ""),
            good + ", sid: 'second-session'",
            good.replace("xhrPrefix: '/videos/d/doc'", "xhrPrefix: '/videos/d/other'"),
            good.replace("xhrPrefix: '/videos/d/doc'", "xhrPrefix: 'https://private.invalid/doc'"),
            good.replace("SAVE_SESSION", "private value"),
        ):
            with self.subTest(html=html), self.assertRaises(ValueError) as caught:
                parse_save_config(html, "doc")
            self.assertNotIn("private", str(caught.exception))

    def test_native_image_fits_existing_scene_without_changing_existing_commands(self):
        model = document()
        before = copy.deepcopy(model)
        image = {"width": 1024, "height": 1024, "cosmo_id": "uploaded-cosmo"}
        commands = image_insert_commands(
            model,
            "new-shape",
            "scene",
            image,
            "https://docs.google.com/videos-images-rt/synthetic",
        )
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command[:3], [3, "new-shape", 3])
        self.assertEqual(command[5], "scene")
        self.assertEqual(command[3], [160.734375, 0, 0, 160.734375, 100584.0, 20574.0])
        properties = dict(zip(command[4][::2], command[4][1::2]))
        self.assertEqual(properties[8], 1024)
        self.assertEqual(properties[9], 1024)
        self.assertEqual(properties[49], "uploaded-cosmo")
        self.assertEqual(properties[177], 1)
        self.assertEqual(model, before)

    def test_native_image_rejects_missing_scene_existing_shape_and_unsafe_snapshot(self):
        image = {"width": 1024, "height": 1024, "cosmo_id": "uploaded-cosmo"}
        for shape_id, scene_id, mutation in (
            ("new-shape", "missing", None),
            ("old-shape", "scene", None),
            ("new-shape", "scene", [0, ["old-shape"]]),
            ("new-shape", "scene", [5, ["old-shape"], [], [49, "changed"]]),
            ("new-shape", "scene", [59, "old-shape"]),
        ):
            model = document()
            if mutation:
                model["chunk"].append(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                image_insert_commands(model, shape_id, scene_id, image, "synthetic")

    def test_document_scenes_rejects_shape_delete_snapshot(self):
        model = document()
        self.assertEqual(document_scenes(model), ["scene"])
        model["chunk"].append([0, ["old-shape"]])
        with self.assertRaisesRegex(ValueError, "shape deletes"):
            document_scenes(model)

    def test_native_image_rejects_invalid_page_and_uploaded_dimensions(self):
        for page in ([0, 100], [100, float("inf")], [100, float("nan")], [True, 100], [100]):
            model = document()
            model["chunk"][0][1] = page
            with self.subTest(page=page), self.assertRaises(ValueError):
                image_insert_commands(
                    model, "new-shape", "scene", {"width": 1, "height": 1, "cosmo_id": "c"}, "u"
                )
        for width in (0, -1, 32769, True, 1024.5, "1024"):
            with self.subTest(width=width), self.assertRaises(ValueError):
                image_insert_commands(
                    document(),
                    "new-shape",
                    "scene",
                    {"width": width, "height": 1, "cosmo_id": "c"},
                    "u",
                )

    def test_save_acknowledgement_requires_strictly_ordered_integer_revisions(self):
        self.assertEqual(saved_revision({"revisionRanges": [[4, 4], [6, 7]]}, 3), 7)
        for response in (
            None,
            {},
            {"revisionRanges": []},
            {"metadata": None, "revisionRanges": [[4, 4]]},
            {"metadata": {"needsTransformOnClient": True}, "revisionRanges": [[4, 4]]},
            {"revisionRanges": [[3, 4]]},
            {"revisionRanges": [[4, 3]]},
            {"revisionRanges": [[4, 5], [5, 6]]},
            {"revisionRanges": [[True, 4]]},
            {"revisionRanges": [[4.0, 4]]},
            {"revisionRanges": [[4]]},
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                saved_revision(response, 3)

    def make_client(self, *, saved=True):
        with patch("src.services.vids.client.AsyncSession"):
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
        checks = []
        image = {"width": 1024, "height": 1024, "cosmo_id": "uploaded-cosmo"}

        async def refresh():
            model = document()
            if client._send.await_count:
                model["revision"] = 4
                if saved:
                    model["chunk"].extend(
                        image_insert_commands(
                            document(),
                            "new-shape",
                            "scene",
                            image,
                            "https://docs.google.com/videos-images-rt/synthetic",
                        )
                    )
            client.model_chunk = model
            client.flags = {"info_params": {"token": "synthetic-token", "ouid": "synthetic-user"}}
            client.document_save_config = {"path": "/videos/d/doc/save", "sid": "SAVE_SESSION"}

        client.bootstrap = AsyncMock(side_effect=refresh)
        client.upload_image = AsyncMock(return_value=image)
        client.document_image_url = AsyncMock(
            return_value="https://docs.google.com/videos-images-rt/synthetic"
        )
        client._send = AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                text=")]}'\n" + json.dumps({"revisionRanges": [[4, 4]]}),
            )
        )
        client.before_generate = lambda: checks.append("identity")
        return client, checks

    def test_http_insert_saves_once_then_confirms_shape_cosmo_scene_and_revision(self):
        client, checks = self.make_client()
        checkpoints = []
        result = asyncio.run(
            client.insert_document_image(
                "synthetic.jpg",
                mime_type="image/jpeg",
                shape_id="new-shape",
                scene_id="scene",
                checkpoint=checkpoints.append,
            )
        )
        self.assertEqual(result, {"shape_id": "new-shape", "scene_id": "scene", "revision": 4})
        client.upload_image.assert_awaited_once_with(
            "synthetic.jpg",
            mime_type="image/jpeg",
            with_metadata=True,
        )
        client._send.assert_awaited_once()
        call = client._send.call_args
        self.assertEqual(call.args, ("post", DOCS_ORIGIN + "/videos/d/doc/save"))
        self.assertEqual(call.kwargs["params"]["id"], "doc")
        self.assertEqual(call.kwargs["data"]["rev"], "3")
        bundle = json.loads(call.kwargs["data"]["bundles"])[0]
        self.assertEqual(bundle["sid"], "SAVE_SESSION")
        self.assertEqual(bundle["reqId"], 0)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(set(call.kwargs["headers"]), {"Origin", "Referer"})
        self.assertEqual(client.bootstrap.await_count, 2)
        self.assertEqual(len(checks), 3)
        self.assertEqual(len(checkpoints), 2)
        self.assertNotIn("acknowledged_revision", checkpoints[0])
        self.assertEqual(checkpoints[1]["acknowledged_revision"], 4)

    def test_invalid_target_is_rejected_before_upload_or_save(self):
        client, _ = self.make_client()
        with self.assertRaises(DocumentImageSourceError):
            asyncio.run(
                client.insert_document_image(
                    "synthetic.jpg",
                    mime_type="image/jpeg",
                    shape_id="new-shape",
                    scene_id="missing",
                )
            )
        client.upload_image.assert_not_awaited()
        client._send.assert_not_awaited()

    def test_shape_delete_snapshot_is_rejected_before_upload_or_save(self):
        client, _ = self.make_client()
        refresh = client.bootstrap.side_effect

        async def deleted_snapshot():
            await refresh()
            client.model_chunk["chunk"].append([0, ["old-shape"]])

        client.bootstrap.side_effect = deleted_snapshot
        with self.assertRaises(DocumentImageSourceError):
            asyncio.run(
                client.insert_document_image(
                    "synthetic.jpg",
                    mime_type="image/jpeg",
                    shape_id="new-shape",
                    scene_id="scene",
                )
            )
        client.upload_image.assert_not_awaited()
        client.document_image_url.assert_not_awaited()
        client._send.assert_not_awaited()

    def test_identity_drift_prevents_upload_or_save_without_retries(self):
        for fail_at in (1, 2):
            client, _ = self.make_client()
            calls = []

            def verify():
                calls.append("check")
                if len(calls) == fail_at:
                    raise ValueError("Account identity changed")

            client.before_generate = verify
            with self.subTest(fail_at=fail_at), self.assertRaises(ValueError):
                asyncio.run(
                    client.insert_document_image(
                        "synthetic.jpg",
                        mime_type="image/jpeg",
                        shape_id="new-shape",
                        scene_id="scene",
                    )
                )
            client._send.assert_not_awaited()
            self.assertEqual(client.upload_image.await_count, fail_at - 1)

    def test_save_failure_or_conflict_never_repeats_the_mutation(self):
        for failure in (
            RequestException("synthetic transport failure"),
            OSError("synthetic transport failure"),
            SimpleNamespace(status_code=401, text="synthetic login failure"),
            SimpleNamespace(status_code=409, text="synthetic conflict"),
            SimpleNamespace(
                status_code=200,
                text=json.dumps(
                    {
                        "metadata": {"needsTransformOnClient": True, "serverRevision": 4},
                    }
                ),
            ),
        ):
            client, _ = self.make_client()
            if isinstance(failure, Exception):
                client._send.side_effect = failure
            else:
                client._send.return_value = failure
            with (
                self.subTest(failure=type(failure).__name__),
                self.assertRaises((VidsTransportError, VidsProtocolError, ValueError)),
            ):
                asyncio.run(
                    client.insert_document_image(
                        "synthetic.jpg",
                        mime_type="image/jpeg",
                        shape_id="new-shape",
                        scene_id="scene",
                    )
                )
            client._send.assert_awaited_once()
            client.upload_image.assert_awaited_once()
            client.bootstrap.assert_awaited_once()

    def test_save_acknowledgement_requires_confirming_snapshot(self):
        client, _ = self.make_client(saved=False)
        with self.assertRaises(VidsTransportError):
            asyncio.run(
                client.insert_document_image(
                    "synthetic.jpg",
                    mime_type="image/jpeg",
                    shape_id="new-shape",
                    scene_id="scene",
                )
            )
        client._send.assert_awaited_once()
        client.upload_image.assert_awaited_once()
        self.assertEqual(client.bootstrap.await_count, 2)

    def test_bootstrap_retains_save_config_and_clears_it_when_absent(self):
        flags = {
            "docs-gaopd": "appsgenaiserver-pa.clients6.google.com",
            "espresso_api_key": "synthetic-key",
            "docs-est": "synthetic-token",
        }
        html = "_docs_flag_initialData=" + json.dumps(flags) + ";"
        response = SimpleNamespace(
            status_code=200,
            text=html
            + "xhrPrefix: '/videos/d/doc', urlPrefix: '/videos/d/doc', sid: 'SAVE_SESSION'",
        )
        with patch("src.services.vids.client.AsyncSession") as session:
            session.return_value.get = AsyncMock(return_value=response)
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            asyncio.run(client.bootstrap())
            self.assertEqual(
                client.document_save_config,
                {
                    "path": "/videos/d/doc/save",
                    "sid": "SAVE_SESSION",
                },
            )
            response.text = html
            asyncio.run(client.bootstrap())
            self.assertIsNone(client.document_save_config)

    def test_upload_metadata_is_optional_and_requires_dimensions(self):
        completion = {
            "status": "SUCCESS",
            "customerSpecificInfo": {
                "id": "uploaded-cosmo",
                "width": "1024",
                "height": "768",
            },
        }
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "sessionStatus": {
                    "additionalInfo": {
                        "uploader_service.GoogleRupioAdditionalInfo": {
                            "completionInfo": completion
                        },
                    }
                },
            },
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.services.vids.client.AsyncSession"),
        ):
            path = Path(directory) / "synthetic.jpg"
            path.write_bytes(b"synthetic image bytes")
            client = VidsClient("https://docs.google.com/videos/d/doc/edit", [])
            client.flags = {
                "scotty_upload_url": "/upload/videos/resumable",
                "info_params": {"token": "synthetic-token", "ouid": "synthetic-user"},
            }
            client._send = AsyncMock(return_value=response)
            self.assertEqual(
                asyncio.run(client.upload_image(path, mime_type="image/jpeg")), "uploaded-cosmo"
            )
            self.assertEqual(
                asyncio.run(client.upload_image(path, mime_type="image/jpeg", with_metadata=True)),
                {
                    "cosmo_id": "uploaded-cosmo",
                    "width": 1024,
                    "height": 768,
                },
            )
            del completion["customerSpecificInfo"]["width"]
            with self.assertRaises(ValueError):
                asyncio.run(client.upload_image(path, mime_type="image/jpeg", with_metadata=True))


if __name__ == "__main__":
    unittest.main()
