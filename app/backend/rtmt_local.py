import asyncio
import base64
import json
import logging
import os
import re
import ssl
import struct
import time
from typing import Any

import aiohttp
from aiohttp import web

from order_state import SessionIdentifiers, order_state_singleton
from rtmt import Tool, ToolResult, ToolResultDirection

logger = logging.getLogger("coffee-chat")

__all__ = ["RTLocalPipeline"]

# ---------------------------------------------------------------------------
# Service URLs (overridable via env vars)
# ---------------------------------------------------------------------------
WHISPER_STT_URL = os.environ.get("WHISPER_STT_URL", "http://whisper-stt.dunkin-voice.svc:8000")
FOUNDRY_LLM_URL = os.environ.get("FOUNDRY_LLM_URL", "http://phi-4-mini-gpu.foundry-local-operator.svc:5000")
FOUNDRY_API_KEY = os.environ.get("FOUNDRY_API_KEY", "")
PIPER_TTS_URL = os.environ.get("PIPER_TTS_URL", "http://piper-tts.dunkin-voice.svc:5000")
TTS_VOICE = os.environ.get("TTS_VOICE", "en_US-amy-medium")

# Audio constants — frontend expects 24 kHz 16-bit mono PCM
TARGET_SAMPLE_RATE = 24000
BITS_PER_SAMPLE = 16
CHANNELS = 1
AUDIO_CHUNK_SIZE = 4800  # bytes per streaming chunk (~100 ms at 24 kHz)

# VAD constants — tuned to reduce false triggers
SPEECH_THRESHOLD = 800000       # energy floor (raised to reject ambient noise)
SILENCE_DURATION = 0.8          # seconds of silence before processing
MIN_SPEECH_DURATION = 0.4       # minimum seconds of speech to send to STT
MIN_SPEECH_BYTES = int(MIN_SPEECH_DURATION * TARGET_SAMPLE_RATE * 2)  # 19200 bytes

# Whisper hallucination filter — common phantom transcripts
_HALLUCINATION_RE = re.compile(
    r"^(you|thank you\.?|thanks\.?|bye\.?|okay\.?|uh|um|hmm|huh|ah|oh|"
    r"thanks for watching\.?|subscribe\.?|like and subscribe\.?|"
    r"thank you for watching\.?|have a great day\.?|"
    r"see you in the next video\.?|i've helped you today\.?|"
    r"please don'?t\.?|i'?ll see you\.?|"
    r"music|applause|laughter|silence|"
    r"so|\.+|"
    r"\[.*\]|\(.*\))[\.\!\?\s]*$",
    re.IGNORECASE,
)

LLM_MODEL = os.environ.get("FOUNDRY_LLM_MODEL", "Phi-4-mini-instruct-cuda-gpu:5")


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def create_wav_header(pcm_data_len: int, sample_rate: int = TARGET_SAMPLE_RATE,
                      bits_per_sample: int = BITS_PER_SAMPLE, channels: int = CHANNELS) -> bytes:
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + pcm_data_len, b'WAVE',
        b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b'data', pcm_data_len,
    )


def calculate_energy(pcm_bytes: bytes) -> float:
    if len(pcm_bytes) < 2:
        return 0.0
    n_samples = len(pcm_bytes) // 2
    samples = struct.unpack(f'<{n_samples}h', pcm_bytes[:n_samples * 2])
    return sum(s * s for s in samples) / len(samples)


