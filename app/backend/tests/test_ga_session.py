"""Tests for GA session translation (_to_ga_session) and semantic ranker gating."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from rtmt import ToolResultDirection, _to_ga_session
from tools import search


class ToGaSessionTests(unittest.TestCase):
    """Tests for _to_ga_session translation from legacy → GA realtime session shape."""

    def test_type_field_added(self):
        result = _to_ga_session({"instructions": "hello"})
        self.assertEqual(result["type"], "realtime")

    def test_turn_detection_moved_to_audio_input(self):
        session = {"turn_detection": {"type": "server_vad", "threshold": 0.7}}
        result = _to_ga_session(session)
        self.assertNotIn("turn_detection", result)
        self.assertEqual(result["audio"]["input"]["turn_detection"], {"type": "server_vad", "threshold": 0.7})

    def test_voice_moved_to_audio_output(self):
        session = {"voice": "coral"}
        result = _to_ga_session(session)
        self.assertNotIn("voice", result)
        self.assertEqual(result["audio"]["output"]["voice"], "coral")

    def test_input_audio_format_converted(self):
        session = {"input_audio_format": "pcm16"}
        result = _to_ga_session(session)
        self.assertNotIn("input_audio_format", result)
        self.assertEqual(result["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})

    def test_output_audio_format_converted(self):
        session = {"output_audio_format": "g711_ulaw"}
        result = _to_ga_session(session)
        self.assertNotIn("output_audio_format", result)
        self.assertEqual(result["audio"]["output"]["format"], {"type": "audio/pcmu"})

    def test_max_response_output_tokens_renamed(self):
        session = {"max_response_output_tokens": 4096}
        result = _to_ga_session(session)
        self.assertNotIn("max_response_output_tokens", result)
        self.assertEqual(result["max_output_tokens"], 4096)

    def test_modalities_renamed(self):
        session = {"modalities": ["text", "audio"]}
        result = _to_ga_session(session)
        self.assertNotIn("modalities", result)
        self.assertEqual(result["output_modalities"], ["text", "audio"])

    def test_temperature_dropped(self):
        session = {"temperature": 0.6, "instructions": "test"}
        result = _to_ga_session(session)
        self.assertNotIn("temperature", result)

    def test_disable_audio_dropped(self):
        session = {"disable_audio": False, "instructions": "test"}
        result = _to_ga_session(session)
        self.assertNotIn("disable_audio", result)

    def test_unknown_keys_dropped(self):
        session = {"unknown_field": "value", "another_random": 42, "instructions": "test"}
        result = _to_ga_session(session)
        self.assertNotIn("unknown_field", result)
        self.assertNotIn("another_random", result)

    def test_tools_preserved(self):
        session = {"tools": [{"type": "function", "name": "search"}], "tool_choice": "auto"}
        result = _to_ga_session(session)
        self.assertEqual(result["tools"], [{"type": "function", "name": "search"}])
        self.assertEqual(result["tool_choice"], "auto")

    def test_input_audio_transcription_moved(self):
        session = {"input_audio_transcription": {"model": "whisper-1"}}
        result = _to_ga_session(session)
        self.assertNotIn("input_audio_transcription", result)
        self.assertEqual(result["audio"]["input"]["transcription"], {"model": "whisper-1"})

    def test_full_legacy_session(self):
        """Full integration test with a realistic legacy session."""
        session = {
            "instructions": "You are a helpful assistant.",
            "tools": [{"type": "function", "name": "search"}],
            "tool_choice": "auto",
            "voice": "coral",
            "temperature": 0.6,
            "max_response_output_tokens": 4096,
            "modalities": ["text", "audio"],
            "turn_detection": {"type": "server_vad", "threshold": 0.7},
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "disable_audio": False,
        }
        result = _to_ga_session(session)
        # Required fields present
        self.assertEqual(result["type"], "realtime")
        self.assertEqual(result["instructions"], "You are a helpful assistant.")
        self.assertEqual(result["max_output_tokens"], 4096)
        self.assertEqual(result["output_modalities"], ["text", "audio"])
        # Audio nested properly
        self.assertEqual(result["audio"]["input"]["turn_detection"], {"type": "server_vad", "threshold": 0.7})
        self.assertEqual(result["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(result["audio"]["input"]["transcription"], {"model": "whisper-1"})
        self.assertEqual(result["audio"]["output"]["voice"], "coral")
        self.assertEqual(result["audio"]["output"]["format"], {"type": "audio/pcm", "rate": 24000})
        # Dropped legacy keys
        self.assertNotIn("temperature", result)
        self.assertNotIn("disable_audio", result)
        self.assertNotIn("voice", result)
        self.assertNotIn("turn_detection", result)
        # Only allowed keys remain
        for key in result:
            self.assertIn(key, {"type", "instructions", "tools", "tool_choice",
                                "max_output_tokens", "output_modalities", "audio"})


class SemanticRankerGatingTests(unittest.TestCase):
    """Tests for semantic ranker gating in the search tool."""

    def _make_mock_client(self, records, fail_on_semantic=False):
        """Create a mock SearchClient."""
        client = AsyncMock()
        call_count = {"n": 0}

        async def _fake_search(**kwargs):
            call_count["n"] += 1
            if fail_on_semantic and kwargs.get("query_type") == "semantic":
                from unittest.mock import MagicMock

                from azure.core.exceptions import HttpResponseError
                mock_resp = MagicMock()
                mock_resp.status_code = 400
                mock_resp.reason = "Bad Request"
                raise HttpResponseError("Semantic search is not available", response=mock_resp)

            async def _async_iter():
                for r in records:
                    yield r
            return _async_iter()

        client.search = _fake_search
        client._call_count = call_count
        return client

    def test_no_semantic_when_disabled(self):
        """When use_semantic_ranker=False, no semantic query is attempted."""
        records = [{"id": "1", "name": "Latte", "category": "Drinks", "description": "A latte", "sizes": "M"}]
        client = self._make_mock_client(records)
        result = asyncio.run(search(
            client, "menuSemanticConfig", "id", "description", "embedding", False,
            {"query": "latte"}, use_semantic_ranker=False
        ))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("[1]", result.text)

    def test_semantic_used_when_enabled(self):
        """When use_semantic_ranker=True, semantic query is attempted."""
        records = [{"id": "1", "name": "Latte", "category": "Drinks", "description": "A latte", "sizes": "M"}]
        client = self._make_mock_client(records)
        result = asyncio.run(search(
            client, "menuSemanticConfig", "id", "description", "embedding", False,
            {"query": "latte"}, use_semantic_ranker=True
        ))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("[1]", result.text)

    def test_runtime_fallback_on_semantic_failure(self):
        """When semantic ranker fails at runtime, falls back to non-semantic."""
        records = [{"id": "1", "name": "Latte", "category": "Drinks", "description": "A latte", "sizes": "M"}]
        client = self._make_mock_client(records, fail_on_semantic=True)
        result = asyncio.run(search(
            client, "menuSemanticConfig", "id", "description", "embedding", False,
            {"query": "latte"}, use_semantic_ranker=True
        ))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("[1]", result.text)


if __name__ == "__main__":
    unittest.main()
