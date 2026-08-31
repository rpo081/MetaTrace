"""CLIP image embeddings (open_clip) with PSD-aware image decoding."""
from __future__ import annotations

import io
import logging
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps
from PIL.Image import Image as PILImage

log = logging.getLogger(__name__)

_load_lock = threading.Lock()
_state: dict = {}
_pil_limits_initialized = False

# Decompression-bomb guard for the psd-tools fallback. Pillow's own
# MAX_IMAGE_PIXELS check covers Image.open() but NOT psd-tools' composite()
# rendering, which allocates the full merged canvas in RAM.
DEFAULT_MAX_PIXELS = 100_000_000             # hard cap: 100 Mpixel ≈ 1.2 GiB RGB
PILLOW_WARN_PIXELS = DEFAULT_MAX_PIXELS  # ~89.5 Mpixel (Pillow default warn) — updated after init
MAX_PIXELS_ENV = "METATRACE_MAX_PIXELS"


def _ensure_pil_limits() -> None:
    """Idempotently apply PIL global limits (avoid import-time side-effects).

    Previously these were set at import, polluting every PIL consumer in the
    process. Now applied lazily on first decode/model load, guarded for threads.
    """
    global _pil_limits_initialized, PILLOW_WARN_PIXELS
    if _pil_limits_initialized:
        return
    with _load_lock:
        if _pil_limits_initialized:
            return
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        Image.MAX_IMAGE_PIXELS = DEFAULT_MAX_PIXELS
        PILLOW_WARN_PIXELS = Image.MAX_IMAGE_PIXELS
        _pil_limits_initialized = True


def _max_psd_pixels() -> int:
    """Hard pixel cap for PSD composites (env METATRACE_MAX_PIXELS, default 1e8)."""
    raw = os.environ.get(MAX_PIXELS_ENV)
    if not raw:
        return DEFAULT_MAX_PIXELS
    try:
        val = int(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %d pixels",
                    MAX_PIXELS_ENV, raw, DEFAULT_MAX_PIXELS)
        return DEFAULT_MAX_PIXELS
    if val < 1:
        log.warning("%s=%r must be >= 1; using default %d pixels",
                    MAX_PIXELS_ENV, raw, DEFAULT_MAX_PIXELS)
        return DEFAULT_MAX_PIXELS
    return val


def select_device(preferred: str, torch) -> str:
    """Resolve the embedding device: 'auto' autodetects cuda > mps > cpu."""
    preferred = (preferred or "auto").strip().lower()
    if preferred in ("cuda", "mps", "cpu"):
        return preferred
    if preferred != "auto":
        log.warning("invalid device %r; using auto detection", preferred)
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def get_model(settings):
    """Lazy-load the CLIP model once. Returns (model, preprocess, device)."""
    _ensure_pil_limits()
    key = (settings.model_name, settings.model_pretrained)
    with _load_lock:
        cached = _state.get(key)
        if cached is not None:
            return cached["model"], cached["preprocess"], cached["device"]
        import open_clip
        import torch

        # NOTE: on macOS, loading a model on MPS after faiss has been
        # imported segfaults (OpenMP runtime conflict). METATRACE_DEVICE=cpu
        # is the workaround; see README.
        device = select_device(getattr(settings, "device", "auto"), torch)
        log.info("loading CLIP model %s (%s) on %s", *key, device)
        if device == "cuda":
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        model, _, preprocess = open_clip.create_model_and_transforms(
            settings.model_name,
            pretrained=settings.model_pretrained,
            device=device,
        )
        model.eval()
        _state[key] = {"model": model, "preprocess": preprocess, "device": device}
        # Keep legacy single-key entry for backwards-compat introspection
        _state["key"] = key
        _state["model"] = model
        _state["preprocess"] = preprocess
        _state["device"] = device
        return model, preprocess, device


def decode_image(source: Path | bytes) -> PILImage:
    """Decode JPG/PNG/PSD (merged composite) to an RGB PIL image.

    Pillow is tried first; PSD files that Pillow cannot read fall back to a
    psd-tools composite rendering. Enforces a 100 Mpixel hard cap to block
    decompression bombs (tiny file claiming huge dimensions).
    """
    _ensure_pil_limits()
    try:
        fh = io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb")  # noqa: SIM115
        try:
            img = Image.open(fh)
            # Pillow sets DecompressionBombError only as a warning by default;
            # we enforce aggressively via MAX_IMAGE_PIXELS above, plus an explicit
            # check after transpose before allocating the RGB buffer.
            img = ImageOps.exif_transpose(img)
            w, h = img.size
            if w * h > Image.MAX_IMAGE_PIXELS:
                raise ValueError(f"image {w}x{h} ({w*h} px) exceeds {Image.MAX_IMAGE_PIXELS} px limit")
            return img.convert("RGB")
        finally:
            if isinstance(source, bytes):
                fh.close()
            else:
                try:
                    fh.close()
                except Exception:
                    pass
    except Exception as exc:
        is_psd_path = not isinstance(source, bytes) and Path(source).suffix.lower() == ".psd"
        if not is_psd_path:
            raise
        log.info("Pillow failed on %s (%s); trying psd-tools", source, exc)
        from psd_tools import PSDImage

        fh = io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb")  # noqa: SIM115
        try:
            psd = PSDImage.open(fh)
            # Guard BEFORE composite(): rendering allocates width*height*3+
            # bytes regardless of file size, so a tiny crafted PSD can be a
            # decompression bomb. Same failure path as any other decode error
            # (counted as failed in scans / HTTP 400 for uploads).
            width, height = int(psd.width), int(psd.height)
            pixels = width * height
            max_pixels = _max_psd_pixels()
            if pixels > max_pixels:
                raise ValueError(
                    f"PSD composite {width}x{height} ({pixels} px) exceeds "
                    f"{max_pixels} px limit ({MAX_PIXELS_ENV})"
                ) from exc
            if pixels > PILLOW_WARN_PIXELS:
                log.warning(
                    "large PSD %s: %dx%d (%.1f Mpixel) above Pillow's warn "
                    "threshold, under the %d px hard cap",
                    source, width, height, pixels / 1e6, max_pixels,
                )
            composite = psd.composite()
        finally:
            fh.close()
        if composite is None:
            raise ValueError(f"PSD has no composite layer: {source}") from exc
        return composite.convert("RGB")


def embed_images(images: list[PILImage], settings) -> np.ndarray:
    """L2-normalised CLIP embeddings, shape (len(images), dim), float32."""
    import torch

    model, preprocess, device = get_model(settings)
    tensors = torch.stack([preprocess(img) for img in images]).to(device)
    with torch.inference_mode():
        if device == "cuda":
            with torch.autocast("cuda", dtype=torch.float16):
                features = model.encode_image(tensors)
            features = features.float()
        else:
            features = model.encode_image(tensors)
        features = features.cpu().numpy()
    norms = np.linalg.norm(features, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (features / norms).astype(np.float32)
