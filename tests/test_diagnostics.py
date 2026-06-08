from __future__ import annotations

import pytest

from voiceagent.config import Settings
from voiceagent.diagnostics import run_audio_test, run_led_test, run_respeaker_tune


def _mock_settings(**over: object) -> Settings:
    data: dict[str, object] = {
        "audio": {"backend": "mock"},
        "respeaker": {"simulate": True},
    }
    data.update(over)
    return Settings(**data)


async def test_audio_test_runs_on_mock() -> None:
    s = _mock_settings()
    result = await run_audio_test(s, duration_s=0.05)
    assert result["backend"] == "mock"
    assert isinstance(result["captured_samples"], int)
    assert result["captured_samples"] >= int(16000 * 0.05)
    assert "mock-respeaker-xvf3800" in result["devices"]  # type: ignore[operator]


async def test_audio_test_plays_wake_cue(tmp_path: object) -> None:
    import wave
    from pathlib import Path

    p = Path(str(tmp_path)) / "wake.wav"
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x01" * 128)
    s = _mock_settings(wakeword={"wake_sound": str(p)})
    result = await run_audio_test(s, duration_s=0.02)
    assert result["played_bytes"]  # captured playback happened


async def test_led_test_cycles_all() -> None:
    s = _mock_settings()
    result = await run_led_test(s, "all")
    assert result["host"] == "MockXvfHost"
    assert "thinking" in result["shown"]  # type: ignore[operator]


async def test_led_test_single_state() -> None:
    s = _mock_settings()
    result = await run_led_test(s, "speaking")
    assert result["shown"] == ["speaking"]


async def test_respeaker_tune_applies_and_reads_back() -> None:
    s = _mock_settings(respeaker={"simulate": True, "tuning": {"PP_AGCGAIN": [1]}})
    result = await run_respeaker_tune(s)
    assert result["applied"] == ["PP_AGCGAIN"]
    assert result["readback"] == {"PP_AGCGAIN": [1.0]}


@pytest.mark.parametrize(
    "state", ["idle", "engaging", "listening", "thinking", "speaking", "error"]
)
async def test_led_all_states_valid(state: str) -> None:
    s = _mock_settings()
    result = await run_led_test(s, state)
    assert result["shown"] == [state]
