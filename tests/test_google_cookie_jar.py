"""Google cookie scope survives response updates and persistence."""

import json
import time
import unittest

from curl_cffi.requests.headers import Headers

from src.services.flow.google_cookies import GoogleCookieJar


class GoogleCookieJarTests(unittest.TestCase):
    def merge(self, jar, *lines, url="https://accounts.google.com/oauth/continue"):
        jar.merge(Headers([("Set-Cookie", line) for line in lines]), url)

    def test_chrome_and_playwright_metadata_survive_round_trip(self):
        expires = int(time.time()) + 86400
        rows = [
            {
                "name": "SID",
                "value": "root",
                "domain": ".google.com",
                "path": "/",
                "hostOnly": False,
                "secure": True,
                "httpOnly": True,
                "sameSite": "lax",
                "expirationDate": expires,
            },
            {
                "name": "SID",
                "value": "account",
                "domain": "accounts.google.com",
                "path": "/oauth",
                "secure": True,
                "httpOnly": False,
                "sameSite": "None",
                "expires": -1,
            },
            {
                "name": "OSID",
                "value": "flow",
                "domain": "flow.google.com",
                "path": "/",
                "secure": False,
                "httpOnly": True,
                "sameSite": "Strict",
                "expires": expires,
            },
        ]
        jar = GoogleCookieJar(rows)
        exported = jar.export()
        self.assertEqual(exported[0], {**rows[0], "session": False})
        self.assertTrue(exported[1]["hostOnly"])
        self.assertTrue(exported[1]["session"])
        self.assertNotIn("expirationDate", exported[1])
        self.assertEqual(exported[2]["expirationDate"], expires)
        self.assertEqual(GoogleCookieJar(json.dumps(exported)).export(), exported)
        self.assertEqual(
            jar.header("https://accounts.google.com/oauth/start"), "SID=account; SID=root"
        )
        self.assertEqual(jar.header("https://flow.google.com/"), "SID=root; OSID=flow")
        self.assertEqual(jar.header("http://flow.google.com/"), "OSID=flow")
        self.assertEqual(jar.header("https://child.accounts.google.com/oauth/start"), "SID=root")
        self.assertEqual(jar.header("https://accounts.google.com/oauth-sibling"), "SID=root")
        self.assertEqual(jar.header("https://labs.google/"), "")

    def test_legacy_formats_have_explicit_scope_assumptions_and_no_invented_expiry(self):
        raw = {"SID": "root", "LSID": "account", "__Host-GAPS": "gaps", "OSID": "flow"}
        for value in (
            raw,
            "; ".join(f"{k}={v}" for k, v in raw.items()),
            {"refreshCookie": raw},
            {"cookies": raw},
        ):
            with self.subTest(value=value):
                jar = GoogleCookieJar(value)
                self.assertEqual(
                    jar.header("https://accounts.google.com/"),
                    "SID=root; LSID=account; __Host-GAPS=gaps",
                )
                self.assertEqual(jar.header("https://flow.google.com/"), "SID=root; OSID=flow")
                for item in jar.export():
                    self.assertEqual(item["scopeSource"], "legacy")
                    self.assertTrue(item["session"])
                    self.assertNotIn("expirationDate", item)

    def test_response_cookie_default_path_host_only_and_priority_attribute(self):
        jar = GoogleCookieJar([])
        self.merge(jar, "LSID=accounts; Secure; HttpOnly; SameSite=Lax; Priority=High")
        item = jar.export()[0]
        self.assertEqual(item["path"], "/oauth")
        self.assertTrue(item["hostOnly"])
        self.assertTrue(item["httpOnly"])
        self.assertEqual(item["sameSite"], "Lax")
        self.assertEqual(jar.header("https://accounts.google.com/oauth/next"), "LSID=accounts")
        self.assertEqual(jar.header("https://accounts.google.com/other"), "")
        self.assertEqual(jar.header("https://child.accounts.google.com/oauth/next"), "")
        self.assertEqual([row["name"] for row in jar.export()], ["LSID"])

    def test_same_name_multiple_scopes_survive_rotations_and_deletions(self):
        jar = GoogleCookieJar([])
        self.merge(jar, "SID=root; Domain=.google.com; Path=/", "SID=account; Path=/oauth")
        self.merge(jar, "SID=flow; Path=/", url="https://flow.google.com/login")
        self.assertEqual(len(jar.export()), 3)
        self.merge(
            jar, "SID=deleted; Path=/oauth; Max-Age=0; Expires=Wed, 01 Jan 2040 00:00:00 GMT"
        )
        self.assertEqual(jar.header("https://accounts.google.com/oauth/next"), "SID=root")
        self.assertEqual(jar.header("https://flow.google.com/"), "SID=root; SID=flow")
        self.merge(
            jar,
            "SID=new; Domain=.google.com; Path=/; Max-Age=600; Expires=Wed, 01 Jan 2020 00:00:00 GMT",
        )
        self.assertIn("SID=new", jar.header("https://accounts.google.com/"))
        self.merge(
            jar, "SID=gone; Domain=.google.com; Path=/; Expires=Wed, 01 Jan 2020 00:00:00 GMT"
        )
        self.assertEqual(jar.header("https://flow.google.com/"), "SID=flow")

    def test_recaptcha_www_responses_preserve_parent_and_host_only_scopes(self):
        anchor = "https://www.google.com/recaptcha/enterprise/anchor"
        reload = "https://www.google.com/recaptcha/enterprise/reload"
        jar = GoogleCookieJar([])
        self.merge(
            jar,
            "NID=parent; Domain=.google.com; Path=/; Secure; Max-Age=600",
            "session=www; Path=/recaptcha; Secure; HttpOnly",
            url=anchor,
        )
        self.assertEqual(jar.header(reload), "session=www; NID=parent")
        self.assertEqual(jar.header("https://accounts.google.com/"), "NID=parent")
        self.assertEqual(jar.header("https://child.www.google.com/recaptcha/"), "NID=parent")
        exported = jar.export()
        session = next(row for row in exported if row["name"] == "session")
        self.assertEqual(session["domain"], "www.google.com")
        self.assertTrue(session["hostOnly"])
        self.assertTrue(session["httpOnly"])
        self.assertEqual(GoogleCookieJar(exported).header(reload), "session=www; NID=parent")
        self.merge(jar, "NID=rotated; Domain=.google.com; Path=/; Secure", url=reload)
        self.merge(jar, "session=; Path=/recaptcha; Secure; Max-Age=0", url=reload)
        self.assertEqual(jar.header(reload), "NID=rotated")

    def test_recaptcha_www_cannot_modify_other_google_hosts(self):
        jar = GoogleCookieJar(
            [{"name": "LSID", "value": "kept", "domain": "accounts.google.com", "path": "/"}]
        )
        self.merge(
            jar,
            "LSID=; Domain=accounts.google.com; Path=/; Max-Age=0",
            "OSID=wrong; Domain=flow.google.com; Path=/",
            url="https://www.google.com/recaptcha/enterprise/reload",
        )
        self.assertEqual(jar.header("https://accounts.google.com/"), "LSID=kept")
        self.assertEqual(jar.header("https://flow.google.com/"), "")
        self.assertEqual(len(jar.export()), 1)

    def test_foreign_or_parent_domain_set_cookie_cannot_add_or_delete(self):
        jar = GoogleCookieJar(
            [{"name": "OSID", "value": "kept", "domain": ".flow.google.com", "path": "/"}]
        )
        self.merge(
            jar,
            "OSID=; Domain=.flow.google.com; Path=/; Max-Age=0",
            "foreign=value; Domain=evil.test; Path=/",
            "parent=value; Domain=.com; Path=/",
            "parent=value; Domain=.google; Path=/",
            "wrong=value; Domain=..google.com; Path=/",
            "__Secure-next-auth.session-token=labs; Domain=.google.com; Path=/; Secure",
        )
        for url in (
            "https://evil.test/",
            "https://labs.google/",
            "https://other.google.com/",
            "http://accounts.google.com/",
        ):
            self.merge(jar, "SID=bad; Domain=.google.com; Path=/", url=url)
        self.assertEqual(jar.header("https://flow.google.com/"), "OSID=kept")
        self.assertEqual(len(jar.export()), 1)

    def test_expired_imports_partitioned_and_invalid_prefix_cookies_are_rejected(self):
        rows = [
            {
                "name": "SID",
                "value": "expired",
                "domain": ".google.com",
                "expirationDate": time.time() - 1,
            },
            {
                "name": "SID",
                "value": "partitioned",
                "domain": ".google.com",
                "partitionKey": {"topLevelSite": "https://elsewhere.test"},
            },
            {
                "name": "__Host-GAPS",
                "value": "invalid",
                "domain": ".accounts.google.com",
                "path": "/",
                "secure": True,
            },
            {"name": "__Secure-PSID", "value": "invalid", "domain": ".google.com", "secure": False},
        ]
        jar = GoogleCookieJar(rows)
        self.assertFalse(jar.has_any(("SID",)))
        self.assertEqual(jar.export(), [])
        self.merge(
            jar,
            "__Host-GAPS=invalid; Domain=accounts.google.com; Path=/; Secure",
            "__Secure-PSID=invalid; Path=/",
        )
        self.assertEqual(jar.export(), [])

    def test_independent_jars_never_share_rotation(self):
        first = GoogleCookieJar("SID=first")
        second = GoogleCookieJar("SID=second")
        self.merge(first, "SID=rotated; Domain=.google.com; Path=/")
        self.assertEqual(second.header("https://accounts.google.com/"), "SID=second")
        self.assertEqual(first.header("https://accounts.google.com/"), "SID=rotated")

    def test_explicit_domain_without_dot_remains_domain_cookie(self):
        jar = GoogleCookieJar([])
        self.merge(jar, "scoped=value; Domain=accounts.google.com; Path=/")
        self.assertFalse(jar.export()[0]["hostOnly"])
        self.assertEqual(jar.export()[0]["domain"], "accounts.google.com")
        restored = GoogleCookieJar(jar.export())
        self.assertEqual(restored.header("https://child.accounts.google.com/"), "scoped=value")

    def test_legacy_gapsts_is_accounts_host_only_and_secure(self):
        jar = GoogleCookieJar("SID=root; __Host-GAPSTS=accounts; Priority=High")
        restored = GoogleCookieJar(jar.export())
        self.assertIn("__Host-GAPSTS=accounts", restored.header("https://accounts.google.com/"))
        self.assertNotIn("__Host-GAPSTS", restored.header("https://flow.google.com/"))
        self.assertNotIn("__Host-GAPSTS", restored.header("http://accounts.google.com/"))
        self.assertNotIn("Priority", restored.header("https://accounts.google.com/"))

    def test_invalid_import_types_and_controls_do_not_change_scope(self):
        baseline = {"name": "SID", "value": "value", "domain": ".google.com"}
        for fields in (
            {"hostOnly": "false"},
            {"secure": "false"},
            {"httpOnly": 1},
            {"session": "true"},
            {"name": "SID\x7f"},
            {"value": "secret\x01"},
            {"name": None, "domain": ""},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(GoogleCookieJar([{**baseline, **fields}]).export(), [])
        jar = GoogleCookieJar([baseline])
        self.assertEqual(jar.header("https://user@accounts.google.com/"), "")
        self.assertEqual(jar.header("https://accounts.google.com:8443/"), "")

    def test_expired_and_empty_deletions_do_not_persist(self):
        jar = GoogleCookieJar("SID=root")
        self.merge(jar, "SID=; Domain=.google.com; Path=/; Max-Age=0")
        self.assertEqual(jar.export(), [])

    def test_javascript_export_requires_recorded_scope_and_httponly_metadata(self):
        baseline = {
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "name": "visible",
            "value": "synthetic-visible",
        }
        rows = [
            baseline,
            {**baseline, "name": "private", "httpOnly": True},
            {**baseline, "name": "legacy", "scopeSource": "legacy"},
            {
                key: value
                for key, value in {**baseline, "name": "unknown"}.items()
                if key != "httpOnly"
            },
            {key: value for key, value in {**baseline, "name": "scope"}.items() if key != "path"},
        ]
        jar = GoogleCookieJar(rows)
        self.assertEqual([row["name"] for row in jar.javascript_cookies()], ["visible"])
        restored = GoogleCookieJar(jar.export())
        self.assertEqual(restored.export(), jar.export())
        self.assertEqual(restored.javascript_cookies(), jar.javascript_cookies())
        self.assertIn("unknown=synthetic-visible", restored.header("https://www.google.com/"))
        self.merge(restored, "unknown=rotated; Domain=.google.com; Path=/; Secure")
        visible = {row["name"]: row for row in restored.javascript_cookies()}
        self.assertEqual(visible["unknown"]["value"], "rotated")
        self.assertNotIn("httpOnlySource", visible["unknown"])


if __name__ == "__main__":
    unittest.main()
