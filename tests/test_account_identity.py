import unittest

from src.core.account_identity import same_google_identity


class AccountIdentityTests(unittest.TestCase):
    def test_googlemail_and_gmail_aliases_match(self):
        self.assertTrue(
            same_google_identity("first.last+flow@googlemail.com", "first.last+flow@gmail.com")
        )

    def test_google_aliases_are_case_and_whitespace_insensitive(self):
        self.assertTrue(same_google_identity(" FIRST.LAST@gmail.com ", "first.last@googlemail.com"))

    def test_dots_and_plus_suffixes_remain_distinct(self):
        self.assertFalse(same_google_identity("first.last@gmail.com", "firstlast@googlemail.com"))
        self.assertFalse(same_google_identity("user+flow@gmail.com", "user@googlemail.com"))
        self.assertFalse(same_google_identity("different@gmail.com", "user@googlemail.com"))

    def test_non_google_domains_keep_exact_mailbox_identity(self):
        self.assertFalse(
            same_google_identity("first.last+flow@example.com", "firstlast@example.com")
        )
        self.assertFalse(same_google_identity("user@googlemail.com", "user@example.com"))

    def test_empty_values_do_not_match(self):
        self.assertFalse(same_google_identity("", "user@gmail.com"))
        self.assertFalse(same_google_identity(None, None))
