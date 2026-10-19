"""Keep the business API reference complete and its copyable examples callable."""

import asyncio
import html
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from src.core.media_skus import MEDIA_SKUS, prepare_sku_request

ROOT = Path(__file__).resolve().parents[1]
BUSINESS_PAGES = {
    "downstream",
    "business-models",
    "business-parameters",
    "business-endpoints",
    "business-chat",
    "business-chat-stream",
    "business-images",
    "business-image-edits",
    "business-videos",
    "business-video-query",
    "business-video-download",
    "business-video-delete",
    "business-video-edit",
    "business-billing",
    "business-errors",
}
API_PAGES = {
    "business-models": ("get", "/compat/v1/models"),
    "business-chat": ("post", "/compat/v1/chat/completions"),
    "business-chat-stream": ("post", "/compat/v1/chat/completions"),
    "business-images": ("post", "/compat/v1/images/generations"),
    "business-image-edits": ("post", "/compat/v1/images/edits"),
    "business-videos": ("post", "/compat/v1/videos"),
    "business-video-query": ("get", "/compat/v1/videos/{video_id}"),
    "business-video-download": ("get", "/compat/v1/videos/{video_id}/content"),
    "business-video-delete": ("delete", "/compat/v1/videos/{video_id}"),
    "business-video-edit": ("post", "/compat/v1/videos"),
}


