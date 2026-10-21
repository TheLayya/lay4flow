"""Account identity comparisons for Google email aliases."""

_GOOGLE_MAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def same_google_identity(left: object, right: object) -> bool:
    """Compare Google mailbox aliases without loosening other email domains."""
    left_value = str(left or "").strip().casefold()
    right_value = str(right or "").strip().casefold()
    if not left_value or not right_value:
        return False
    left_local, left_separator, left_domain = left_value.rpartition("@")
    right_local, right_separator, right_domain = right_value.rpartition("@")
    left_google = left_separator == "@" and left_domain in _GOOGLE_MAIL_DOMAINS
    right_google = right_separator == "@" and right_domain in _GOOGLE_MAIL_DOMAINS
    if left_google or right_google:
        if not left_google or not right_google:
            return False
        return bool(left_local) and left_local == right_local
    return left_value == right_value
