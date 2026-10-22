"""Current Flow session probes use synthetic HTML/RPC responses only."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


if __name__ == "__main__":
    unittest.main()
