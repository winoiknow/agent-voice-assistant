"""Real audio backend using sounddevice (PortAudio) + wpctl/pactl for ducking.

Only imported when a non-mock backend is selected, so the ``sounddevice`` dependency
(the ``audio`` extra) is not needed for development or CI. Capture runs in PortAudio's
callback thread and is bridged to asyncio via a queue; playback uses blocking writes
on a worker thread so the event loop is never stalled.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from array import array
from collections.abc import AsyncIterator
from typing import Any

from voiceagent.audio.base import AudioIO
from voiceagent.audio.types import AudioDevice, AudioFormat
from voiceagent.logging_setup import get_logger

log = get_logger("audio.sounddevice")


class SounddeviceAudioIO(AudioIO):
    def __init__(
        self,
        capture_format: AudioFormat,
        playback_format: AudioFormat,
        *,
        frame_samples: int = 512,
        capture_device: str | int | None = None,
        playback_device: str | int | None = None,
        music_target: str | None = None,
        capture_channels: int = 1,
        capture_pick_channel: int = 0,
    ) -> None:
        super().__init__(capture_format, playback_format)
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - exercised only on-device
            raise RuntimeError(
                "the 'sounddevice' package is required for the pipewire/alsa audio "
                "backend; install the 'audio' extra (pip install '.[audio]') or set "
                "audio.backend: mock for development"
            ) from exc
        self._sd = sd
        self.frame_samples = frame_samples
        self.capture_device = capture_device
        self.playback_device = playback_device
        self.music_target = music_target
        # Open this many channels and keep only `capture_pick_channel` as mono,
        # so a multi-channel source (e.g. the XVF3800 via PulseAudio) isn't
        # downmixed (which would blend the raw/echo channel into the clean one).
        self.capture_channels = capture_channels
        self.capture_pick_channel = capture_pick_channel

        self._in_stream: Any = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = asyncio.Event()
        # Streaming playback state.
        self._out_stream: Any = None
        self._pb_buffer = bytearray()
        self._pb_lock = threading.Lock()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopped.clear()

        nch = self.capture_channels
        pick = self.capture_pick_channel

        def _callback(indata: bytes, _frames: int, _time: object, status: object) -> None:
            if status:
                log.warning("capture_status", status=str(status))
            data = bytes(indata)
            if nch > 1:
                samples = array("h")
                samples.frombytes(data)
                data = array("h", samples[pick::nch]).tobytes()  # keep one channel
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._enqueue, data)

        self._in_stream = self._sd.RawInputStream(
            samplerate=self.capture_format.rate,
            channels=nch,
            dtype="int16",
            blocksize=self.frame_samples,
            device=self.capture_device,
            callback=_callback,
        )
        self._in_stream.start()
        log.info("audio_started", capture_rate=self.capture_format.rate)

    def _enqueue(self, data: bytes) -> None:
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            # Expected when nothing is consuming yet (e.g. during WS connect).
            log.debug("capture_overrun_dropped_frame")

    def drain_capture(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def stop(self) -> None:
        self._stopped.set()
        if self._in_stream is not None:
            self._in_stream.stop()
            self._in_stream.close()
            self._in_stream = None
        log.info("audio_stopped")

    def list_devices(self) -> list[AudioDevice]:
        out: list[AudioDevice] = []
        for i, d in enumerate(self._sd.query_devices()):
            out.append(
                AudioDevice(
                    index=i,
                    name=str(d["name"]),
                    max_input_channels=int(d["max_input_channels"]),
                    max_output_channels=int(d["max_output_channels"]),
                )
            )
        return out

    async def capture_stream(self) -> AsyncIterator[bytes]:
        while not self._stopped.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue

    async def play_pcm(self, data: bytes, fmt: AudioFormat | None = None) -> None:
        fmt = fmt or self.playback_format

        def _blocking_play() -> None:
            stream = self._sd.RawOutputStream(
                samplerate=fmt.rate,
                channels=fmt.channels,
                dtype="int16",
                device=self.playback_device,
            )
            stream.start()
            try:
                stream.write(data)
            finally:
                stream.stop()
                stream.close()

        await asyncio.to_thread(_blocking_play)

    async def play_stream_start(self, fmt: AudioFormat | None = None) -> None:
        fmt = fmt or self.playback_format

        def _callback(outdata: Any, frames: int, _time: object, status: object) -> None:
            if status:
                log.debug("playback_status", status=str(status))
            need = frames * fmt.bytes_per_frame
            with self._pb_lock:
                have = min(need, len(self._pb_buffer))
                if have:
                    outdata[:have] = self._pb_buffer[:have]
                    del self._pb_buffer[:have]
                if have < need:
                    outdata[have:need] = b"\x00" * (need - have)

        with self._pb_lock:
            self._pb_buffer.clear()
        self._out_stream = self._sd.RawOutputStream(
            samplerate=fmt.rate,
            channels=fmt.channels,
            dtype="int16",
            device=self.playback_device,
            callback=_callback,
        )
        self._out_stream.start()
        log.info("playback_stream_started", rate=fmt.rate)

    def play_stream_write(self, pcm: bytes) -> None:
        with self._pb_lock:
            self._pb_buffer.extend(pcm)

    def play_stream_clear(self) -> None:
        with self._pb_lock:
            self._pb_buffer.clear()

    async def play_stream_stop(self) -> None:
        if self._out_stream is not None:
            self._out_stream.stop()
            self._out_stream.close()
            self._out_stream = None
        with self._pb_lock:
            self._pb_buffer.clear()
        log.info("playback_stream_stopped")

    async def set_music_gain(self, level: float) -> None:
        if not self.music_target:
            log.debug("duck_noop_no_target", level=level)
            return
        wpctl = shutil.which("wpctl")
        pactl = shutil.which("pactl")
        if wpctl:
            cmd = [wpctl, "set-volume", self.music_target, f"{level:.3f}"]
        elif pactl:
            cmd = [pactl, "set-sink-input-volume", self.music_target, f"{int(level * 65536)}"]
        else:  # pragma: no cover - depends on host tooling
            log.warning("duck_no_mixer_tool")
            return
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:  # pragma: no cover - host-dependent
            log.warning("duck_failed", rc=proc.returncode, err=err.decode(errors="replace"))
