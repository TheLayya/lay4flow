"""Preserve existing admin request contracts and class imports during extraction."""

import hashlib
import json
import unittest

from pydantic import ValidationError

from src.api import admin, admin_schemas

# Remote edition contracts exclude the removed extension routing field.
SCHEMA_DIGESTS = {
    "LoginRequest": "d0d246580364261e6f126d19844f68403b39f208c07430c474821095ffc51b36",
    "AddTokenRequest": "c3c01578758b55c8ea7e03ee2fc619ef0d0d990d3bebd2048350422b43fea245",
    "UpdateTokenRequest": "e804dd9659c2a98643db4e4873b4e801e3902593cefe87bb794361219af00714",
    "ProxyConfigRequest": "08284976c6d6888035baf0cf5abb38bd33a2c45748f8a673184bc2db07e159df",
    "ProxyTestRequest": "9d6cc1915e890cf5101da7bdb31d5ee988a21358a0cc14aee6bbc1b243fb355b",
    "CaptchaScoreTestRequest": "bc7c6a801c7cee08ec0b9fae0d6fbe932b867c399c8a3c9e13c45543c907ab10",
    "GenerationConfigRequest": "02a07bd650813507b8ad55f6443b16e2fddb06373e9a3979fbc8e754cd88ca2c",
    "CallLogicConfigRequest": "ee7037db5ffca9dccb5fb7594df2420ae9dd6b3ab84c1a10fbf338289120d620",
    "ChangePasswordRequest": "c227a9e2c5612293f7d6691158a6d9d885b5cbd3b7112d281cfc90c4356675c4",
    "UpdateAPIKeyRequest": "40dd653e79a359ffe5d0714559a4f67ee779bc1ecb6787357f599d912ab6456d",
    "UpdateDebugConfigRequest": "173e7ff37b02b251671df8ed55de04135f8dc320c3da92fb3e128cad892bd975",
    "UpdateAdminConfigRequest": "45f940bf3db07a8d7958c86af2d6b6003b1fc6bae581253177b5cbe581a0ed1d",
    "ST2ATRequest": "649ab29d64dc3d5182b01e7c1623863e08205c3501e146bef666b82d40bdeb7c",
    "ImportTokenItem": "a0dfd90f976268b5c051568757c9475e730ae22b8df887364be6cce83cce5ea0",
    "ImportTokensRequest": "dd1fad64989f35986b53c61be1af8f46ab251c00f7b8ec8f5b90a474ab70374f",
    "TokenRefreshConfigRequest": "d6aea5faacd262c4ee800c51061b97e15435a1e2cdbacaed743337ba5fabf369",
}


class AdminSchemaTests(unittest.TestCase):
    def test_schema_defaults_required_fields_and_nested_contracts_are_unchanged(self):
        for name, expected_digest in SCHEMA_DIGESTS.items():
            with self.subTest(schema=name):
                model = getattr(admin_schemas, name)
                self.assertIs(getattr(admin, name), model)
                value = model.model_json_schema()
                # The new optional binding is additive; compare every original
                # property/default against the pre-extraction contract.
                if name in {"AddTokenRequest", "UpdateTokenRequest"}:
                    value["properties"].pop("proxy_pool_id")
                schema = json.dumps(value, sort_keys=True, separators=(",", ":"))
                self.assertEqual(
                    hashlib.sha256(schema.encode("utf-8")).hexdigest(), expected_digest
                )

    def test_account_contracts_exclude_extension_routing(self):
        for name in ("AddTokenRequest", "UpdateTokenRequest", "ImportTokenItem"):
            self.assertNotIn("extension_route_key", getattr(admin_schemas, name).model_fields)

    def test_update_token_keeps_omitted_fields_distinct_from_explicit_values(self):
        request = admin.UpdateTokenRequest()
        self.assertEqual(request.model_dump(exclude_unset=True), {})
        self.assertEqual(
            admin.UpdateTokenRequest(st=None).model_dump(exclude_unset=True),
            {"st": None},
        )
        self.assertEqual(
            admin.UpdateTokenRequest(video_enabled=False, video_concurrency=0).model_dump(
                exclude_unset=True
            ),
            {"video_enabled": False, "video_concurrency": 0},
        )

    def test_import_items_keep_defaults_and_reject_invalid_token_list(self):
        request = admin.ImportTokensRequest(tokens=[{"email": "test@example.invalid"}])
        self.assertIsInstance(request.tokens[0], admin.ImportTokenItem)
        self.assertEqual(request.tokens[0].protocol_mode, "session")
        self.assertEqual(request.tokens[0].refresh_interval_minutes, 120)
        self.assertTrue(request.tokens[0].auto_refresh_enabled)
        with self.assertRaises(ValidationError):
            admin.ImportTokensRequest(tokens="invalid")

    def test_pool_binding_is_optional_and_requires_a_positive_integer(self):
        for model in (admin_schemas.AddTokenRequest, admin_schemas.UpdateTokenRequest):
            required = {"st": "synthetic"} if model is admin_schemas.AddTokenRequest else {}
            self.assertIsNone(model(**required).proxy_pool_id)
            self.assertEqual(model(**required, proxy_pool_id=7).proxy_pool_id, 7)
            for value in (0, -1, True, "7"):
                with self.subTest(model=model.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        model(**required, proxy_pool_id=value)

    def test_pool_import_requires_text_and_retains_physical_lines(self):
        text = "\r\nproxy.invalid:80\r\n\r\nsecond.invalid:80\r\n"
        self.assertEqual(admin_schemas.ProxyPoolImportRequest(text=text).text, text)
        for payload in ({}, {"text": None}, {"text": ["proxy.invalid:80"]}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                admin_schemas.ProxyPoolImportRequest(**payload)


if __name__ == "__main__":
    unittest.main()
