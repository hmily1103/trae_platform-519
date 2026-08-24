"""预览图压缩：避免原图 base64（常 2～4MB）撑爆浏览器 / SSE。"""
from __future__ import annotations

import base64
import io
import os
from typing import Dict, Optional, Union

from utils.logger import setup_logger

logger = setup_logger("ui_preview_image")

DEFAULT_MAX_WIDTH = 540
DEFAULT_JPEG_QUALITY = 55


def encode_preview_payload(
    source: Union[str, bytes],
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    quality: int = DEFAULT_JPEG_QUALITY,
    device_width: int = 0,
    device_height: int = 0,
) -> Optional[Dict]:
    """
    压缩预览图并返回元数据。

    返回:
      {
        "image": base64 JPEG,
        "device_width": 设备逻辑宽（点击映射用）,
        "device_height": 设备逻辑高,
        "preview_width": 预览图像素宽,
        "preview_height": 预览图像素高,
      }
    """
    try:
        from PIL import Image
    except ImportError:
        raw = _raw_base64(source)
        if not raw:
            return None
        return {
            "image": raw,
            "device_width": int(device_width or 0),
            "device_height": int(device_height or 0),
            "preview_width": int(device_width or 0),
            "preview_height": int(device_height or 0),
        }

    try:
        if isinstance(source, str):
            if not source or not os.path.exists(source):
                return None
            img = Image.open(source)
        else:
            if not source:
                return None
            img = Image.open(io.BytesIO(source))

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")

        orig_w, orig_h = img.size
        dw = int(device_width or orig_w or 0)
        dh = int(device_height or orig_h or 0)

        if max_width > 0 and orig_w > max_width:
            nh = max(1, int(orig_h * (max_width / float(orig_w))))
            # BILINEAR 比 LANCZOS 更快，预览够用
            resample = getattr(Image, "BILINEAR", None) or Image.BICUBIC
            img = img.resize((max_width, nh), resample)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        preview_w, preview_h = img.size
        return {
            "image": base64.b64encode(buf.getvalue()).decode("ascii"),
            "device_width": dw,
            "device_height": dh,
            "preview_width": preview_w,
            "preview_height": preview_h,
        }
    except Exception as e:
        logger.warning(f"预览图压缩失败，回退原图: {e}")
        raw = _raw_base64(source)
        if not raw:
            return None
        return {
            "image": raw,
            "device_width": int(device_width or 0),
            "device_height": int(device_height or 0),
            "preview_width": int(device_width or 0),
            "preview_height": int(device_height or 0),
        }


def encode_preview_base64(
    source: Union[str, bytes],
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Optional[str]:
    """兼容旧调用：只返回 base64 字符串。"""
    payload = encode_preview_payload(source, max_width=max_width, quality=quality)
    return payload.get("image") if payload else None


def _raw_base64(source: Union[str, bytes]) -> Optional[str]:
    try:
        if isinstance(source, str):
            if not source or not os.path.exists(source):
                return None
            with open(source, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
        return base64.b64encode(source).decode("ascii")
    except Exception:
        return None
