"""PCM WAV input/output and deterministic, dependency-light offline resampling."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator
import wave

import numpy as np


def _rate(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _mono(samples: np.ndarray) -> np.ndarray:
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim != 1:
        raise ValueError("Audio samples must be a one-dimensional mono array")
    if not np.all(np.isfinite(data)):
        raise ValueError("Audio samples contain NaN or infinity")
    return data


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read uncompressed integer PCM8/16/24/32 WAV and downmix to float32 mono.

    PCM8 is unsigned; wider PCM is signed little-endian. IEEE-float, ADPCM,
    compressed WAV, and unsupported containers are rejected rather than guessed.
    """
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getcomptype() != "NONE":
                raise ValueError("Only uncompressed integer PCM WAV is supported")
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frames = reader.getnframes()
            if channels < 1 or rate < 1:
                raise ValueError("Invalid WAV channel count or sample rate")
            if width not in (1, 2, 3, 4):
                raise ValueError(f"Unsupported PCM sample width: {width * 8} bits")
            raw = reader.readframes(frames)
            if len(raw) != frames * channels * width:
                raise ValueError("Truncated WAV audio data")
    except (wave.Error, EOFError, ValueError) as exc:
        raise ValueError(f"Cannot read WAV '{path}': {exc}") from exc
    except OSError as exc:
        raise OSError(f"Cannot open WAV '{path}': {exc}") from exc

    if width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        signed = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        signed = np.where(signed & 0x800000, signed - 0x1000000, signed)
        values = signed.astype(np.float64) / 8388608.0
    else:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    mono = values.reshape(-1, channels).mean(axis=1)
    return mono.astype(np.float32), int(rate)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Offline linear resampling (not a per-block live resampler).

    This deliberately avoids SciPy. Linear interpolation is adequate for the
    coarse spectral fingerprints; it is not a mastering-quality antialiasing
    filter. Live capture uses a persistent phase accumulator in audio.py.
    """
    src_rate = _rate(src_rate, "src_rate")
    dst_rate = _rate(dst_rate, "dst_rate")
    data = _mono(samples)
    if data.size == 0 or src_rate == dst_rate:
        return data.copy()
    count = (len(data) * dst_rate + src_rate - 1) // src_rate
    positions = np.arange(count, dtype=np.float64) * (src_rate / dst_rate)
    return np.interp(positions, np.arange(len(data)), data).astype(np.float32)


def write_wav(
    path: str | Path,
    samples: np.ndarray,
    rate: int,
    *,
    overwrite: bool = False,
) -> None:
    """Write PCM16 mono; refuse existing paths unless overwrite=True.

    Finite samples outside [-1, 1] are clipped before quantization. The exclusive
    file creation is intentional: an existence check alone would race writers.
    """
    path = Path(path)
    rate = _rate(rate, "rate")
    data = _mono(samples)
    pcm = np.rint(np.clip(data.astype(np.float64), -1.0, 1.0) * 32768.0)
    encoded = np.clip(pcm, -32768, 32767).astype("<i2").tobytes()
    with path.open("wb" if overwrite else "xb") as handle:
        with wave.open(handle, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(rate)
            writer.writeframes(encoded)


def iter_wav_chunks(
    path: str | Path,
    sample_rate: int = 22050,
    block_seconds: float = 0.1,
) -> Iterator["AudioChunk"]:
    """Yield offline chunks on a zero-based audio clock, including the tail."""
    from .detection import AudioChunk

    sample_rate = _rate(sample_rate, "sample_rate")
    if not np.isfinite(block_seconds) or block_seconds <= 0:
        raise ValueError("block_seconds must be positive and finite")
    block = max(1, int(round(sample_rate * block_seconds)))
    samples, rate = read_wav(path)
    samples = resample(samples, rate, sample_rate)
    for start in range(0, len(samples), block):
        end = min(start + block, len(samples))
        yield AudioChunk(samples=samples[start:end].copy(), sample_rate=sample_rate, t_end=end / sample_rate)
