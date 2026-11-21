"""Measure how configured bite templates survive remote-play degradation.

This is a report, not a pass/fail test: it answers "will the built-in sounds still
match after UU encoding, quiet volume, background noise, or slight speed changes?"
It uses only local WAVs, so it runs on Windows and macOS without a microphone.

    python scripts/template_robustness.py
    python scripts/template_robustness.py --threshold 0.60
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from uu_fishing.detection import AudioChunk  # noqa: E402
from uu_fishing.config import load_config  # noqa: E402
from uu_fishing.templates import list_templates, make_detector  # noqa: E402
from uu_fishing.wav_io import read_wav, resample  # noqa: E402

RATE = 22050
BLOCK = 0.1


def load(path: Path) -> np.ndarray:
    samples, rate = read_wav(path)
    return resample(samples, rate, RATE)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def gain(x: np.ndarray, db: float) -> np.ndarray:
    return (x * (10 ** (db / 20))).astype(np.float32)


def add_noise(x: np.ndarray, snr_db: float, seed: int = 11) -> np.ndarray:
    noise = np.random.default_rng(seed).normal(0, 1, len(x))
    target = rms(x) / (10 ** (snr_db / 20))
    noise *= target / max(rms(noise), 1e-12)
    return (x + noise).astype(np.float32)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """Brick-wall lowpass via FFT: crude, but enough to mimic codec roll-off."""
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / RATE)
    spectrum[freqs > cutoff] = 0
    return np.fft.irfft(spectrum, n=len(x)).astype(np.float32)


def speed(x: np.ndarray, factor: float) -> np.ndarray:
    """Play faster/slower, which shifts pitch as remote codecs commonly do."""
    count = max(1, int(round(len(x) / factor)))
    positions = np.linspace(0, len(x) - 1, count)
    return np.interp(positions, np.arange(len(x)), x).astype(np.float32)


CONDITIONS = {
    "clean": lambda x: x,
    "quiet -20dB": lambda x: gain(x, -20),
    "quiet -30dB": lambda x: gain(x, -30),
    "noise SNR 0dB": lambda x: add_noise(x, 0),
    "noise SNR 10dB": lambda x: add_noise(x, 10),
    "lowpass 4kHz": lambda x: lowpass(x, 4000),
    "lowpass 2kHz": lambda x: lowpass(x, 2000),
    "speed 1.05x": lambda x: speed(x, 1.05),
    "speed 0.95x": lambda x: speed(x, 0.95),
    "codec-like": lambda x: add_noise(lowpass(speed(x, 1.04), 4000), 12),
}


def score(detector, audio: np.ndarray) -> float:
    detector.reset()
    padded = np.concatenate([
        np.zeros(int(RATE * 0.5), np.float32), audio,
        np.zeros(int(RATE * 1.0), np.float32),
    ])
    best = -1.0
    block = int(RATE * BLOCK)
    for start in range(0, len(padded), block):
        chunk = padded[start:start + block]
        detector.feed(AudioChunk(chunk, RATE, (start + len(chunk)) / RATE))
        best = max(best, detector.best_heard.score)
    return best


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="已配置的咬钩模板抗干扰能力报告")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--threshold", type=float)
    args = parser.parse_args()
    cfg = load_config(args.config)
    settings = cfg.detector
    threshold = settings.threshold if args.threshold is None else args.threshold
    try:
        paths = list_templates(cfg.templates_path, settings.use_builtin)
        detector = make_detector(cfg.templates_path, settings.use_builtin, threshold=threshold,
                                 onset_seconds=settings.onset_seconds, refractory_seconds=settings.refractory_seconds)
        samples = [(path.stem, load(path)) for path in paths]
    except (OSError, ValueError) as exc:
        parser.error(f"{exc}；请先按 README 准备音频模板")

    width = max(12, max(len(name) for name, _ in samples) + 2)
    header = "condition".ljust(16) + "".join(name.ljust(width) for name, _ in samples)
    print(f"threshold {threshold:.2f}; score is spectral correlation, not probability. OK=at or above\n")
    print(header)
    print("-" * len(header))
    misses = []
    for label, transform in CONDITIONS.items():
        cells = []
        for name, audio in samples:
            value = score(detector, transform(audio))
            mark = "OK" if value >= threshold else "--"
            cells.append(f"{mark} {value:.3f}".ljust(width))
            if value < threshold:
                misses.append((label, name, value))
        print(label.ljust(16) + "".join(cells))
    print()
    if misses:
        print("Below threshold:")
        for label, name, value in misses:
            print(f"  - {label}: {name} = {value:.3f}")
    else:
        print("All conditions reached the threshold.")
    print("\nNote: single-clip offline simulation only; it cannot predict the real UU miss rate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
