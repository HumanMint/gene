"""Compatibility import; implementation lives in red-audio-adapter."""

import sys

from red_audio_adapter import audio_resolver as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
