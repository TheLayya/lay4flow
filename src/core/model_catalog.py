"""Upstream model keys, request configurations, and callable legacy aliases.

This module only describes model data. Request parsing lives in public_models
and model_resolver; generation_handler owns execution and keeps the legacy
MODEL_CONFIG import available. Keep aliases and their insertion order stable
when updating upstream keys.
"""

from typing import Any, Dict, Optional


def _make_image_configs() -> Dict[str, Dict[str, Any]]:
    """Expand the shared image aspect/resolution variants in legacy order."""
    aspects = (
        ("landscape", "IMAGE_ASPECT_RATIO_LANDSCAPE"),
        ("portrait", "IMAGE_ASPECT_RATIO_PORTRAIT"),
        ("square", "IMAGE_ASPECT_RATIO_SQUARE"),
        ("four-three", "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"),
        ("three-four", "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"),
    )
    configs = {}
    for alias, upstream, supported_aspects, resolutions in (
        ("gemini-3.0-pro-image", "GEM_PIX_2", aspects, ("", "2k", "4k")),
        ("imagen-4.0-generate-preview", "IMAGEN_3_5", aspects[:2], ("",)),
        # Flow retired NARWHAL; keep published Flash aliases callable through
        # its BELUGA successor (Nano Banana 2.1), verified 2026-10-07.
        ("gemini-3.1-flash-image", "BELUGA", aspects, ("", "2k", "4k")),
    ):
        for resolution in resolutions:
            for aspect, aspect_ratio in supported_aspects:
                cfg = {"type": "image", "model_name": upstream, "aspect_ratio": aspect_ratio}
                suffix = f"-{resolution}" if resolution else ""
                if resolution:
                    cfg["upsample"] = f"UPSAMPLE_IMAGE_RESOLUTION_{resolution.upper()}"
                configs[f"{alias}-{aspect}{suffix}"] = cfg
    return configs


