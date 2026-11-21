"""Pure audio-only streaming bite detection; safe to import on any platform."""
from ._types import AudioChunk
from .detector import BiteDetector, BiteEvent, ClipScore

__all__ = ["AudioChunk", "BiteEvent", "BiteDetector", "ClipScore"]