def pcm_to_wav(pcm_data: bytes, sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    return create_wav_header(len(pcm_data), sample_rate=sample_rate) + pcm_data


def _parse_wav(wav_data: bytes) -> tuple[bytes, int]:
    """Return (pcm_bytes, sample_rate) from a WAV file."""
    if wav_data[:4] != b'RIFF':
        return wav_data, TARGET_SAMPLE_RATE
    # Parse fmt chunk for sample rate
    fmt_idx = wav_data.find(b'fmt ')
    src_rate = TARGET_SAMPLE_RATE
    if fmt_idx != -1:
        src_rate = struct.unpack_from('<I', wav_data, fmt_idx + 12)[0]
    # Parse data chunk
    data_idx = wav_data.find(b'data')
    if data_idx != -1:
        data_size = struct.unpack_from('<I', wav_data, data_idx + 4)[0]
        pcm = wav_data[data_idx + 8: data_idx + 8 + data_size]
        return pcm, src_rate
    return wav_data, src_rate


def _resample_linear(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM via linear interpolation."""
    if src_rate == dst_rate:
        return pcm
    n_src = len(pcm) // 2
    if n_src == 0:
        return pcm
    samples_in = struct.unpack(f'<{n_src}h', pcm)
    ratio = src_rate / dst_rate
    n_dst = int(n_src / ratio)
    out = []
    for i in range(n_dst):
        src_pos = i * ratio
        idx = int(src_pos)
        frac = src_pos - idx
        s0 = samples_in[min(idx, n_src - 1)]
        s1 = samples_in[min(idx + 1, n_src - 1)]
        out.append(max(-32768, min(32767, int(s0 + frac * (s1 - s0)))))
    return struct.pack(f'<{len(out)}h', *out)


# ---------------------------------------------------------------------------
# Per-connection session state
# ---------------------------------------------------------------------------

class _ConnectionState:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.audio_buffer = bytearray()
        self.conversation: list[dict[str, Any]] = []
        self.speech_started = False
        self.last_speech_time: float = 0.0
        self.greeting_sent = False
        self.processing = False           # prevent re-entrant processing
        self.tts_playing = False          # mute mic while TTS is playing
        self.energy_history: list[float] = []  # rolling energy window


# ---------------------------------------------------------------------------
# RTLocalPipeline – drop-in replacement for RTMiddleTier
# ---------------------------------------------------------------------------

class RTLocalPipeline:
    tools: dict[str, Tool]
    system_message: str | None = None
    temperature: float | None = None
    voice_choice: str | None = None

    def __init__(self, voice_choice: str | None = None):
        self.tools = {}
        self.voice_choice = voice_choice or TTS_VOICE
        if voice_choice is not None:
            logger.info("Local pipeline voice choice set to %s", voice_choice)

    # ------------------------------------------------------------------
    # Session-identifier helpers (mirrors RTMiddleTier)
    # ------------------------------------------------------------------

    @staticmethod
    async def _emit_session_identifiers(
        client_ws: web.WebSocketResponse,
        event_type: str,
        identifiers: SessionIdentifiers | None,
    ) -> None:
        if identifiers is None:
            return
        await client_ws.send_json({
            "type": event_type,
            "sessionToken": identifiers.session_token,
            "roundTripIndex": identifiers.round_trip_index,
            "roundTripToken": identifiers.round_trip_token,
        })

    # ------------------------------------------------------------------
    # Service calls
    # ------------------------------------------------------------------

    async def _stt(self, http: aiohttp.ClientSession, wav_bytes: bytes) -> str:
        """Send WAV audio to Whisper STT and return transcribed text."""
        form = aiohttp.FormData()
        form.add_field('file', wav_bytes, filename='audio.wav', content_type='audio/wav')
        form.add_field('model', 'Systran/faster-whisper-small')
        form.add_field('language', 'en')
        form.add_field('vad_filter', 'true')
        async with http.post(f"{WHISPER_STT_URL}/v1/audio/transcriptions", data=form) as resp:
            resp.raise_for_status()
            result = await resp.json()
            return result.get("text", "")

    async def _llm_chat(self, http: aiohttp.ClientSession,
                        messages: list[dict], tools_schema: list[dict] | None = None) -> dict:
        """Call Phi-4 Mini chat completions endpoint."""
        payload: dict[str, Any] = {
            "model": LLM_MODEL,
            "messages": messages,
            "max_tokens": 150,  # keep responses short for faster TTS
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools_schema:
            payload["tools"] = tools_schema
            payload["tool_choice"] = "auto"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if FOUNDRY_API_KEY:
            headers["Authorization"] = f"Bearer {FOUNDRY_API_KEY}"
        async with http.post(f"{FOUNDRY_LLM_URL}/v1/chat/completions",
                             json=payload, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _tts(self, http: aiohttp.ClientSession, text: str) -> bytes:
        """Call Piper TTS and return WAV audio bytes."""
        payload = {
            "model": self.voice_choice or TTS_VOICE,
            "input": text,
            "response_format": "wav",
        }
        async with http.post(f"{PIPER_TTS_URL}/v1/audio/speech", json=payload) as resp:
            resp.raise_for_status()
            return await resp.read()

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _build_tools_schema(self) -> list[dict]:
        """Convert tool schemas to OpenAI chat completions format.

        The RTMiddleTier schemas use a flat format (type/name/description/parameters
        at top level) from the Realtime API.  The standard /v1/chat/completions
        endpoint expects {type: "function", function: {name, description, parameters}}.
        """
        out = []
        for tool in self.tools.values():
            s = tool.schema
            if "function" not in s and "name" in s:
                out.append({
                    "type": "function",
                    "function": {
                        "name": s["name"],
                        "description": s.get("description", ""),
                        "parameters": s.get("parameters", {}),
                    },
                })
            else:
                out.append(s)
        return out

    async def _execute_tool_calls(
        self,
        http: aiohttp.ClientSession,
        client_ws: web.WebSocketResponse,
        state: _ConnectionState,
        tool_calls: list[dict],
    ) -> str:
        """Execute tool calls, append results to conversation, re-call LLM
        for a final natural-language answer."""

        # Build assistant message with tool_calls
        assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
        state.conversation.append(assistant_msg)

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args_str = tc["function"]["arguments"]
            fn_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
            tc_id = tc["id"]

            tool = self.tools.get(fn_name)
            if tool is None:
                tool_output = f"Unknown tool: {fn_name}"
                destination = ToolResultDirection.TO_SERVER
            else:
                if fn_name in ("update_order", "get_order"):
                    result: ToolResult = await tool.target(fn_args, state.session_id)
                else:
                    result = await tool.target(fn_args)
                tool_output = result.to_text()
                destination = result.destination

                if destination == ToolResultDirection.TO_CLIENT:
                    await client_ws.send_json({
                        "type": "extension.middle_tier_tool_response",
                        "tool_name": fn_name,
                        "tool_result": tool_output,
                    })

            state.conversation.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_output if destination == ToolResultDirection.TO_SERVER else "",
            })

        # Re-call LLM with tool results for final response
        llm_resp = await self._llm_chat(http, state.conversation, self._build_tools_schema())
        choice = llm_resp["choices"][0]
        msg = choice["message"]

        # Handle nested tool calls (up to 3 levels)
        if msg.get("tool_calls"):
            return await self._execute_tool_calls(http, client_ws, state, msg["tool_calls"])

        final_text = msg.get("content", "")
        state.conversation.append({"role": "assistant", "content": final_text})
        return final_text

    # ------------------------------------------------------------------
    # Audio streaming helpers
    # ------------------------------------------------------------------

    async def _tts_to_24k_pcm(self, http: aiohttp.ClientSession, text: str) -> bytes:
        """TTS text → WAV → resample to 24 kHz PCM."""
        wav_bytes = await self._tts(http, text)
        pcm, src_rate = _parse_wav(wav_bytes)
        if src_rate != TARGET_SAMPLE_RATE:
            logger.debug("Resampling TTS audio from %d Hz to %d Hz", src_rate, TARGET_SAMPLE_RATE)
        return _resample_linear(pcm, src_rate, TARGET_SAMPLE_RATE)

    async def _stream_pcm(self, client_ws: web.WebSocketResponse, pcm_data: bytes) -> None:
        """Send PCM as base64 chunks to the client with pacing."""
        offset = 0
        chunk_count = 0
        while offset < len(pcm_data):
            chunk = pcm_data[offset: offset + AUDIO_CHUNK_SIZE]
            b64 = base64.b64encode(chunk).decode("ascii")
            await client_ws.send_json({
                "type": "response.audio.delta",
                "delta": b64,
            })
            offset += AUDIO_CHUNK_SIZE
            chunk_count += 1
            # Pace audio sending — yield every 5 chunks (~500ms of audio)
            # to prevent WS buffer overflow which causes dropped audio
            if chunk_count % 5 == 0:
                await asyncio.sleep(0.01)

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text on sentence boundaries for incremental TTS."""
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        return [p for p in parts if p]

    async def _stream_audio_response(
        self,
        http: aiohttp.ClientSession,
        client_ws: web.WebSocketResponse,
        state: _ConnectionState,
        text: str,
    ) -> None:
        """TTS the text as one block, stream audio, then wait for playback
        to finish before unmuting mic."""

        state.tts_playing = True
        state.audio_buffer.clear()

        # TTS the entire response as one piece (avoid per-sentence overhead)
        if not text:
            state.tts_playing = False
            return

        # Send full transcript immediately
        await client_ws.send_json({
            "type": "response.audio_transcript.delta",
            "delta": text,
        })

        tts_start = time.monotonic()
        try:
            pcm_data = await self._tts_to_24k_pcm(http, text)
            logger.info("TTS (%d chars) → %d bytes in %.1fs",
                        len(text), len(pcm_data), time.monotonic() - tts_start)
            await self._stream_pcm(client_ws, pcm_data)
        except Exception as exc:
            logger.error("TTS failed: %s", exc)
            pcm_data = b""

        # Send response.done
        await client_ws.send_json({
            "type": "response.done",
            "response": {"output": []},
        })

        # Wait for the client to finish playing the audio before unmuting mic.
        # Playback duration = pcm_bytes / (sample_rate * bytes_per_sample)
        playback_secs = len(pcm_data) / (TARGET_SAMPLE_RATE * 2) if pcm_data else 0
        wait_time = playback_secs + 1.0  # +1s safety margin
        logger.info("Waiting %.1fs for playback (%.1fs audio + 1s margin)", wait_time, playback_secs)
        await asyncio.sleep(wait_time)

        state.tts_playing = False
        state.audio_buffer.clear()

        # Advance round-trip tracking
        identifiers = order_state_singleton.advance_round_trip(state.session_id)
        await self._emit_session_identifiers(client_ws, "extension.round_trip_token", identifiers)

    # ------------------------------------------------------------------
    # Full speech processing pipeline
    # ------------------------------------------------------------------

    async def _process_speech(
        self,
        http: aiohttp.ClientSession,
        client_ws: web.WebSocketResponse,
        state: _ConnectionState,
    ) -> None:
        pipeline_start = time.monotonic()
        pcm_data = bytes(state.audio_buffer)
        state.audio_buffer.clear()

        audio_duration = len(pcm_data) / (TARGET_SAMPLE_RATE * 2)
        if not pcm_data or len(pcm_data) < MIN_SPEECH_BYTES:
            logger.info("Audio too short (%.1fs / %d bytes), skipping", audio_duration, len(pcm_data))
            return

        logger.info("Processing %.1fs of audio (%d bytes)", audio_duration, len(pcm_data))

        # 1. STT
        wav_bytes = pcm_to_wav(pcm_data)
        t0 = time.monotonic()
        try:
            transcript = await self._stt(http, wav_bytes)
        except Exception as exc:
            logger.error("STT failed: %s", exc)
            transcript = ""
        stt_time = time.monotonic() - t0

        transcript = transcript.strip()
        if not transcript:
            logger.info("Empty transcript (STT took %.1fs), skipping", stt_time)
            return

        # Filter Whisper hallucinations
        if _HALLUCINATION_RE.match(transcript):
            logger.info("Filtered hallucination: '%s' (STT took %.1fs)", transcript, stt_time)
            return

        logger.info("STT: '%s' (%.1fs)", transcript, stt_time)

        # Filter echo: if transcript substantially matches the last assistant response,
        # it's the mic picking up TTS playback — reject it
        if state.conversation:
            for msg in reversed(state.conversation):
                if msg.get("role") == "assistant" and msg.get("content"):
                    last_reply = msg["content"].lower()
                    t_lower = transcript.lower()
                    # Check if >40% of the transcript words appear in the last reply
                    t_words = set(t_lower.split())
                    r_words = set(last_reply.split())
                    if t_words and len(t_words & r_words) / len(t_words) > 0.4:
                        logger.info("Filtered echo (%.0f%% overlap): '%s'",
                                    100 * len(t_words & r_words) / len(t_words), transcript)
                        return
                    break

        # Notify client of completed transcription
        await client_ws.send_json({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": transcript,
        })

        # 2. LLM — add tool-use reminder for Phi-4 Mini
        state.conversation.append({"role": "user", "content": transcript})

        tools_schema = self._build_tools_schema()

        # Inject a system reminder to force tool use when the user orders something
        order_keywords = {"coffee", "donut", "latte", "tea", "sandwich", "bagel", "muffin",
                          "croissant", "wrap", "order", "add", "get", "want", "medium", "large", "small"}
        if any(w in transcript.lower().split() for w in order_keywords):
            state.conversation.append({
                "role": "system",
                "content": (
                    "IMPORTANT: The customer just mentioned a menu item. You MUST: "
                    "1) Call the 'search' tool to find the item and its price. "
                    "2) Then call 'update_order' with action='add' and the correct price. "
                    "Do NOT just say you will add it — you MUST use the tools. "
                    "Keep your spoken response to ONE short sentence."
                ),
            })

        t0 = time.monotonic()
        try:
            llm_resp = await self._llm_chat(http, state.conversation, tools_schema or None)
        except Exception as exc:
            logger.error("LLM call failed: %s", exc)
            await client_ws.send_json({"type": "error", "error": {"message": str(exc)}})
            return
        llm_time = time.monotonic() - t0

        choice = llm_resp["choices"][0]
        msg = choice["message"]

        # 3. Handle tool calls or direct text
        if msg.get("tool_calls"):
            logger.info("LLM: tool calls (%.1fs)", llm_time)
            try:
                final_text = await self._execute_tool_calls(http, client_ws, state, msg["tool_calls"])
            except Exception as exc:
                logger.error("Tool execution failed: %s", exc)
                final_text = "I'm sorry, something went wrong while processing your request."
                state.conversation.append({"role": "assistant", "content": final_text})
        else:
            final_text = msg.get("content", "")
            logger.info("LLM: '%s' (%.1fs)", final_text[:80], llm_time)
            state.conversation.append({"role": "assistant", "content": final_text})

        # 4. TTS + stream
        await self._stream_audio_response(http, client_ws, state, final_text)

        logger.info("Pipeline total: %.1fs (STT=%.1fs LLM=%.1fs)",
                     time.monotonic() - pipeline_start, stt_time, llm_time)

    # ------------------------------------------------------------------
    # Auto-greeting
    # ------------------------------------------------------------------

    async def _send_greeting(
        self,
        http: aiohttp.ClientSession,
        client_ws: web.WebSocketResponse,
        state: _ConnectionState,
    ) -> None:
        state.conversation.append({
            "role": "user",
            "content": "Please greet the guest with: 'Welcome to Dunkin! How may I help you today?'",
        })
        try:
            llm_resp = await self._llm_chat(http, state.conversation)
            greeting = llm_resp["choices"][0]["message"].get("content", "Welcome to Dunkin! How may I help you today?")
        except Exception as exc:
            logger.error("Greeting LLM call failed: %s", exc)
            greeting = "Welcome to Dunkin! How may I help you today?"

        state.conversation.append({"role": "assistant", "content": greeting})
        await self._stream_audio_response(http, client_ws, state, greeting)
        state.greeting_sent = True

    # ------------------------------------------------------------------
    # VAD + audio buffering
    # ------------------------------------------------------------------

    async def _handle_audio_append(
        self,
        http: aiohttp.ClientSession,
        client_ws: web.WebSocketResponse,
        state: _ConnectionState,
        audio_b64: str,
    ) -> None:
        # Drop all audio while TTS is playing (prevents echo feedback loop)
        if state.tts_playing or state.processing:
            return

        pcm_chunk = base64.b64decode(audio_b64)
        energy = calculate_energy(pcm_chunk)

        # Keep rolling window of recent energy values (last ~1s)
        state.energy_history.append(energy)
        if len(state.energy_history) > 10:
            state.energy_history.pop(0)

        if energy > SPEECH_THRESHOLD:
            if not state.speech_started:
                state.speech_started = True
                await client_ws.send_json({"type": "input_audio_buffer.speech_started"})
            state.last_speech_time = time.monotonic()
            state.audio_buffer.extend(pcm_chunk)
        elif state.speech_started:
            # Always buffer audio during speech (captures quiet vowels/pauses)
            state.audio_buffer.extend(pcm_chunk)
            if time.monotonic() - state.last_speech_time >= SILENCE_DURATION:
                state.speech_started = False
                state.processing = True
                try:
                    await self._process_speech(http, client_ws, state)
                finally:
                    state.processing = False

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session_id = order_state_singleton.create_session()
        state = _ConnectionState(session_id)

        # Initialise conversation with system prompt
        if self.system_message:
            state.conversation.append({"role": "system", "content": self.system_message})

        # Allow self-signed certs from Foundry Local operator
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)

        async with aiohttp.ClientSession(connector=connector) as http:
            # Send session.created
            await ws.send_json({
                "type": "session.created",
                "session": {
                    "id": session_id,
                    "voice": self.voice_choice,
                    "instructions": "",
                    "tools": [],
                    "tool_choice": "none",
                    "max_response_output_tokens": None,
                },
            })

            # Emit session metadata
            identifiers = order_state_singleton.get_session_identifiers(session_id)
            await self._emit_session_identifiers(ws, "extension.session_metadata", identifiers)

            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        msg_type = data.get("type")

                        if msg_type == "session.update":
                            # Send greeting on first session.update (client is ready)
                            if not state.greeting_sent:
                                await self._send_greeting(http, ws, state)

                        elif msg_type == "input_audio_buffer.append":
                            if not state.greeting_sent:
                                await self._send_greeting(http, ws, state)
                            audio_b64 = data.get("audio", "")
                            if audio_b64:
                                await self._handle_audio_append(http, ws, state, audio_b64)

                        elif msg_type == "input_audio_buffer.clear":
                            state.audio_buffer.clear()
                            state.speech_started = False

                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
            except ConnectionResetError:
                pass
            finally:
                order_state_singleton.delete_session(session_id)

        return ws

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach_to_app(
        self,
        app: web.Application,
        path: str,
        *,
        simulator=None,
        crm_repo=None,
    ) -> None:
        # Local pipeline currently ignores simulator/CRM hooks but keeps signature parity
        app.router.add_get(path, self._websocket_handler)