MODEL_CONFIG = _make_image_configs()
MODEL_CONFIG.update(
    {
        # ========== 文生视频 (T2V - Text to Video) ==========
        # 不支持上传图片，只使用文本提示词生成
        # veo_3_1_t2v_fast_portrait (竖屏)
        # 上游模型名: veo_3_1_t2v_fast_portrait
        "veo_3_1_t2v_fast_portrait": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        # veo_3_1_t2v_fast_landscape (横屏)
        # 上游模型名: veo_3_1_t2v_fast
        "veo_3_1_t2v_fast_landscape": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        # veo_3_1_t2v_fast_ultra (横竖屏)
        "veo_3_1_t2v_fast_portrait_ultra": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        "veo_3_1_t2v_fast_ultra": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        # veo_3_1_t2v_fast_ultra_relaxed (横竖屏)
        "veo_3_1_t2v_fast_portrait_ultra_relaxed": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        "veo_3_1_t2v_fast_ultra_relaxed": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_ultra_relaxed",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        # veo_3_1_t2v (横竖屏)
        "veo_3_1_t2v_portrait": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        "veo_3_1_t2v_landscape": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "use_v2_model_config": True,
        },
        # veo_3_1_t2v_lite (横竖屏，来自 labs.google.har)
        "veo_3_1_t2v_lite_portrait": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_lite",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        },
        "veo_3_1_t2v_lite_landscape": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_lite",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        },
        # ========== 首尾帧模型 (I2V - Image to Video) ==========
        # 支持1-2张图片：1张作为首帧，2张作为首尾帧
        # veo_3_1_i2v_s_fast_fl (需要新增横竖屏)
        "veo_3_1_i2v_s_fast_portrait_fl": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_portrait_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        "veo_3_1_i2v_s_fast_fl": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        # veo_3_1_i2v_s_fast_ultra (横竖屏)
        "veo_3_1_i2v_s_fast_portrait_ultra_fl": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        "veo_3_1_i2v_s_fast_ultra_fl": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        # veo_3_1_i2v_s_fast_ultra_relaxed (需要新增横竖屏)
        "veo_3_1_i2v_s_fast_portrait_ultra_relaxed": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        "veo_3_1_i2v_s_fast_ultra_relaxed": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_ultra_relaxed",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        # veo_3_1_i2v_s (需要新增横竖屏)
        "veo_3_1_i2v_s_portrait": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        "veo_3_1_i2v_s_landscape": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
        },
        # veo_3_1_i2v_lite (横竖屏，仅首帧，来自 labs.google.har)
        "veo_3_1_i2v_lite_portrait": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_lite",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 1,
            "max_images": 1,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        },
        "veo_3_1_i2v_lite_landscape": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_lite",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 1,
            "max_images": 1,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        },
        # veo_3_1_interpolation_lite (横竖屏，首尾帧，来自 labs.google.har)
        "veo_3_1_interpolation_lite_portrait": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_interpolation_lite",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 2,
            "max_images": 2,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        },
        "veo_3_1_interpolation_lite_landscape": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_interpolation_lite",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 2,
            "max_images": 2,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        },
        # ========== 多图生成 (R2V - Reference Images to Video) ==========
        # 当前上游协议最多支持 3 张参考图
        # veo_3_1_r2v_fast (横竖屏)
        "veo_3_1_r2v_fast_portrait": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_portrait",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
        },
        "veo_3_1_r2v_fast": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_landscape",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
        },
        # veo_3_1_r2v_fast_ultra (横竖屏)
        "veo_3_1_r2v_fast_portrait_ultra": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_portrait_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
        },
        "veo_3_1_r2v_fast_ultra": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_landscape_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
        },
        # veo_3_1_r2v_fast_ultra_relaxed (横竖屏)
        "veo_3_1_r2v_fast_portrait_ultra_relaxed": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
        },
        "veo_3_1_r2v_fast_ultra_relaxed": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_landscape_ultra_relaxed",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
        },
        # ========== 视频放大 (Video Upsampler) ==========
        # 先生成基础视频再放大；Omni 720p 也复用这些上采样模型。
        # T2V 4K 放大版
        "veo_3_1_t2v_fast_portrait_4k": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        "veo_3_1_t2v_fast_4k": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        "veo_3_1_t2v_fast_portrait_ultra_4k": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        "veo_3_1_t2v_fast_ultra_4k": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        # T2V 1080P 放大版
        "veo_3_1_t2v_fast_portrait_1080p": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        "veo_3_1_t2v_fast_1080p": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        "veo_3_1_t2v_fast_portrait_ultra_1080p": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_portrait_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        "veo_3_1_t2v_fast_ultra_1080p": {
            "type": "video",
            "video_type": "t2v",
            "model_key": "veo_3_1_t2v_fast_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        # I2V 4K 放大版
        "veo_3_1_i2v_s_fast_portrait_ultra_fl_4k": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        "veo_3_1_i2v_s_fast_ultra_fl_4k": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        # I2V 1080P 放大版
        "veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        "veo_3_1_i2v_s_fast_ultra_fl_1080p": {
            "type": "video",
            "video_type": "i2v",
            "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 1,
            "max_images": 2,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        # R2V 4K 放大版
        "veo_3_1_r2v_fast_portrait_ultra_4k": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_portrait_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        "veo_3_1_r2v_fast_ultra_4k": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_landscape_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
            "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"},
        },
        # R2V 1080P 放大版
        "veo_3_1_r2v_fast_portrait_ultra_1080p": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_portrait_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        "veo_3_1_r2v_fast_ultra_1080p": {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_fast_landscape_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        },
        # ========== 视频续写 (Extend - Video Continuation) ==========
        # 基于已生成的视频续写7秒，最多续写20次（最长148秒）
        # 需要提供源视频的 mediaGenerationId
        # VEO 3.1 Extend (横竖屏)
        "veo_3_1_extend_portrait": {
            "type": "video",
            "video_type": "extend",
            "model_key": "veo_3_1_extend_fast_portrait_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": False,
            "requires_video_id": True,
        },
        "veo_3_1_extend": {
            "type": "video",
            "video_type": "extend",
            "model_key": "veo_3_1_extend_fast_ultra",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": False,
            "requires_video_id": True,
        },
        # ========== Gemini Omni Flash ==========
        # 2026-05-26 实测上游真实请求：
        # - 纯文本 -> video:batchAsyncGenerateVideoText, videoModelKey=abra_t2v_8s
        # - 参考图 -> video:batchAsyncGenerateVideoReferenceImages, videoModelKey=abra_r2v_8s
        "omni": {
            "type": "video",
            "video_type": "omni",
            "model_key": "abra_t2v_8s",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "supports_images": True,
            "min_images": 0,
            "max_images": 7,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
            "reference_model_key": "abra_r2v_8s",
            "reference_duration": 8,
            "reference_model_display_name": "Omni Flash",
        },
        "omni_portrait": {
            "type": "video",
            "video_type": "omni",
            "model_key": "abra_t2v_8s",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "supports_images": True,
            "min_images": 0,
            "max_images": 7,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
            "reference_model_key": "abra_r2v_8s",
            "reference_duration": 8,
            "reference_model_display_name": "Omni Flash",
        },
    }
)


