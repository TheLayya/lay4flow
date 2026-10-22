"""Current Flow session probes use synthetic HTML/RPC responses only."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.flow.flow_rpc import (
    CREDITS_RPC,
    FLOW_URL,
    IDENTITY_RPC,
    FlowRPCError,
    flow_rpc,
    read_flow_session,
)

EMAIL = "account@gmail.com"
CONFIG = {
    "oPEP7c": EMAIL,
    "SNlM0e": "synthetic-private-at",
    "eptZe": "/_/AiSandboxAngularFrontend/",
    "cfb2h": "synthetic-build",
    "FdrFJe": "synthetic-private-sid",
    "QrtxK": "2",
}
RPC_URL = "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute"


def response(text="", *, status=200, url=FLOW_URL, headers=None):
    return SimpleNamespace(
        status=status, url=url, headers=headers or {}, text=AsyncMock(return_value=text)
    )


def bootstrap(config=None):
    data = CONFIG if config is None else config
    return response("<script>window.WIZ_global_data = " + json.dumps(data) + ";</script>")


def people(email=EMAIL):
    person = [None] * 10
    person[9] = [[None, email]]
    return [[[None, None, person]]]


def rpc(rpcid, payload, *, framed=True):
    text = json.dumps([["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]])
    if framed:
        text = ")]}'\n\n" + str(len(text.encode())) + "\n" + text + "\n"
    return response(text, url=RPC_URL)


class FlowRPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_session_reuses_bootstrap_per_path_with_bounded_lifetime(self):
        context = self.context()
        context.flow_bootstrap_cache = {}
        context.request.post.side_effect = None
        context.request.post.return_value = rpc("jwpduf", ["fixture"])
        with patch("src.services.flow.flow_rpc.time.monotonic", return_value=100):
            for _ in range(3):
                await flow_rpc(context, "jwpduf", [], expected_email=EMAIL)
        self.assertEqual(context.request.get.await_count, 1)
        with patch("src.services.flow.flow_rpc.time.monotonic", return_value=161):
            await flow_rpc(context, "jwpduf", [], expected_email=EMAIL)
            await flow_rpc(context, "jwpduf", [], expected_email=EMAIL, source_path="/project/p")
        self.assertEqual(context.request.get.await_count, 3)

    async def test_cached_bootstrap_still_checks_identity_and_error_invalidates_without_replay(
        self,
    ):
        context = self.context()
        context.flow_bootstrap_cache = {}
        context.request.post.side_effect = None
        context.request.post.return_value = rpc("jwpduf", [])
        await flow_rpc(context, "jwpduf", [], expected_email=EMAIL)
        with self.assertRaises(FlowRPCError):
            await flow_rpc(context, "ogiZ0b", [], expected_email="other@gmail.com")
        self.assertEqual(context.request.post.await_count, 1)
        context.request.post.return_value = response(status=401, url=RPC_URL)
        with self.assertRaises(FlowRPCError):
            await flow_rpc(context, "ogiZ0b", [], expected_email=EMAIL)
        self.assertEqual(context.request.post.await_count, 2)
        self.assertEqual(context.flow_bootstrap_cache, {})

    async def test_generation_bootstrap_rejects_changed_identity_before_write(self):
        context = SimpleNamespace(
            request=SimpleNamespace(
                get=AsyncMock(return_value=bootstrap({**CONFIG, "oPEP7c": "other@gmail.com"})),
                post=AsyncMock(),
            )
        )
        with self.assertRaises(FlowRPCError) as caught:
            await flow_rpc(context, "ogiZ0b", [], expected_email=EMAIL)
        self.assertEqual(caught.exception.reason, "identity_mismatch")
        context.request.post.assert_not_awaited()

    async def test_generic_rpc_uses_current_bootstrap_and_returns_decoded_payload(self):
        context = SimpleNamespace(
            request=SimpleNamespace(
                get=AsyncMock(return_value=bootstrap()),
                post=AsyncMock(return_value=rpc("UpteDb", [["project-id"]])),
            )
        )
        result = await flow_rpc(context, "UpteDb", ["projects/*", 21], network_step="flow_projects")
        self.assertEqual(result, [["project-id"]])
        call = context.request.post.await_args
        self.assertEqual(call.kwargs["params"]["rpcids"], "UpteDb")
        body = json.loads(call.kwargs["form"]["f.req"])[0][0]
        self.assertEqual(json.loads(body[1]), ["projects/*", 21])

    def context(self, *, config=None, identity=None, credits=None):
        request = SimpleNamespace(
            get=AsyncMock(return_value=bootstrap(config)),
            post=AsyncMock(
                side_effect=[
                    identity or rpc(IDENTITY_RPC, people()),
                    credits or rpc(CREDITS_RPC, [120, 1]),
                ]
            ),
        )
        return SimpleNamespace(request=request)

    async def error(self, context, reason, step):
        with self.assertRaises(FlowRPCError) as caught:
            await read_flow_session(context)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.code, reason)
        self.assertEqual(caught.exception.network_step, step)
        return caught.exception

    async def test_valid_identity_and_credits_without_legacy_session_or_bearer(self):
        context = self.context()
        self.assertEqual(
            await read_flow_session(context),
            {"email": EMAIL, "credits": 120, "user_paygate_tier": "PAYGATE_TIER_ONE"},
        )
        context.request.get.assert_awaited_once_with(FLOW_URL, timeout=20000, max_redirects=0)
        calls = context.request.post.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual([call.args[0] for call in calls], [RPC_URL, RPC_URL])
        for call, rpcid in zip(calls, [IDENTITY_RPC, CREDITS_RPC]):
            self.assertEqual(call.kwargs["params"]["rpcids"], rpcid)
            self.assertEqual(call.kwargs["params"]["authuser"], "2")
            self.assertEqual(call.kwargs["params"]["f.sid"], CONFIG["FdrFJe"])
            self.assertEqual(call.kwargs["params"]["bl"], CONFIG["cfb2h"])
            self.assertEqual(call.kwargs["form"]["at"], CONFIG["SNlM0e"])
            self.assertEqual(call.kwargs["max_redirects"], 0)
            self.assertNotIn("Authorization", call.kwargs["headers"])
            self.assertNotIn("Cookie", call.kwargs["headers"])
            self.assertNotIn("proxy", call.kwargs)
        first = json.loads(calls[0].kwargs["form"]["f.req"])[0][0]
        self.assertEqual(first[0], IDENTITY_RPC)
        self.assertEqual(
            json.loads(first[1]),
            [
                ["me"],
                [
                    [["person.photo", "person.name", "person.email", "person.metadata"]],
                    None,
                    [1, 7, 10],
                ],
            ],
        )
        self.assertEqual(json.loads(calls[1].kwargs["form"]["f.req"])[0][0][1], "[]")

    async def test_zero_credits_and_unframed_rpc_json_are_valid(self):
        result = await read_flow_session(self.context(credits=rpc(CREDITS_RPC, [0], framed=False)))
        self.assertEqual(result["credits"], 0)
        self.assertIsNone(result["user_paygate_tier"])

    async def test_multiple_framed_records_skip_unrelated_rpc(self):
        other = json.dumps([["wrb.fr", "other", "[]"]])
        valid = rpc(CREDITS_RPC, [30, 2])
        current = await valid.text()
        valid.text.return_value = ")]}'\n" + str(len(other)) + "\n" + other + "\n" + current[5:]
        result = await read_flow_session(self.context(credits=valid))
        self.assertEqual(result["credits"], 30)

    async def test_google_login_redirect_and_http_auth_statuses_need_login(self):
        for status, url, headers in [
            (401, FLOW_URL, {}),
            (403, FLOW_URL, {}),
            (302, FLOW_URL, {"location": "https://accounts.google.com/ServiceLogin"}),
            (200, "https://accounts.google.com/signin/v2/identifier", {}),
        ]:
            with self.subTest(status=status, url=url):
                context = self.context()
                context.request.get.return_value = response(status=status, url=url, headers=headers)
                await self.error(context, "login_required", "flow_bootstrap")
                context.request.post.assert_not_awaited()
        for status in (401, 403):
            await self.error(
                self.context(identity=response(status=status, url=RPC_URL)),
                "login_required",
                "flow_identity",
            )
            await self.error(
                self.context(credits=response(status=status, url=RPC_URL)),
                "login_required",
                "flow_credits",
            )

    async def test_missing_signed_in_identity_needs_login(self):
        for email in (None, ""):
            context = self.context(config={**CONFIG, "oPEP7c": email})
            await self.error(context, "login_required", "flow_bootstrap")
            context.request.post.assert_not_awaited()

    async def test_invalid_bootstrap_never_executes_javascript_or_changes_origin(self):
        for key, value in [
            ("eptZe", "https://evil.test/"),
            ("eptZe", "//evil.test/"),
            ("eptZe", "/_/AiSandboxAngularFrontend/../evil/"),
            ("SNlM0e", None),
            ("cfb2h", []),
            ("FdrFJe", ""),
            ("QrtxK", "2&other=1"),
        ]:
            with self.subTest(key=key, value=value):
                context = self.context(config={**CONFIG, key: value})
                await self.error(context, "account_probe_error", "flow_bootstrap")
                context.request.post.assert_not_awaited()
        context = self.context()
        context.request.get.return_value = response(
            "<script>WIZ_global_data = dangerous();</script>"
        )
        await self.error(context, "account_probe_error", "flow_bootstrap")

    async def test_untrusted_response_origin_is_rejected(self):
        for url in (
            "https://flow.google.com.evil.test/",
            "http://flow.google.com/",
            "https://flow.google.com:444/",
        ):
            with self.subTest(url=url):
                context = self.context()
                context.request.get.return_value.url = url
                await self.error(context, "account_probe_error", "flow_bootstrap")

    async def test_people_identity_must_exist_and_match_bootstrap_strictly(self):
        for payload in (
            [],
            {},
            [[[None, None, {"email": EMAIL}]]],
            people("other@gmail.com"),
        ):
            with self.subTest(payload=payload):
                context = self.context(identity=rpc(IDENTITY_RPC, payload))
                await self.error(context, "identity_mismatch", "flow_identity")
                self.assertEqual(context.request.post.await_count, 1)
        result = await read_flow_session(
            self.context(identity=rpc(IDENTITY_RPC, people(EMAIL.upper())))
        )
        self.assertEqual(result["email"], EMAIL)
        alias = await read_flow_session(
            self.context(
                identity=rpc(IDENTITY_RPC, people("account@googlemail.com")),
                config={**CONFIG, "oPEP7c": "account@gmail.com"},
            )
        )
        self.assertEqual(alias["email"], "account@googlemail.com")

    async def test_invalid_credits_are_rejected(self):
        for payload in ([], {}, [True], [-1], ["120"], [1.5], [None]):
            with self.subTest(payload=payload):
                await self.error(
                    self.context(credits=rpc(CREDITS_RPC, payload)),
                    "credits_invalid",
                    "flow_credits",
                )

    async def test_rpc_error_payloads_and_malformed_data_are_bounded(self):
        for code in (7, 16, 13):
            text = json.dumps(
                [["wrb.fr", CREDITS_RPC, None, None, None, [code, "private-upstream-message"]]]
            )
            reason = "login_required" if code in (7, 16) else "transient_error"
            error = await self.error(
                self.context(credits=response(text, url=RPC_URL)), reason, "flow_credits"
            )
            self.assertNotIn("private-upstream-message", str(error))
        for text in (
            "private-html",
            ")]}'\n99\n[[",
            '[["wrb.fr","wrong","[]"]]',
            '[["wrb.fr","nzlxg",null]]',
        ):
            with self.subTest(text=text):
                await self.error(
                    self.context(credits=response(text, url=RPC_URL)),
                    "account_probe_error",
                    "flow_credits",
                )

    async def test_transport_and_upstream_failures_preserve_stage_without_secrets(self):
        for method, step in (("get", "flow_bootstrap"), ("post", "flow_identity")):
            context = self.context()
            getattr(context.request, method).side_effect = TimeoutError("private-token-and-cookie")
            error = await self.error(context, "transient_error", step)
            self.assertNotIn("private-token-and-cookie", str(error))
            self.assertIsNone(error.__cause__)
        context = self.context(credits=response(status=503, url=RPC_URL))
        error = await self.error(context, "transient_error", "flow_credits")
        self.assertEqual(error.http_status, 503)

    async def test_project_source_path_and_timeout_use_same_origin_bootstrap(self):
        context = self.context()
        context.request.post.side_effect = None
        context.request.post.return_value = rpc("ogiZ0b", [["synthetic-result"]])
        value = await flow_rpc(
            context,
            "ogiZ0b",
            ["payload"],
            network_step="flow_generate_image",
            source_path="/project/synthetic-project",
            timeout_ms=45000,
        )
        self.assertEqual(value, [["synthetic-result"]])
        context.request.get.assert_awaited_once_with(
            "https://flow.google.com/project/synthetic-project", timeout=20000, max_redirects=0
        )
        call = context.request.post.call_args.kwargs
        self.assertEqual(call["params"]["source-path"], "/project/synthetic-project")
        self.assertEqual(
            call["headers"]["Referer"], "https://flow.google.com/project/synthetic-project"
        )
        self.assertEqual(call["timeout"], 45000)

    async def test_invalid_source_paths_are_rejected_before_any_requests(self):
        for path in (
            "//evil.invalid",
            "https://evil.invalid",
            "/project/../other",
            "/project/a?token=private",
            "/project/a/b",
            "/projects#fragment",
        ):
            with self.subTest(path=path):
                context = self.context()
                with self.assertRaises(FlowRPCError):
                    await flow_rpc(context, "ogiZ0b", [], source_path=path)
                context.request.get.assert_not_awaited()
                context.request.post.assert_not_awaited()

    async def test_explicit_xsrf_rejection_retries_once_with_updated_at_only(self):
        xsrf = response(json.dumps([["xsrf", "rotated-private-at"]]), url=RPC_URL)
        context = self.context()
        context.request.post.side_effect = [xsrf, rpc("ogiZ0b", ["done"])]
        self.assertEqual(await flow_rpc(context, "ogiZ0b", ["unchanged-payload"]), ["done"])
        self.assertEqual(context.request.post.await_count, 2)
        first, second = context.request.post.await_args_list
        self.assertEqual(first.kwargs["form"]["at"], CONFIG["SNlM0e"])
        self.assertEqual(second.kwargs["form"]["at"], "rotated-private-at")
        self.assertEqual(first.kwargs["form"]["f.req"], second.kwargs["form"]["f.req"])
        self.assertEqual(first.kwargs["params"], second.kwargs["params"])

    async def test_xsrf_sidechannel_with_error_frame_and_no_payload_can_retry(self):
        body = [["er", None, None, None, None, 400], ["xsrf", "rotated-private-at"]]
        context = self.context()
        context.request.post.side_effect = [
            response(json.dumps(body), url=RPC_URL),
            rpc("ogiZ0b", ["done"]),
        ]
        self.assertEqual(await flow_rpc(context, "ogiZ0b", []), ["done"])
        self.assertEqual(context.request.post.await_count, 2)

    async def test_xsrf_rotation_does_not_repeat_a_successful_or_unknown_outcome(self):
        for text in (
            json.dumps([["xsrf", "private-at"], ["wrb.fr", "ogiZ0b", "[1]"]]),
            'html mentions ["xsrf","private-at"]',
        ):
            with self.subTest(text=text):
                context = self.context()
                context.request.post.side_effect = None
                context.request.post.return_value = response(text, url=RPC_URL)
                if text.startswith("html"):
                    with self.assertRaises(FlowRPCError):
                        await flow_rpc(context, "ogiZ0b", [])
                else:
                    self.assertEqual(await flow_rpc(context, "ogiZ0b", []), [1])
                self.assertEqual(context.request.post.await_count, 1)
        context = self.context()
        context.request.post.side_effect = TimeoutError("unknown-private-outcome")
        with self.assertRaises(FlowRPCError) as caught:
            await flow_rpc(context, "ogiZ0b", [])
        self.assertEqual(context.request.post.await_count, 1)
        self.assertNotIn("unknown-private-outcome", str(caught.exception))

    async def test_repeated_xsrf_failure_stops_after_second_attempt(self):
        context = self.context()
        context.request.post.side_effect = [
            response(json.dumps([["xsrf", "new-at-one"]]), url=RPC_URL),
            response(json.dumps([["xsrf", "new-at-two"]]), url=RPC_URL),
        ]
        with self.assertRaises(FlowRPCError) as caught:
            await flow_rpc(context, "ogiZ0b", [])
        self.assertEqual(caught.exception.reason, "xsrf_rejected")
        self.assertEqual(context.request.post.await_count, 2)
        self.assertNotIn("new-at", str(caught.exception))

    async def test_er_code_and_public_error_are_bounded_and_do_not_expose_body(self):
        for code in (401, 16, 400, 99999, "private-code"):
            context = self.context()
            context.request.post.side_effect = None
            context.request.post.return_value = response(
                json.dumps(
                    [
                        [
                            "er",
                            None,
                            "private-response-body",
                            None,
                            None,
                            code,
                            ["PUBLIC_ERROR_HIGH_TRAFFIC"],
                        ]
                    ]
                ),
                url=RPC_URL,
            )
            with self.assertRaises(FlowRPCError) as caught:
                await flow_rpc(context, "ogiZ0b", [])
            error = caught.exception
            self.assertEqual(
                error.rpc_code, code if type(code) is int and 0 <= code <= 599 else None
            )
            self.assertEqual(error.public_error, "PUBLIC_ERROR_HIGH_TRAFFIC")
            self.assertNotIn("private-response-body", str(error))
            self.assertNotIn("private-code", str(error))
            self.assertEqual(context.request.post.await_count, 1)

    async def test_status_typed_public_aitk_error_maps_numeric_enum_without_message(self):
        detail = [
            "type.googleapis.com/google.internal.labs.aisandbox.proto.common.v1.PublicAitkError",
            [49],
        ]
        row = ["wrb.fr", "ogiZ0b", None, None, None, [13, "private-status-message", [detail]]]
        context = self.context()
        context.request.post.side_effect = None
        context.request.post.return_value = response(json.dumps([row]), url=RPC_URL)
        with self.assertRaises(FlowRPCError) as caught:
            await flow_rpc(context, "ogiZ0b", [])
        error = caught.exception
        self.assertEqual(error.rpc_code, 13)
        self.assertEqual(error.public_error, "PUBLIC_ERROR_UNUSUAL_ACTIVITY")
        self.assertTrue(error.retryable)
        self.assertTrue(error.submission_rejected)
        self.assertNotIn("private-status-message", str(error))
        self.assertEqual(context.request.post.await_count, 1)

    async def test_specific_permanent_public_error_is_not_marked_retryable(self):
        row = [
            "wrb.fr",
            "ogiZ0b",
            None,
            None,
            None,
            [13, "private-status-message", ["PUBLIC_ERROR_UNSAFE_GENERATION"]],
        ]
        context = self.context()
        context.request.post.side_effect = None
        context.request.post.return_value = response(json.dumps([row]), url=RPC_URL)
        with self.assertRaises(FlowRPCError) as caught:
            await flow_rpc(context, "ogiZ0b", [])
        self.assertEqual(caught.exception.reason, "rpc_rejected")
        self.assertEqual(caught.exception.public_error, "PUBLIC_ERROR_UNSAFE_GENERATION")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(context.request.post.await_count, 1)

    async def test_permission_denied_uses_typed_public_error_before_login_classification(self):
        for code, enum, reason, retryable in (
            (7, 49, "transient_error", True),
            (7, 50, "transient_error", True),
            (403, 49, "transient_error", True),
            (7, 1, "rpc_rejected", False),
            (7, 11, "rpc_rejected", False),
            (7, None, "login_required", False),
            (16, None, "login_required", False),
            (401, None, "login_required", False),
            (16, 49, "login_required", False),
            (401, 49, "login_required", False),
        ):
            with self.subTest(code=code, enum=enum):
                details = (
                    []
                    if enum is None
                    else [
                        [
                            "type.googleapis.com/google.internal.labs.aisandbox.proto.common.v1.PublicAitkError",
                            [enum],
                        ]
                    ]
                )
                row = [
                    "wrb.fr",
                    "ogiZ0b",
                    None,
                    None,
                    None,
                    [code, "private-status-message", details],
                ]
                context = self.context()
                context.request.post.side_effect = None
                context.request.post.return_value = response(json.dumps([row]), url=RPC_URL)
                with self.assertRaises(FlowRPCError) as caught:
                    await flow_rpc(context, "ogiZ0b", [])
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn("private-status-message", str(caught.exception))
                self.assertEqual(context.request.post.await_count, 1)

    async def test_mixed_or_incomplete_response_does_not_confirm_write_rejection(self):
        error_row = [
            "wrb.fr",
            "ogiZ0b",
            None,
            None,
            None,
            [7, "private-message", ["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]],
        ]
        success_row = ["wrb.fr", "ogiZ0b", "[[]]", None, None, None]
        combined_row = [*error_row[:2], "[[]]", *error_row[3:]]
        for text in (
            json.dumps([error_row, success_row]),
            json.dumps([combined_row]),
            json.dumps([error_row]) + "[",
        ):
            context = self.context()
            context.request.post.side_effect = None
            context.request.post.return_value = response(text, url=RPC_URL)
            with self.assertRaises(FlowRPCError) as caught:
                await flow_rpc(context, "ogiZ0b", [])
            self.assertFalse(caught.exception.submission_rejected)
            self.assertEqual(context.request.post.await_count, 1)

    async def test_unknown_public_error_string_is_not_reflected_and_auth_xsrf_is_not_replayed(self):
        for rows, reason in (
            (
                [["wrb.fr", "ogiZ0b", None, None, None, [13, "PUBLIC_ERROR_PRIVATE_SECRET"]]],
                "transient_error",
            ),
            ([["er", None, None, None, None, 401], ["xsrf", "private-token"]], "login_required"),
        ):
            context = self.context()
            context.request.post.side_effect = None
            context.request.post.return_value = response(json.dumps(rows), url=RPC_URL)
            with self.assertRaises(FlowRPCError) as caught:
                await flow_rpc(context, "ogiZ0b", [])
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(context.request.post.await_count, 1)
            self.assertNotIn("PRIVATE_SECRET", str(caught.exception))
            self.assertNotIn("private-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