class GuideParser(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.sections = {}
        self.section = None
        self.example = None
        self.example_depth = 0
        self.code = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self.section = {"attrs": attrs, "text": [], "examples": [], "codes": []}
            self.sections[attrs["id"]] = self.section
        if not self.section:
            return
        if self.example and tag == "div":
            self.example_depth += 1
        elif tag == "div" and "example" in attrs.get("class", "").split():
            self.example = {"attrs": attrs, "languages": [], "source": ""}
            self.example_depth = 1
            self.section["examples"].append(self.example)
        if tag == "button" and self.example and "data-language" in attrs:
            self.example["languages"].append(attrs["data-language"])
        if tag == "code":
            self.code = []

    def handle_data(self, data):
        if self.section:
            self.section["text"].append(data)
        if self.code is not None:
            self.code.append(data)

    def handle_endtag(self, tag):
        if tag == "code" and self.code is not None:
            source = "".join(self.code)
            self.section["codes"].append(source)
            if self.example:
                self.example["source"] = source
            self.code = None
        if tag == "div" and self.example:
            self.example_depth -= 1
            if self.example_depth == 0:
                self.example = None
        if tag == "section":
            self.section = None


def documented_path(value):
    path = urlsplit(value).path
    if path.startswith("/compat/v1/models/"):
        return "/compat/v1/models/{model}"
    return re.sub(r"/video_[^/]+", "/{video_id}", path)


class BusinessDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static/api-guide.html").read_text(encoding="utf-8")
        cls.guide = GuideParser(cls.source)

    def test_business_navigation_covers_each_interface_and_supporting_page(self):
        self.assertTrue(BUSINESS_PAGES <= set(self.guide.sections))
        for page in BUSINESS_PAGES:
            with self.subTest(page=page):
                section = self.guide.sections[page]
                self.assertEqual(section["attrs"].get("data-doc-mode"), "business")
                self.assertIn('href="#' + page + '"', self.source)

    def test_default_priority_is_implicit_in_documented_sku_names(self):
        for page in BUSINESS_PAGES:
            for source in self.guide.sections[page]["codes"]:
                for model in re.findall(r'"(?:model|id)"\s*:\s*"(flow-[^"]+)"', source):
                    with self.subTest(page=page, model=model):
                        self.assertFalse(model.endswith("-standard"))
        for source in (
            self.source,
            (ROOT / "docs/DOWNSTREAM_COMPAT.md").read_text(encoding="utf-8"),
        ):
            self.assertIn("standard", source)
            self.assertIn("-standard", source)
            self.assertIn("-relaxed", source)

    def test_each_api_page_has_typed_parameters_request_and_response_examples(self):
        for page in API_PAGES:
            with self.subTest(page=page):
                section = self.guide.sections[page]
                text = "".join(section["text"])
                for label in ("请求参数", "类型", "必填", "请求示例", "响应"):
                    self.assertIn(label, text)
                requests = [item for item in section["examples"] if "data-path" in item["attrs"]]
                self.assertTrue(requests, page)
                for example in requests:
                    self.assertTrue({"json", "curl", "python"} <= set(example["languages"]))
                responses = [
                    item for item in section["examples"] if "data-path" not in item["attrs"]
                ]
                self.assertTrue(responses, "Missing copyable response example")
                for example in responses:
                    source = example["source"].strip()
                    if source.startswith(("{", "[")):
                        self.assertIsInstance(json.loads(source), (dict, list))
                    elif source.startswith("data:"):
                        for line in source.splitlines():
                            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                                json.loads(line[5:])

    def test_request_examples_use_real_routes_valid_skus_and_form_uploads(self):
        from src.main import app

        routes = {
            (method.lower(), route.path)
            for route in app.routes
            for method in getattr(route, "methods", ())
        }
        for page, expected in API_PAGES.items():
            with self.subTest(page=page):
                examples = self.guide.sections[page]["examples"]
                requests = [item for item in examples if "data-path" in item["attrs"]]
                normalized = {
                    (
                        item["attrs"]["data-method"].lower(),
                        documented_path(item["attrs"]["data-path"]),
                    )
                    for item in requests
                }
                self.assertIn(expected, normalized)
                if page == "business-models":
                    self.assertIn(("get", "/compat/v1/models/{model}"), normalized)
                if page == "business-video-query":
                    self.assertIn(("get", "/compat/v1/videos"), normalized)
                for example in requests:
                    attrs = example["attrs"]
                    route = (attrs["data-method"].lower(), documented_path(attrs["data-path"]))
                    self.assertIn(route, routes)
                    if route[0] != "post":
                        continue
                    body = json.loads(example["source"])
                    self.assertIn(body.get("model"), MEDIA_SKUS)
                    prepare_sku_request(body)
                    if attrs.get("data-form") == "true":
                        self.assertTrue(
                            any(
                                isinstance(value, str) and value.startswith("@")
                                for field in body.values()
                                for value in (field if isinstance(field, list) else [field])
                            )
                        )
                    else:
                        self.assertFalse(
                            any(
                                isinstance(value, str) and value.startswith("@")
                                for value in body.values()
                            )
                        )
                    if route[1].endswith("chat/completions"):
                        prepared = prepare_sku_request(body)
                        self.assertTrue(prepared.get("messages"))
                    if page == "business-chat-stream":
                        self.assertIs(body.get("stream"), True)
                        self.assertEqual(attrs.get("data-stream"), "true")
                    if page == "business-video-edit":
                        self.assertIn("edit", MEDIA_SKUS[body["model"]]["allowed_modes"])
                        self.assertEqual(MEDIA_SKUS[body["model"]]["type"], "video")
                if page in {"business-image-edits", "business-video-edit"}:
                    self.assertTrue(
                        any(item["attrs"].get("data-form") == "true" for item in requests)
                    )

    def test_business_openapi_describes_uploads_and_required_request_fields(self):
        from src.main import compat_openapi

        schema = asyncio.run(compat_openapi())
        operations = set(API_PAGES.values()) | {
            ("get", "/compat/v1/models/{model}"),
            ("get", "/compat/v1/videos"),
        }
        for method, path in operations:
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method]
                self.assertEqual(operation["security"], [{"HTTPBearer": []}])
                self.assertTrue(operation.get("responses"))
                response = operation["responses"]["200"].get("content", {})
                if path.endswith("/content"):
                    self.assertIn("video/mp4", response)
                else:
                    success = response.get("application/json", {}).get("schema", {})
                    if "$ref" in success:
                        success = schema["components"]["schemas"][success["$ref"].split("/")[-1]]
                    self.assertEqual(success.get("type"), "object")
                    self.assertTrue(success.get("properties"), "Response fields must be documented")
                if method != "post":
                    continue
                request = operation.get("requestBody", {})
                self.assertTrue(request.get("required"), path)
                content = request.get("content", {})
                expected_media = (
                    "multipart/form-data" if path.endswith("/edits") else "application/json"
                )
                self.assertIn(expected_media, content)
                body = content[expected_media]["schema"]
                if "$ref" in body:
                    body = schema["components"]["schemas"][body["$ref"].split("/")[-1]]
                self.assertEqual(body.get("type"), "object")
                self.assertIn("model", body.get("required", []))
                self.assertIn(
                    "messages" if path.endswith("chat/completions") else "prompt",
                    body.get("required", []),
                )
                if path.endswith("/edits"):
                    self.assertIn("image", body.get("properties", {}))
                if path.endswith("/videos"):
                    self.assertIn("multipart/form-data", content)
                    self.assertIn(
                        "input_video",
                        content["multipart/form-data"]["schema"].get("properties", {}),
                    )

    def test_examples_describe_result_states_without_pretending_video_submit_is_billed(self):
        for page in ("business-videos", "business-video-query", "business-video-edit"):
            with self.subTest(page=page):
                section = self.guide.sections[page]
                objects = [
                    json.loads(item["source"])
                    for item in section["examples"]
                    if "data-path" not in item["attrs"] and item["source"].strip().startswith("{")
                ]
                videos = [item for item in objects if item.get("object") == "video"]
                self.assertTrue(videos, "Video status response required")
                for video in videos:
                    self.assertIn(video["status"], {"queued", "in_progress", "completed", "failed"})
                    self.assertIn(video["model"], MEDIA_SKUS)
                    self.assertNotIn("usage", video)
        download = "".join(self.guide.sections["business-video-download"]["text"])
        self.assertIn("video/mp4", download)
        deleted = self.guide.sections["business-video-delete"]
        objects = [
            json.loads(item["source"])
            for item in deleted["examples"]
            if "data-path" not in item["attrs"] and item["source"].strip().startswith("{")
        ]
        self.assertTrue(any(item.get("deleted") is True for item in objects))

    def test_billing_and_async_gateway_limitations_are_explained(self):
        sections = self.guide.sections
        billing = "".join(sections["business-billing"]["text"])
        self.assertIn("1000", billing)
        self.assertIn("不是", billing)
        self.assertIn("One API", billing)
        self.assertIn("New API", billing)
        limitations = html.unescape(
            " ".join(
                "".join(sections[page]["text"])
                for page in ("downstream", "business-videos", "business-errors")
            )
        )
        self.assertIn("One API", limitations)
        self.assertIn("不支持", limitations)
        self.assertIn("New API", limitations)
        self.assertRegex(limitations, r"插件|适配")
        self.assertIn("超时", limitations)
        self.assertNotIn("/compat/v1/videos/{video_id}/remix", self.source)


if __name__ == "__main__":
    unittest.main()