def _apply_omni_model_updates() -> None:
    """Keys observed in Flow GetModels on 2026-09-25; duration/resolution are in the key."""
    for seconds in (4, 6, 8, 10):
        for resolution_suffix in ("", "_360p"):
            for alias_base, video_type, upstream_prefix, frame_suffix, min_images, max_images in (
                ("omni", "t2v", "abra_t2v", "", 0, 0),
                ("omni_r2v", "r2v", "abra_r2v", "", 1, 7),
                ("omni_i2v", "i2v", "omni_flash_i2v", "_first_last", 2, 2),
            ):
                suffix = f"_{seconds}s{frame_suffix}{resolution_suffix}"
                for orientation_suffix, aspect_ratio in (
                    ("", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
                    ("_portrait", "VIDEO_ASPECT_RATIO_PORTRAIT"),
                ):
                    MODEL_CONFIG[f"{alias_base}{orientation_suffix}{suffix}"] = {
                        "type": "video",
                        "video_type": video_type,
                        "model_key": f"{upstream_prefix}{suffix}",
                        "aspect_ratio": aspect_ratio,
                        "supports_images": max_images > 0,
                        "min_images": min_images,
                        "max_images": max_images,
                        "use_v2_model_config": True,
                        "allow_tier_upgrade": False,
                    }

            # Single first-frame I2V keys are distinct from the first+last-frame keys.
            suffix = f"_{seconds}s{resolution_suffix}"
            for orientation_suffix, aspect_ratio in (
                ("", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
                ("_portrait", "VIDEO_ASPECT_RATIO_PORTRAIT"),
            ):
                MODEL_CONFIG[f"omni_i2v{orientation_suffix}{suffix}"] = {
                    "type": "video",
                    "video_type": "i2v",
                    "model_key": f"abra_i2v{suffix}",
                    "aspect_ratio": aspect_ratio,
                    "supports_images": True,
                    "min_images": 1,
                    "max_images": 1,
                    "use_v2_model_config": True,
                    "allow_tier_upgrade": False,
                }

    for resolution_suffix in ("", "_360p"):
        for orientation_suffix, aspect_ratio in (
            ("", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
            ("_portrait", "VIDEO_ASPECT_RATIO_PORTRAIT"),
        ):
            model_suffix = f"{orientation_suffix}{resolution_suffix}"
            MODEL_CONFIG[f"omni_edit{model_suffix}"] = {
                "type": "video",
                "video_type": "edit",
                "model_key": f"abra_edit{resolution_suffix}",
                "aspect_ratio": aspect_ratio,
                "supports_images": True,
                "supports_video": True,
                "min_images": 0,
                "max_images": 5,
                "use_v2_model_config": True,
                "allow_tier_upgrade": False,
            }

    # Omni's 1080p/4K outputs use its native 720p generation followed by
    # the shared Veo upsamplers. Keep native 360p requests at 360p.
    for model_id, model_config in list(MODEL_CONFIG.items()):
        if model_id.startswith("omni") and not model_id.endswith("_360p"):
            for resolution in ("1080p", "4k"):
                MODEL_CONFIG[f"{model_id}_{resolution}"] = {
                    **model_config,
                    "upsample": {
                        "resolution": f"VIDEO_RESOLUTION_{resolution.upper()}",
                        "model_key": f"veo_3_1_upsampler_{resolution}",
                    },
                }


_apply_omni_model_updates()


for _aspect in ("landscape", "portrait", "square", "four-three", "three-four"):
    MODEL_CONFIG[f"gemini-3.1-flash-image-lite-{_aspect}"] = {
        **MODEL_CONFIG[f"gemini-3.1-flash-image-{_aspect}"],
        "model_name": "HARBOR_SEAL",
    }


def _make_t2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "t2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": False,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _make_i2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    min_images: int = 1,
    max_images: int = 2,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "i2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": True,
        "min_images": min_images,
        "max_images": max_images,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _apply_veo_3_1_model_updates():
    """Keep the public aliases aligned with the current Veo 3.1 model families."""
    landscape = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    portrait = "VIDEO_ASPECT_RATIO_PORTRAIT"

    for orientation, aspect_ratio in (("landscape", landscape), ("portrait", portrait)):
        MODEL_CONFIG[f"veo_3_1_r2v_lite_{orientation}"] = {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_lite",
            "aspect_ratio": aspect_ratio,
            "supports_images": True,
            "min_images": 0,
            "max_images": 3,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        }

    def add_alias(alias: str, target: str):
        MODEL_CONFIG[alias] = dict(MODEL_CONFIG[target])

    def add_default_duration_aliases(
        base_alias: str,
        landscape_target: str,
        portrait_target: str,
        *,
        fl_suffix: bool = False,
    ):
        if fl_suffix:
            add_alias(f"{base_alias}_8s_fl", landscape_target)
            add_alias(f"{base_alias}_portrait_8s_fl", portrait_target)
            add_alias(f"{base_alias}_landscape_8s_fl", landscape_target)
            return

        add_alias(f"{base_alias}_8s", landscape_target)
        add_alias(f"{base_alias}_portrait_8s", portrait_target)
        add_alias(f"{base_alias}_landscape_8s", landscape_target)

    def add_default_duration_upsample_aliases(
        base_alias: str,
        resolution_name: str,
        landscape_target: str,
        portrait_target: str,
    ):
        add_alias(f"{base_alias}_8s_{resolution_name}", landscape_target)
        add_alias(f"{base_alias}_portrait_8s_{resolution_name}", portrait_target)
        add_alias(f"{base_alias}_landscape_8s_{resolution_name}", landscape_target)

    # Non-fast/non-lite Veo 3.1 aliases must call Quality upstream keys.
    MODEL_CONFIG["veo_3_1_t2v_landscape"].update({"model_key": "veo_3_1_t2v"})
    MODEL_CONFIG["veo_3_1_t2v_portrait"].update({"model_key": "veo_3_1_t2v_portrait"})
    MODEL_CONFIG["veo_3_1_i2v_s_landscape"].update({"model_key": "veo_3_1_i2v_s_fl"})
    MODEL_CONFIG["veo_3_1_i2v_s_portrait"].update({"model_key": "veo_3_1_i2v_s_portrait_fl"})
    MODEL_CONFIG["veo_3_1_extend"].update({"model_key": "veo_3_1_extend_landscape"})
    MODEL_CONFIG["veo_3_1_extend_portrait"].update({"model_key": "veo_3_1_extend_portrait"})

    for orientation, aspect_ratio in (("landscape", landscape), ("portrait", portrait)):
        MODEL_CONFIG[f"veo_3_1_extend_fast_{orientation}"] = {
            "type": "video",
            "video_type": "extend",
            "model_key": f"veo_3_1_extend_fast_{orientation}",
            "aspect_ratio": aspect_ratio,
            "supports_images": False,
            "requires_video_id": True,
        }
        MODEL_CONFIG[f"veo_3_1_extend_fast_{orientation}_ultra"] = {
            **MODEL_CONFIG[f"veo_3_1_extend_fast_{orientation}"],
            "model_key": f"veo_3_1_extend_fast_{orientation}_ultra",
        }
        MODEL_CONFIG[f"veo_3_1_extension_lite_{orientation}"] = {
            "type": "video",
            "video_type": "extend",
            "model_key": "veo_3_1_extension_lite",
            "aspect_ratio": aspect_ratio,
            "supports_images": False,
            "requires_video_id": True,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        }

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # T2V duration variants.
        MODEL_CONFIG[f"veo_3_1_t2v_fast_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_fast_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", portrait
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_landscape"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            landscape,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_portrait"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            portrait,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", portrait
        )

        # I2V duration variants. FL keys are used for 2 images; the single-image path strips "_fl".
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_portrait_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", portrait
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            landscape,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            portrait,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            landscape,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            portrait,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait
        )

        for resolution_name, resolution, upsampler_model_key in (
            ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
            ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
        ):
            upsample = {"resolution": resolution, "model_key": upsampler_model_key}
            MODEL_CONFIG[f"veo_3_1_t2v_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", portrait, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait, upsample=upsample
            )

    for resolution_name, resolution, upsampler_model_key in (
        ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
        ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
    ):
        upsample = {"resolution": resolution, "model_key": upsampler_model_key}
        MODEL_CONFIG[f"veo_3_1_t2v_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v_portrait", portrait, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_fl", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_portrait_fl", portrait, upsample=upsample
        )

    # Lite generates native 720p first; 1080p uses the shared Veo upsampler.
    for model_id, model_config in list(MODEL_CONFIG.items()):
        if "lite" not in model_id or model_config.get("type") != "video":
            continue
        if "upsample" in model_config or model_id.endswith("_1080p"):
            continue
        MODEL_CONFIG[f"{model_id}_1080p"] = {
            **model_config,
            "upsample": {
                "resolution": "VIDEO_RESOLUTION_1080P",
                "model_key": "veo_3_1_upsampler_1080p",
            },
        }

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # Explicit landscape names for /v1/models; short landscape names remain compatible.
        add_alias(f"veo_3_1_t2v_fast_landscape_{suffix}", f"veo_3_1_t2v_fast_{suffix}")
        add_alias(f"veo_3_1_t2v_landscape_{suffix}", f"veo_3_1_t2v_{suffix}")
        add_alias(f"veo_3_1_i2v_s_fast_landscape_{suffix}_fl", f"veo_3_1_i2v_s_fast_{suffix}_fl")
        add_alias(f"veo_3_1_i2v_s_landscape_{suffix}", f"veo_3_1_i2v_s_{suffix}")

        add_alias(f"veo_3_1_t2v_lite_landscape_{suffix}", f"veo_3_1_t2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_t2v_lite_portrait_{suffix}", f"veo_3_1_t2v_lite_{suffix}_portrait")
        add_alias(f"veo_3_1_i2v_lite_landscape_{suffix}", f"veo_3_1_i2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_i2v_lite_portrait_{suffix}", f"veo_3_1_i2v_lite_{suffix}_portrait")
        add_alias(
            f"veo_3_1_interpolation_lite_landscape_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_landscape",
        )
        add_alias(
            f"veo_3_1_interpolation_lite_portrait_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_portrait",
        )

        for resolution_name in ("4k", "1080p"):
            add_alias(
                f"veo_3_1_t2v_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_t2v_{suffix}_{resolution_name}",
            )
            add_alias(
                f"veo_3_1_i2v_s_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_i2v_s_{suffix}_{resolution_name}",
            )

    for resolution_name in ("4k", "1080p"):
        add_alias(f"veo_3_1_t2v_landscape_{resolution_name}", f"veo_3_1_t2v_{resolution_name}")
        add_alias(f"veo_3_1_i2v_s_landscape_{resolution_name}", f"veo_3_1_i2v_s_{resolution_name}")

    add_alias("veo_3_1_r2v_fast_landscape", "veo_3_1_r2v_fast")
    add_alias("veo_3_1_r2v_fast_landscape_ultra", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_relaxed", "veo_3_1_r2v_fast_ultra_relaxed")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_4k", "veo_3_1_r2v_fast_ultra_4k")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_1080p", "veo_3_1_r2v_fast_ultra_1080p")

    add_default_duration_aliases(
        "veo_3_1_t2v_fast",
        "veo_3_1_t2v_fast_landscape",
        "veo_3_1_t2v_fast_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_t2v",
        "veo_3_1_t2v_landscape",
        "veo_3_1_t2v_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_i2v_s_fast",
        "veo_3_1_i2v_s_fast_fl",
        "veo_3_1_i2v_s_fast_portrait_fl",
        fl_suffix=True,
    )
    add_default_duration_aliases(
        "veo_3_1_i2v_s",
        "veo_3_1_i2v_s_landscape",
        "veo_3_1_i2v_s_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_r2v_fast",
        "veo_3_1_r2v_fast",
        "veo_3_1_r2v_fast_portrait",
    )
    add_alias("veo_3_1_r2v_fast_ultra_8s", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_portrait_ultra_8s", "veo_3_1_r2v_fast_portrait_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_8s", "veo_3_1_r2v_fast_ultra")
    add_alias(
        "veo_3_1_r2v_fast_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_ultra_relaxed",
    )
    add_alias(
        "veo_3_1_r2v_fast_portrait_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_portrait_ultra_relaxed",
    )
    add_alias(
        "veo_3_1_r2v_fast_landscape_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_ultra_relaxed",
    )

    add_alias("veo_3_1_t2v_lite_8s_landscape", "veo_3_1_t2v_lite_landscape")
    add_alias("veo_3_1_t2v_lite_8s_portrait", "veo_3_1_t2v_lite_portrait")
    add_alias("veo_3_1_t2v_lite_landscape_8s", "veo_3_1_t2v_lite_landscape")
    add_alias("veo_3_1_t2v_lite_portrait_8s", "veo_3_1_t2v_lite_portrait")
    add_alias("veo_3_1_i2v_lite_8s_landscape", "veo_3_1_i2v_lite_landscape")
    add_alias("veo_3_1_i2v_lite_8s_portrait", "veo_3_1_i2v_lite_portrait")
    add_alias("veo_3_1_i2v_lite_landscape_8s", "veo_3_1_i2v_lite_landscape")
    add_alias("veo_3_1_i2v_lite_portrait_8s", "veo_3_1_i2v_lite_portrait")
    add_alias(
        "veo_3_1_interpolation_lite_8s_landscape",
        "veo_3_1_interpolation_lite_landscape",
    )
    add_alias(
        "veo_3_1_interpolation_lite_8s_portrait",
        "veo_3_1_interpolation_lite_portrait",
    )
    add_alias(
        "veo_3_1_interpolation_lite_landscape_8s",
        "veo_3_1_interpolation_lite_landscape",
    )
    add_alias(
        "veo_3_1_interpolation_lite_portrait_8s",
        "veo_3_1_interpolation_lite_portrait",
    )

    # Public resolution names place the orientation before the duration.
    for base in (
        "veo_3_1_t2v_lite",
        "veo_3_1_i2v_lite",
        "veo_3_1_interpolation_lite",
    ):
        for orientation in ("landscape", "portrait"):
            for seconds in (4, 6, 8):
                duration = "" if seconds == 8 else f"_{seconds}s"
                source = f"{base}{duration}_{orientation}_1080p"
                add_alias(f"{base}_{orientation}_{seconds}s_1080p", source)

    for orientation in ("landscape", "portrait"):
        for base in ("veo_3_1_r2v_lite", "veo_3_1_extension_lite"):
            source = f"{base}_{orientation}_1080p"
            if source in MODEL_CONFIG:
                add_alias(f"{base}_{orientation}_1080p", source)

    for resolution_name in ("4k", "1080p"):
        add_default_duration_upsample_aliases(
            "veo_3_1_t2v",
            resolution_name,
            f"veo_3_1_t2v_{resolution_name}",
            f"veo_3_1_t2v_portrait_{resolution_name}",
        )
        add_default_duration_upsample_aliases(
            "veo_3_1_i2v_s",
            resolution_name,
            f"veo_3_1_i2v_s_{resolution_name}",
            f"veo_3_1_i2v_s_portrait_{resolution_name}",
        )


_apply_veo_3_1_model_updates()
