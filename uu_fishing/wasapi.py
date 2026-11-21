"""Windows playback-endpoint capture, through PyAudioWPatch (not a microphone)."""
from __future__ import annotations

from contextlib import contextmanager
import queue
import threading
import time

import numpy as np

from .audio import AudioDevice, AudioError, _Capture, _select_device


def _pyaudio():
    try:
        import pyaudiowpatch
    except ImportError as exc:
        raise AudioError("Windows 采音需要 PyAudioWPatch；请安装 requirements.txt") from exc
    return pyaudiowpatch


def _loopbacks(audio):
    return [d for d in audio.get_loopback_device_info_generator()
            if d.get("isLoopbackDevice") and d["maxInputChannels"] > 0]


def _describe(device) -> AudioDevice:
    return AudioDevice(str(device["index"]), device["name"], int(device["maxInputChannels"]))


def list_loopbacks() -> list[AudioDevice]:
    with _pyaudio().PyAudio() as audio:
        return [_describe(d) for d in _loopbacks(audio)]


@contextmanager
def open_loopback(selector: str, block_seconds: float, stop: threading.Event):
    pa = _pyaudio()
    with pa.PyAudio() as audio:
        devices = {_describe(d): d for d in _loopbacks(audio)}
        if selector.casefold() == "default":
            try:
                selected = _describe(audio.get_default_wasapi_loopback())
                device = devices[selected]
            except (KeyError, OSError, ValueError) as exc:
                raise AudioError("默认 Windows 输出没有可用的 WASAPI 回环；请用 devices 查看并指定 audio.windows_device") from exc
        else:
            selected = _select_device(list(devices), selector, "请用 devices 查看可用回环设备。")
            device = devices[selected]
        rate, channels = round(device["defaultSampleRate"]), selected.channels
        packets: queue.Queue[tuple[bytes, float]] = queue.Queue(maxsize=4)
        failed = threading.Event()

        def callback(data, frames, timing, status):
            if stop.is_set():
                return None, pa.paComplete
            if not (status & pa.paInputOverflow) and data and len(data) == frames * channels * 2:
                try:
                    packets.put_nowait((data, time.monotonic()))
                    return None, pa.paContinue
                except queue.Full:
                    pass
            failed.set()
            return None, pa.paAbort

        stream = audio.open(format=pa.paInt16, channels=channels, rate=rate, input=True,
                            input_device_index=device["index"],
                            frames_per_buffer=max(1, round(rate * block_seconds)),
                            stream_callback=callback, start=False)
        try:
            stream.start_stream()

            def read():
                while not stop.is_set():
                    if failed.is_set():
                        raise AudioError("WASAPI 采音丢帧或收到无效数据；请重启监听")
                    try:
                        data, captured_at = packets.get(timeout=0.05)
                    except queue.Empty:
                        if not stream.is_active() and not stop.is_set():
                            raise AudioError("WASAPI 采音流已停止；请检查当前的 Windows 输出设备")
                        continue
                    if failed.is_set():
                        raise AudioError("WASAPI 采音丢帧；不处理积压音频")
                    samples = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, channels)
                    return samples / 32768.0, captured_at
                return None

            yield _Capture(selected.name, rate, read)
        finally:
            stream.close()
