"""Admin request contracts, kept independent of route handlers and service state."""

from typing import Annotated, Any, List, Literal, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class ArchiveMediaRequest(BaseModel):
    url: str = Field(min_length=1, max_length=16384)


class DirectProfileLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    proxy_url: str = Field(default="", max_length=4096)
    proxy_pool_id: Optional[int] = Field(default=None, ge=1, strict=True)


class CompleteProfileLoginRequest(BaseModel):
    viewer_id: str = Field(min_length=1, max_length=128)


class AddTokenRequest(BaseModel):
    st: str
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    protocol_mode: str = "session"
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    proxy_pool_id: Optional[int] = Field(default=None, ge=1, strict=True)
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = 120
    browser_profile_source: str = "cookies"


class UpdateTokenRequest(BaseModel):
    st: Optional[str] = None  # Only a changed ST needs validation.
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    image_enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None
    protocol_mode: Optional[str] = None
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    proxy_pool_id: Optional[int] = Field(default=None, ge=1, strict=True)
    auto_refresh_enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None
    browser_profile_source: Optional[str] = None


class ProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    media_proxy_enabled: Optional[bool] = None
    media_proxy_url: Optional[str] = None


class ProxyTestRequest(BaseModel):
    proxy_url: str
    test_url: Optional[str] = "https://labs.google/"
    timeout_seconds: Optional[int] = 15


class ProxyPoolAddRequest(BaseModel):
    proxy_url: str
    note: str = ""
    captcha_enabled: bool = True


class ProxyPoolImportRequest(BaseModel):
    """One proxy per physical line; route validation avoids echoing credentials."""

    text: str


class ProxyPoolUpdateRequest(BaseModel):
    """Editable pool fields; omitted values retain the existing row."""

    proxy_url: Optional[str] = None
    note: Optional[str] = None
    captcha_enabled: Optional[bool] = None


class ProxyPoolBatchRequest(BaseModel):
    """Apply one operation to a bounded list of explicit pool IDs."""

    action: Literal["enable", "disable", "check", "delete"]
    ids: List[Annotated[int, Field(gt=0, strict=True)]] = Field(min_length=1, max_length=500)


class CaptchaScoreTestRequest(BaseModel):
    website_url: Optional[str] = "https://antcpt.com/score_detector/"
    website_key: Optional[str] = "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf"
    action: Optional[str] = "homepage"
    verify_url: Optional[str] = "https://antcpt.com/score_detector/verify.php"
    enterprise: Optional[bool] = False


class GenerationConfigRequest(BaseModel):
    image_timeout: Optional[int] = None
    video_timeout: Optional[int] = None
    max_retries: Optional[int] = None


class CallLogicConfigRequest(BaseModel):
    call_mode: str


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str


class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str


class UpdateDebugConfigRequest(BaseModel):
    enabled: bool


class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: int


class ST2ATRequest(BaseModel):
    """ST转AT请求"""

    st: str


class ImportTokenItem(BaseModel):
    """导入Token项"""

    email: Optional[str] = None
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    is_active: bool = True
    captcha_proxy_url: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    protocol_mode: str = "session"
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = 120
    browser_profile_source: str = "cookies"


class ImportTokensRequest(BaseModel):
    """导入Token请求"""

    tokens: List[ImportTokenItem]


class TokenRefreshConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None


class ProfileTransferImportRequest(BaseModel):
    bundle: dict[str, Any]
    proxy_url: Optional[str] = None
    proxy_pool_id: Optional[int] = Field(default=None, ge=1, strict=True)


class ProfileImportJobRequest(BaseModel):
    bundles: List[dict[str, Any]] = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    proxy_url: Optional[str] = Field(default=None, max_length=4096)
    proxy_pool_id: Optional[int] = Field(default=None, ge=1, strict=True)
