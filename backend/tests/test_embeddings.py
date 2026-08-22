"""PSD fallback tests: decompression-bomb guard via a fake PSDImage."""
import pytest
from PIL import Image

from backend.app import embeddings


class _FakePSD:
    """PSDImage stand-in: records whether composite() was attempted."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.composite_calls = 0

    def composite(self):
        self.composite_calls += 1
        return Image.new("RGB", (4, 4))


def _install_fake_psd(monkeypatch, psd: _FakePSD) -> None:
    """decode_image imports `from psd_tools import PSDImage` lazily, so patch
    the attribute on the module itself."""
    import psd_tools

    class _Module:
        @staticmethod
        def open(fh):  # signature-compatible; content is irrelevant to the fake
            return psd

    monkeypatch.setattr(psd_tools, "PSDImage", _Module)


def _psd_path(tmp_path) -> "object":
    """A .psd-suffixed file Pillow cannot decode -> forces the psd-tools branch."""
    p = tmp_path / "bomb.psd"
    p.write_bytes(b"not a real psd")
    return p


def test_psd_over_hard_cap_rejected_before_composite(tmp_path, monkeypatch):
    psd = _FakePSD(20_000, 20_000)  # 400 Mpixel > default 100 Mpixel cap
    _install_fake_psd(monkeypatch, psd)

    with pytest.raises(ValueError, match="exceeds"):
        embeddings.decode_image(_psd_path(tmp_path))
    assert psd.composite_calls == 0  # never allocated the composite canvas


def test_psd_under_cap_composites_normally(tmp_path, monkeypatch):
    psd = _FakePSD(1000, 1000)
    _install_fake_psd(monkeypatch, psd)

    img = embeddings.decode_image(_psd_path(tmp_path))
    assert img.mode == "RGB" and img.size == (4, 4)
    assert psd.composite_calls == 1


def test_psd_cap_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("METATRACE_MAX_PIXELS", "1")
    psd = _FakePSD(2, 2)  # 4 px > cap of 1
    _install_fake_psd(monkeypatch, psd)

    with pytest.raises(ValueError, match="METATRACE_MAX_PIXELS"):
        embeddings.decode_image(_psd_path(tmp_path))
    assert psd.composite_calls == 0


def test_psd_cap_env_invalid_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("METATRACE_MAX_PIXELS", "not-a-number")
    psd = _FakePSD(10, 10)
    _install_fake_psd(monkeypatch, psd)

    img = embeddings.decode_image(_psd_path(tmp_path))  # default cap applies
    assert img.size == (4, 4)


def test_decode_failure_of_non_psd_still_raises(tmp_path):
    p = tmp_path / "broken.png"
    p.write_bytes(b"not an image")
    with pytest.raises(Exception):
        embeddings.decode_image(p)


class _FakeTorch:
    """Minimal torch stand-in for device autodetection tests."""

    def __init__(self, cuda=False, mps=False):
        self._cuda = cuda
        self.backends = type("B", (), {})()
        if mps:
            self.backends.mps = type("M", (), {"is_available": staticmethod(lambda: True)})
        else:
            self.backends.mps = None

    class cuda:
        @staticmethod
        def is_available():
            return _FakeTorch._current._cuda


_FakeTorch._current = None


def test_select_device_explicit_overrides_autodetect():
    torch = _FakeTorch(cuda=True, mps=True)
    assert embeddings.select_device("cpu", torch) == "cpu"
    assert embeddings.select_device("CUDA", torch) == "cuda"
    assert embeddings.select_device(" mps ", torch) == "mps"


def test_select_device_auto_prefers_cuda_then_mps_then_cpu():
    _FakeTorch._current = _FakeTorch(cuda=True)
    try:
        assert embeddings.select_device("auto", _FakeTorch._current) == "cuda"
        _FakeTorch._current = _FakeTorch(mps=True)
        assert embeddings.select_device(None, _FakeTorch._current) == "mps"
        _FakeTorch._current = _FakeTorch()
        assert embeddings.select_device("auto", _FakeTorch._current) == "cpu"
    finally:
        _FakeTorch._current = None


def test_select_device_invalid_falls_back_to_autodetect():
    _FakeTorch._current = _FakeTorch(mps=True)
    try:
        assert embeddings.select_device("tpu", _FakeTorch._current) == "mps"
    finally:
        _FakeTorch._current = None
