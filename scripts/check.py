"""One offline check entry point for Windows, Linux and CI."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = [
    "src/core/version.py",
    "src/core/update_guard.py",
    "tests/test_update_guard.py",
    "tests/test_release_metadata.py",
    "tests/test_update_runtime.py",
    "tests/test_update_shell.py",
    "tests/test_windows_installer.py",
    "src/core/auth.py",
    "tests/test_default_credentials.py",
    "src/main.py",
    "src/api/openai_common.py",
    "src/api/openai_images.py",
    "src/api/openai_videos.py",
    "src/api/vids.py",
    "src/api/vids_videos.py",
    "src/api/vids_media.py",
    "src/api/vids_schemas.py",
    "src/api/compat.py",
    "src/api/compat_images.py",
    "src/api/compat_videos.py",
    "src/api/compat_schemas.py",
    "src/core/media_skus.py",
    "src/services/video_jobs.py",
    "src/services/model_gateway.py",
    "src/services/__init__.py",
    "src/core/model_catalog.py",
    "src/core/observability.py",
    "src/api/admin_schemas.py",
    "src/api/admin.py",
    "src/api/admin_routes",
    "src/core/storage",
    "src/services/factory.py",
    "src/services/studio_jobs.py",
    "tests/test_studio_jobs.py",
    "src/services/flow",
    "src/services/vids",
    "src/services/browser",
    "src/services/cache",
    "scripts",
    "tests/test_model_catalog.py",
    "tests/test_flow_protocol.py",
    "tests/test_vids_client.py",
    "tests/test_vids_document.py",
    "tests/test_vids_document_gateway.py",
    "tests/test_vids_image_edit.py",
    "tests/test_vids_image_input.py",
    "tests/test_vids_video_request.py",
    "tests/test_vids_video_input.py",
    "tests/test_vids_video_gateway.py",
    "tests/test_vids_gateway.py",
    "tests/test_vids_image_outputs.py",
    "tests/test_vids_speech_api.py",
    "tests/test_vids_images_api.py",
    "tests/test_vids_videos_api.py",
    "tests/test_vids_profile_cookies.py",
    "tests/test_vids_session_store.py",
    "tests/test_vids_persistence_gateway.py",
    "tests/test_vids_dispatcher.py",
    "tests/test_vids_admission.py",
    "tests/test_vids_runtime.py",
    "tests/test_vids_media_api.py",
    "tests/test_vids_console.py",
    "tests/test_vids_admin_management.py",
    "tests/test_vids_console_media.py",
    "tests/test_vids_document_sources.py",
    "tests/test_vids_onboarding.py",
    "tests/test_vids_auto_onboarding.py",
    "tests/test_vids_generation_preflight.py",
    "tests/test_vids_openapi.py",
    "tests/test_flow_contracts.py",
    "tests/test_service_factory.py",
    "tests/test_model_gateway.py",
    "tests/test_openai_responses.py",
    "tests/test_model_endpoint_contract.py",
    "tests/test_public_openapi_metadata.py",
    "tests/test_upstream_compatibility.py",
    "tests/test_admin_schemas.py",
    "tests/test_admin_log_routes.py",
    "tests/test_admin_account_config_routes.py",
    "tests/test_admin_remaining_routes.py",
    "tests/test_admin_captcha_config.py",
    "tests/test_database_schema.py",
    "tests/test_database_settings.py",
    "tests/test_database_accounts.py",
    "tests/test_generation_config_max_retries.py",
    "tests/test_static_model_catalog.py",
    "tests/test_generation_history_storage.py",
    "tests/test_flow_media.py",
    "tests/test_flow_retry.py",
    "tests/test_release_package.py",
    "tests/test_frontend_routes.py",
    "tests/test_project_pool.py",
    "tests/test_proxy_urls.py",
    "tests/test_cache_rules.py",
    "tests/test_observability.py",
    "tests/test_monitoring.py",
    "tests/test_load_balancer_metrics.py",
    "tests/test_generation_concurrency.py",
    "tests/test_flow_proxy_policy.py",
    "tests/test_flow_session_rotation.py",
    "tests/test_google_cookie_jar.py",
    "tests/test_refresh_singleflight.py",
    "tests/test_concurrency_manager.py",
    "tests/test_openai_images.py",
    "tests/test_image_pipeline_integration.py",
    "tests/test_openai_videos.py",
    "tests/test_openai_sdk_contract.py",
    "tests/test_media_skus.py",
    "tests/test_sku_fallback.py",
    "tests/test_media_sku_api.py",
    "tests/test_compat_images.py",
    "tests/test_compat_videos.py",
    "tests/test_business_docs.py",
]
COMPONENTS = [path for path in COMPONENTS if (ROOT / path).exists()]


def run(*command: str) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lint-only", action="store_true")
    args = parser.parse_args()
    try:
        run(sys.executable, "-m", "ruff", "check", "src", "tests", "scripts", "main.py")
        run(sys.executable, "-m", "ruff", "check", "--select", "E4,E7,E9,F,I", *COMPONENTS)
        run(sys.executable, "-m", "ruff", "format", "--check", *COMPONENTS)
        run(sys.executable, "scripts/generate_model_fallback.py", "--check")
        run(sys.executable, "scripts/generate_sku_fallback.py", "--check")
        if not args.lint_only:
            node = shutil.which("node")
            if node is None:
                parser.error("Node.js is required for the UI checks")
            run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test*.py")
            for test in sorted((ROOT / "tests").glob("test_*.cjs")):
                run(node, str(test.relative_to(ROOT)))
            run(sys.executable, "scripts/smoke_package.py")
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
