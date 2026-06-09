from __future__ import annotations

import math
from array import array

import pytest

from voiceagent.config import WakewordConfig
from voiceagent.wakeword import (
    PrerollBuffer,
    create_wake_detector,
    frame_rms,
    is_model_path,
    validate_model_specs,
)
from voiceagent.wakeword.mock import MockWakeDetector


def _tone(samples: int, amp: int = 8000, freq: float = 300.0, rate: int = 16000) -> bytes:
    a = array("h")
    for n in range(samples):
        a.append(int(amp * math.sin(2 * math.pi * freq * n / rate)))
    return a.tobytes()


def _silence(samples: int) -> bytes:
    return b"\x00\x00" * samples


def test_frame_rms_silence_and_tone() -> None:
    assert frame_rms(_silence(512)) == 0.0
    assert frame_rms(_tone(512, amp=8000)) > 5000  # ~8000/sqrt(2)


def test_frame_rms_empty() -> None:
    assert frame_rms(b"") == 0.0


def test_preroll_keeps_last_bytes() -> None:
    buf = PrerollBuffer(max_bytes=8)
    buf.extend(b"AAAA")
    buf.extend(b"BBBB")
    buf.extend(b"CCCC")
    assert buf.snapshot() == b"BBBBCCCC"  # last 8 bytes
    buf.clear()
    assert buf.snapshot() == b""


def _mock(trigger_rms: float = 1500.0, cooldown_samples: int = 0) -> MockWakeDetector:
    return MockWakeDetector(
        rate=16000, preroll_bytes=16000, cooldown_samples=cooldown_samples,
        trigger_rms=trigger_rms,
    )


def test_mock_fires_on_loud_not_on_silence() -> None:
    det = _mock(trigger_rms=1500.0)
    assert det.process(_silence(512)) is None
    ev = det.process(_tone(512, amp=8000))
    assert ev is not None
    assert ev.model == "mock"
    assert 0.0 < ev.score <= 1.0
    assert len(ev.preroll) > 0  # pre-roll captured


def test_mock_cooldown_suppresses_repeat() -> None:
    # cooldown of 1000 samples drains over two 512-sample frames, so the 4th
    # loud frame fires again while the 2nd/3rd are suppressed.
    det = _mock(trigger_rms=1500.0, cooldown_samples=1000)
    assert det.process(_tone(512, amp=8000)) is not None  # first fires
    assert det.process(_tone(512, amp=8000)) is None      # cooldown 1000->488
    assert det.process(_tone(512, amp=8000)) is None      # cooldown 488->-24
    assert det.process(_tone(512, amp=8000)) is not None  # cooldown elapsed, fires


def test_mock_preroll_contains_recent_audio() -> None:
    det = _mock(trigger_rms=1500.0)
    quiet = _silence(512)
    det.process(quiet)  # goes into preroll
    ev = det.process(_tone(512, amp=8000))
    assert ev is not None
    # preroll holds the earlier silence + the triggering frame
    assert len(ev.preroll) >= 512 * 2


def test_factory_selects_mock() -> None:
    det = create_wake_detector(WakewordConfig(engine="mock"), capture_rate=16000)
    assert isinstance(det, MockWakeDetector)


def test_is_model_path() -> None:
    assert not is_model_path("alexa")
    assert not is_model_path("hey_jarvis")
    assert is_model_path("models/hey_panel.onnx")
    assert is_model_path("/abs/path/wake.onnx")
    assert is_model_path("relative.tflite")


def test_validate_model_specs_passes_names_and_existing_onnx(tmp_path) -> None:  # type: ignore[no-untyped-def]
    onnx = tmp_path / "hey_panel.onnx"
    onnx.write_bytes(b"\x00")  # contents irrelevant to validation
    validate_model_specs(["alexa", str(onnx)])  # no raise


def test_validate_model_specs_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        validate_model_specs(["/nope/missing.onnx"])


def test_validate_model_specs_rejects_tflite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    tfl = tmp_path / "w.tflite"
    tfl.write_bytes(b"\x00")
    with pytest.raises(ValueError, match="must be .onnx"):
        validate_model_specs([str(tfl)])


def test_reset_clears_state() -> None:
    det = _mock(trigger_rms=1500.0, cooldown_samples=99999)
    det.process(_tone(512, amp=8000))  # arms cooldown
    det.reset()
    # after reset, a loud frame fires again immediately
    assert det.process(_tone(512, amp=8000)) is not None
