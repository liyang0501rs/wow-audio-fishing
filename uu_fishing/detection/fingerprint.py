"""Gain-invariant onset fingerprints and normalized cross-correlation.

Adapted from rsamf/wow-fishing-forever, a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7.
Copyright (c) 2019-2026 Richard Franklin. MIT; see third_party/rsamf/LICENSE.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .features import SILENCE_LEVEL_DB, MelParams, log_mel

NO_SCORE = -1.0
_BLOCK = 2048


@dataclass(frozen=True)
class Fingerprint:
    name: str
    feats: np.ndarray

    @property
    def length(self) -> int:
        return len(self.feats)


def normalize_block(feats: np.ndarray) -> np.ndarray:
    """Remove per-frame gain and centre each window, then take unit norm."""
    f = feats - feats.mean(axis=-1, keepdims=True)
    f = f - f.mean(axis=(-2, -1), keepdims=True)
    norm = np.sqrt((f * f).sum(axis=(-2, -1), keepdims=True)) + 1e-9
    return f / norm


def build_fingerprint(
    name: str, samples: np.ndarray, p: MelParams, onset_s: float = 0.8
) -> Fingerprint:
    feats = log_mel(samples[: int(onset_s * p.sample_rate)], p)
    if len(feats) < 2:
        raise ValueError(f"clip {name!r} is too short for a {onset_s}s onset fingerprint")
    return Fingerprint(name, normalize_block(feats).astype(np.float32))


def score_sequence(feats: np.ndarray, fp: Fingerprint) -> np.ndarray:
    """NCC of every trailing L-frame window; NO_SCORE before L or in silence."""
    t, length = len(feats), fp.length
    out = np.full(t, NO_SCORE, dtype=np.float32)
    if t < length:
        return out
    windows = sliding_window_view(feats, (length, feats.shape[1]))[:, 0]
    level = np.convolve(feats.mean(axis=1), np.ones(length) / length, mode="valid")
    for start in range(0, len(windows), _BLOCK):
        block = windows[start : start + _BLOCK]
        scores = np.einsum("tij,ij->t", normalize_block(block), fp.feats)
        quiet = level[start : start + len(block)] < SILENCE_LEVEL_DB
        lo = length - 1 + start
        out[lo : lo + len(block)] = np.where(quiet, NO_SCORE, scores)
    return out
