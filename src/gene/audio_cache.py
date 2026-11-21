"""Compatibility import; implementation lives in red-audio-adapter."""

import sys

from red_audio_adapter import audio_cache as _implementation

sys.modules[__name__] = _implementation
