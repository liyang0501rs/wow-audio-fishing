"""Built-in and user-supplied WAV templates, each with its own fingerprint."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .detection import BiteDetector
from .detection.features import DEFAULT_MEL
from .detection.fingerprint import build_fingerprint
from .wav_io import read_wav, resample

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "assets" / "bites"
BUILTIN_NAMES = ("fishbite", "fishingbobber_1", "fishingbobber_2", "fishingbobber_3")
# Mac UU reports showed repeated premature reels with this template at 0.660–0.669.
# Keep the other templates' sensitivity; a higher user threshold still wins.
_FISHBITE_FLOOR = 0.75
MAX_TEMPLATE_SECONDS = 30.0


def list_templates(
    template_dir: Path | None = None,
    use_builtin: bool = True,
) -> list[Path]:
    """Return deterministic built-in-then-user paths; do not relabel user audio.

    User directories are searched recursively, case-insensitively for .wav.
    Aliased/case-colliding paths are errors rather than silently duplicated.
    """
    paths: list[Path] = []
    if use_builtin:
        for name in BUILTIN_NAMES:
            path = BUILTIN_DIR / f"{name}.wav"
            if not path.is_file():
                raise ValueError(f"Built-in bite template is missing: {path}")
            paths.append(path)
    if template_dir is not None:
        directory = Path(template_dir).expanduser()
        if not directory.is_dir():
            raise ValueError(f"Template directory does not exist or is not a directory: {directory}")
        custom = sorted(
            (path for path in directory.rglob("*") if path.is_file() and path.suffix.casefold() == ".wav"),
            key=lambda path: (path.relative_to(directory).as_posix().casefold(), path.relative_to(directory).as_posix()),
        )
        if not custom and not use_builtin:
            raise ValueError(f"No WAV templates found in: {directory}")
        relative_names: set[str] = set()
        for path in custom:
            relative = path.relative_to(directory).as_posix().casefold()
            if relative in relative_names:
                raise ValueError(f"Case-colliding custom WAV template name: {path}")
            relative_names.add(relative)
        paths.extend(custom)
    if not paths:
        raise ValueError("No templates selected: enable built-ins or provide a WAV template directory")
    seen: set[str] = set()
    for path in paths:
        identity = str(path.resolve()).casefold()
        if identity in seen:
            raise ValueError(f"Duplicate WAV template path (possibly an alias of a built-in): {path}")
        seen.add(identity)
    return paths


def make_detector(
    template_dir: Path | None = None,
    use_builtin: bool = True,
    threshold: float = 0.66,
    onset_seconds: float = 0.6,
    refractory_seconds: float = 3.0,
) -> BiteDetector:
    """Build an independent onset fingerprint for every selected WAV.

    Built-ins retain upstream stems. User names are ``user:<relative/path.wav>``
    and are never assigned to one of the four built-in sound classes. Templates
    must contain a full onset, be <=30s and have audible, finite onset samples.
    """
    if not math.isfinite(onset_seconds) or onset_seconds <= 0:
        raise ValueError("onset_seconds must be positive and finite")
    if onset_seconds > MAX_TEMPLATE_SECONDS:
        raise ValueError(f"onset_seconds exceeds the {MAX_TEMPLATE_SECONDS:g}s template limit")
    if int(onset_seconds * DEFAULT_MEL.sample_rate) < DEFAULT_MEL.n_fft + DEFAULT_MEL.hop:
        raise ValueError("onset_seconds is too short to build an audio fingerprint")
    paths = list_templates(template_dir, use_builtin)
    builtin_count = len(BUILTIN_NAMES) if use_builtin else 0
    directory = Path(template_dir).expanduser() if template_dir is not None else None
    fingerprints = []
    names: set[str] = set()
    for index, path in enumerate(paths):
        name = path.stem if index < builtin_count else "user:" + path.relative_to(directory).as_posix()
        if name.casefold() in names:
            raise ValueError(f"Duplicate template name: {name}")
        names.add(name.casefold())
        try:
            samples, rate = read_wav(path)
            duration = len(samples) / rate
            if duration < onset_seconds:
                raise ValueError(f"too short ({duration:.3f}s); need at least {onset_seconds:g}s")
            if duration > MAX_TEMPLATE_SECONDS:
                raise ValueError(f"too long ({duration:.1f}s); trim to <= {MAX_TEMPLATE_SECONDS:g}s around the bite onset")
            onset = samples[: int(onset_seconds * rate)]
            rms = float(np.sqrt(np.mean(onset.astype(np.float64) ** 2)))
            if not math.isfinite(rms) or rms < 1e-5:
                raise ValueError("silent or nearly silent onset; trim leading silence and increase the recording level")
            samples = resample(samples, rate, DEFAULT_MEL.sample_rate)
            fp = build_fingerprint(name, samples, DEFAULT_MEL, onset_seconds)
            if float(np.linalg.norm(fp.feats)) < 0.5:
                raise ValueError("onset has no usable spectral variation")
            fingerprints.append(fp)
        except (ValueError, OSError) as exc:
            raise ValueError(f"Invalid WAV template '{path}': {exc}") from exc
    floors = {"fishbite": _FISHBITE_FLOOR} if use_builtin else None
    return BiteDetector(fingerprints, threshold=threshold, threshold_floors=floors,
                        refractory_s=refractory_seconds)
