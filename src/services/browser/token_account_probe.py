"""Validate a browser Profile through the current Flow identity and credits RPCs."""

from typing import Any

from ...core.account_identity import same_google_identity
from ..flow.flow_rpc import FlowRPCError, read_flow_session


class TokenManagerAccountProbe:
    def __init__(self, token_manager: Any, token: Any, *, discover_identity: bool = False):
        self.token_manager = token_manager
        self.token = token
        self.context = None
        self.validated_session = None
        self.discover_identity = bool(discover_identity)
        self.observed_email = ""
        self.network_step = ""

    def set_context(self, context: Any) -> None:
        self.context = context

    async def verify(self, expected_email: str, context: Any = None) -> dict[str, Any]:
        self.validated_session = None
        self.observed_email = ""
        self.network_step = ""
        if not self.discover_identity and not same_google_identity(
            getattr(self.token, "email", ""), expected_email
        ):
            return {"reason": "identity_mismatch"}
        active_context = context if context is not None else self.context
        if active_context is None:
            return {"reason": "session_invalid"}
        self.network_step = "flow_bootstrap"
        try:
            session = await read_flow_session(active_context)
        except FlowRPCError as error:
            self.network_step = error.network_step
            return {"reason": error.reason}
        except (TimeoutError, ConnectionError):
            return {"reason": "transient_error"}
        except Exception:
            return {"reason": "account_probe_error"}
        if not isinstance(session, dict):
            return {"reason": "account_probe_error"}
        self.network_step = "flow_identity"
        email = str(session.get("email") or "").strip().casefold()
        if not email or "@" not in email:
            return {"reason": "identity_mismatch"}
        self.observed_email = email
        if not self.discover_identity and not same_google_identity(email, expected_email):
            return {"reason": "identity_mismatch"}
        self.network_step = "flow_credits"
        credits = session.get("credits", 0)
        if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
            return {"reason": "credits_invalid"}
        self.validated_session = {
            "at": None,
            "at_expires": None,
            "credits": credits,
            "user_paygate_tier": session.get("user_paygate_tier"),
        }
        return {"email": email, "session_valid": True, "credits": credits}
