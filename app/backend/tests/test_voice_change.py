"""Tests for extension.set_voice handling in the middle tier."""

import json
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from rtmt import _VALID_VOICES, _to_ga_session


class VoiceChangeTests(unittest.TestCase):
    """Unit tests for voice-change plumbing."""

    def test_valid_voices_include_all_expected(self):
        """All 10 documented voices are in the valid set."""
        expected = {"alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"}
        self.assertEqual(_VALID_VOICES, expected)

    def test_to_ga_session_routes_voice_to_audio_output(self):
        """voice key should end up at session.audio.output.voice in GA shape."""
        legacy = {"voice": "ash"}
        ga = _to_ga_session(legacy)
        self.assertEqual(ga["audio"]["output"]["voice"], "ash")
        self.assertNotIn("voice", ga)  # removed from top level

    def test_to_ga_session_preserves_existing_audio_fields(self):
        """Existing audio.output fields are preserved when voice is added."""
        legacy = {"voice": "coral", "output_audio_format": "pcm16"}
        ga = _to_ga_session(legacy)
        self.assertEqual(ga["audio"]["output"]["voice"], "coral")
        # pcm16 should be translated to GA format object
        self.assertEqual(ga["audio"]["output"]["format"]["type"], "audio/pcm")

    def test_extension_set_voice_message_shape(self):
        """Verify the expected message shape from the frontend."""
        msg = json.dumps({"type": "extension.set_voice", "voice": "marin"})
        parsed = json.loads(msg)
        self.assertEqual(parsed["type"], "extension.set_voice")
        self.assertIn(parsed["voice"], _VALID_VOICES)

    def test_invalid_voice_not_in_valid_set(self):
        """An unknown voice name is not accepted."""
        self.assertNotIn("INVALID", _VALID_VOICES)
        self.assertNotIn("", _VALID_VOICES)


if __name__ == "__main__":
    unittest.main()
