"""Keep current media and legacy operation response contracts compatible."""

import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from src.services.flow.media import normalize_video_generation_response, operations_to_media_refs
from src.services.flow_client import FlowClient


class FlowMediaTests(unittest.TestCase):
    def test_parser_import_requires_no_runtime_configuration_or_transport(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['src.core'] = None; sys.modules['curl_cffi'] = None; "
                "from src.services.flow.media import normalize_video_generation_response; "
                "assert normalize_video_generation_response({'media': []}) == {'media': []}",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_media_fills_legacy_operation_without_overwriting_explicit_fields(self):
        response = {
            "futureEnvelope": {"keep": True},
            "media": [
                {
                    "name": "media",
                    "projectId": "media-project",
                    "sceneId": "scene",
                    "mediaMetadata": {
                        "mediaStatus": {
                            "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_FAILED",
                            "error": {"code": 13, "message": "Synthetic failure"},
                        }
                    },
                    "video": {
                        "fifeUrl": "https://example.invalid/video",
                        "dimensions": {"length": "8s"},
                    },
                }
            ],
            "operations": [
                {
                    "operation": {"name": "media", "metadata": {"video": {"duration": "4s"}}},
                    "status": "EXPLICIT_STATUS",
                    "projectId": "explicit-project",
                    "futureOp": 42,
                }
            ],
        }
        untouched = deepcopy(response)
        normalized = normalize_video_generation_response(response, "fallback")
        self.assertEqual(response, untouched)
        self.assertEqual(
            normalized["operations"],
            [
                {
                    "operation": {
                        "name": "media",
                        "metadata": {"video": {"duration": "4s"}},
                        "error": {"code": 13, "message": "Synthetic failure"},
                    },
                    "status": "EXPLICIT_STATUS",
                    "projectId": "explicit-project",
                    "futureOp": 42,
                    "mediaName": "media",
                    "sceneId": "scene",
                }
            ],
        )
        self.assertEqual(normalized["futureEnvelope"], {"keep": True})
        self.assertEqual(
            FlowClient(None)._normalize_video_generation_response(response, "fallback"), normalized
        )

    def test_poll_references_accept_legacy_and_current_names(self):
        operations = [
            {
                "mediaName": " current ",
                "projectId": " project-1 ",
                "operation": {"name": "ignored"},
            },
            {"name": "legacy", "project_id": "project-2"},
            {"operation": {"name": "nested", "projectId": "project-3"}},
            {"operation": {"name": "missing-project"}},
            None,
        ]
        expected = [
            {"name": "current", "projectId": "project-1"},
            {"name": "legacy", "projectId": "project-2"},
            {"name": "nested", "projectId": "project-3"},
        ]
        self.assertEqual(operations_to_media_refs(operations), expected)
        self.assertEqual(FlowClient(None)._operations_to_media_refs(operations), expected)


if __name__ == "__main__":
    unittest.main()
