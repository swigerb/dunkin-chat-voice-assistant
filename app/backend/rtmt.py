import asyncio
import copy
import json
import logging
import re
from collections.abc import Callable
from enum import Enum
from typing import Any

import aiohttp
from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

from order_state import SessionIdentifiers, order_state_singleton

logger = logging.getLogger("coffee-chat")

__all__ = ["RTMiddleTier", "RTToolCall", "Tool", "ToolResult", "ToolResultDirection"]


# GA → legacy event name translation for client compatibility.
# The frontend expects legacy names; the GA /openai/v1 endpoint sends these.
_GA_TO_LEGACY_EVENTS: dict[str, str] = {
    "response.output_audio.delta": "response.audio.delta",
    "response.output_audio.done": "response.audio.done",
    "response.output_audio_transcript.delta": "response.audio_transcript.delta",
    "response.output_audio_transcript.done": "response.audio_transcript.done",
    "response.output_text.delta": "response.text.delta",
    "response.output_text.done": "response.text.done",
}

# High-frequency server message types that can be forwarded with minimal processing.
_PASSTHROUGH_SERVER_TYPES = frozenset({
    # GA event names
    "response.output_audio.delta",
    "response.output_audio.done",
    "response.output_audio_transcript.delta",
    "response.output_audio_transcript.done",
    "response.output_text.delta",
    "response.output_text.done",
    # Legacy event names
    "response.audio.delta",
    "response.audio.done",
    "response.audio_transcript.delta",
    "response.audio_transcript.done",
    "response.text.delta",
    "response.text.done",
    # Unchanged across versions
    "response.content_part.added",
    "response.content_part.done",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.committed",
    "rate_limits.updated",
})

# Client messages that never need modification.
_PASSTHROUGH_CLIENT_TYPES = frozenset({
    "input_audio_buffer.append",
    "input_audio_buffer.clear",
    "input_audio_buffer.commit",
})

# Regex to extract "type":"..." from raw JSON without full parse.
_TYPE_RE = re.compile(r'"type"\s*:\s*"([^"]+)"')


# Session keys the GA realtime API accepts at the top level. Anything else that
# the legacy (2024-10-01-preview) clients send is dropped, because GA rejects
# unknown parameters outright instead of ignoring them.
_GA_SESSION_TOP_LEVEL = frozenset({
    "type", "model", "instructions", "tools", "tool_choice",
    "max_output_tokens", "output_modalities", "audio", "tracing",
    "include", "prompt", "truncation",
})

# Legacy audio formats were bare strings ("pcm16"); GA expects an object.
_GA_AUDIO_FORMATS = {
    "pcm16": {"type": "audio/pcm", "rate": 24000},
    "g711_ulaw": {"type": "audio/pcmu"},
    "g711_alaw": {"type": "audio/pcma"},
}


def _ga_audio_format(value: Any) -> Any:
    if isinstance(value, str):
        return _GA_AUDIO_FORMATS.get(value, {"type": "audio/pcm", "rate": 24000})
    return value


def _to_ga_session(session: dict) -> dict:
    """Translate a legacy realtime `session` object into the GA shape.

    The browser client speaks the 2024-10-01-preview dialect. The GA endpoint
    moved most audio settings under `audio.input` / `audio.output`, renamed a
    couple of fields, requires a `type` discriminator, and errors on unknown
    parameters rather than ignoring them. Doing the translation here keeps the
    client contract stable and keeps the failure modes in one place.
    """
    ga: dict = dict(session)
    audio: dict = dict(ga.get("audio") or {})
    audio_in: dict = dict(audio.get("input") or {})
    audio_out: dict = dict(audio.get("output") or {})

    # input side
    if (turn_detection := ga.pop("turn_detection", None)) is not None:
        audio_in["turn_detection"] = turn_detection
    if (transcription := ga.pop("input_audio_transcription", None)) is not None:
        audio_in["transcription"] = transcription
    if (in_fmt := ga.pop("input_audio_format", None)) is not None:
        audio_in["format"] = _ga_audio_format(in_fmt)
    if (noise := ga.pop("input_audio_noise_reduction", None)) is not None:
        audio_in["noise_reduction"] = noise

    # output side
    if (voice := ga.pop("voice", None)) is not None:
        audio_out["voice"] = voice
    if (out_fmt := ga.pop("output_audio_format", None)) is not None:
        audio_out["format"] = _ga_audio_format(out_fmt)
    if (speed := ga.pop("speed", None)) is not None:
        audio_out["speed"] = speed

    # renamed top-level fields
    if (max_tokens := ga.pop("max_response_output_tokens", None)) is not None:
        ga["max_output_tokens"] = max_tokens
    if (modalities := ga.pop("modalities", None)) is not None:
        ga["output_modalities"] = modalities

    if audio_in:
        audio["input"] = audio_in
    if audio_out:
        audio["output"] = audio_out
    if audio:
        ga["audio"] = audio

    ga["type"] = "realtime"

    # `temperature` and `disable_audio` are not part of the GA session object.
    dropped = [k for k in ga if k not in _GA_SESSION_TOP_LEVEL]
    for key in dropped:
        ga.pop(key)
    if dropped:
        logger.debug("session.update: dropped non-GA keys %s", dropped)

    return ga

# Valid voices for the GA realtime API.
_VALID_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"
})

# Fast markers (raw-string checks, no JSON parse on the hot path).
_MARKER_SET_VOICE = '"extension.set_voice"'
_MARKER_SESSION_UPDATE = '"session.update"'
_MARKER_SESSION_UPDATED = '"session.updated"'
_MARKER_AUDIO_DELTA = '"response.output_audio.delta"'
_MARKER_AUDIO_DELTA_LEGACY = '"response.audio.delta"'

# What the browser's useRealtime.startSession() sends. The middle tier applies
# the same values itself the moment the upstream socket opens, so a socket the
# browser never configures (e.g. react-use-websocket reconnected while the mic
# was live) still runs with our instructions, tools and voice.
_BOOTSTRAP_CLIENT_SESSION: dict = {
    "turn_detection": {
        "type": "server_vad",
        "threshold": 0.7,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 500,
    },
    "input_audio_transcription": {"model": "whisper-1"},
}

# How long the greeting waits for the service to confirm the session config.
_SESSION_CONFIGURED_TIMEOUT_SEC = 5.0

_GREETING_TEXT = "Please greet the guest with: 'Welcome to Dunkin! How may I help you today?'"


def _strip_output_voice(ga_session: dict) -> bool:
    """Remove `audio.output.voice` from a GA session in place. Returns True if removed."""
    audio = ga_session.get("audio")
    if not isinstance(audio, dict):
        return False
    output = audio.get("output")
    if not isinstance(output, dict) or "voice" not in output:
        return False
    output.pop("voice")
    if not output:
        audio.pop("output")
    if not audio:
        ga_session.pop("audio")
    return True


class ToolResultDirection(Enum):
    TO_SERVER = 1
    TO_CLIENT = 2

class ToolResult:
    text: str
    destination: ToolResultDirection

    def __init__(self, text: str, destination: ToolResultDirection):
        self.text = text
        self.destination = destination

    def to_text(self) -> str:
        if self.text is None:
            return ""
        return self.text if isinstance(self.text, str) else json.dumps(self.text)

class Tool:
    target: Callable[..., ToolResult]
    schema: Any

    def __init__(self, target: Any, schema: Any):
        self.target = target
        self.schema = schema

class RTToolCall:
    tool_call_id: str
    previous_id: str

    def __init__(self, tool_call_id: str, previous_id: str):
        self.tool_call_id = tool_call_id
        self.previous_id = previous_id

class RTMiddleTier:
    endpoint: str
    deployment: str
    key: str | None = None
    
    # Tools are server-side only for now, though the case could be made for client-side tools
    # in addition to server-side tools that are invisible to the client
    tools: dict[str, Tool]

    # Server-enforced configuration, if set, these will override the client's configuration
    # Typically at least the model name and system message will be set by the server
    model: str | None = None
    system_message: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    disable_audio: bool | None = None
    voice_choice: str | None = None

    def __init__(self, endpoint: str, deployment: str, credentials: AzureKeyCredential | DefaultAzureCredential, voice_choice: str | None = None):
        self.endpoint = endpoint
        self.deployment = deployment
        self.voice_choice = voice_choice
        self.tools = {}
        self._token_provider = None
        self._session_map: dict[web.WebSocketResponse, str] = {}
        self._sent_greeting: set[str] = set()
        if voice_choice is not None:
            logger.info("Realtime voice choice set to %s", voice_choice)
        if isinstance(credentials, AzureKeyCredential):
            self.key = credentials.key
        else:
            self._token_provider = get_bearer_token_provider(credentials, "https://cognitiveservices.azure.com/.default")
            self._token_provider() # Warm up during startup so we have a token cached when the first request arrives

    def _build_session(self, session: dict, voice_locked: bool = False) -> dict:
        """Overlay the server-owned configuration onto a legacy-shaped session
        and translate it to the GA shape.

        `voice_locked` must be True once the upstream conversation contains
        assistant audio. From then on GA rejects any session.update whose voice
        differs from the current one with `cannot_update_voice` -- and it
        rejects the WHOLE event, so tools, tool_choice and instructions are
        lost along with the voice.
        """
        if self.system_message is not None:
            session["instructions"] = self.system_message
        if self.temperature is not None:
            session["temperature"] = self.temperature
        if self.max_tokens is not None:
            session["max_response_output_tokens"] = self.max_tokens
        if self.disable_audio is not None:
            session["disable_audio"] = self.disable_audio
        if self.voice_choice is not None:
            session["voice"] = self.voice_choice
        session["tool_choice"] = "auto" if len(self.tools) > 0 else "none"
        session["tools"] = [tool.schema for tool in self.tools.values()]
        # Translate to the GA shape so the browser contract is unchanged
        # and unsupported legacy keys are dropped rather than rejected.
        ga_session = _to_ga_session(session)
        if voice_locked and _strip_output_voice(ga_session):
            logger.info("session.update: assistant audio already present — omitting voice so the update is not rejected")
        return ga_session

    def build_bootstrap_session_update(self) -> str:
        """Serialise the session.update sent as the very first frame on every
        upstream socket, before any browser traffic is relayed.

        Without it the upstream session runs on service defaults (no tools,
        generic instructions, server VAD auto-responding) until the browser's
        own session.update arrives -- and if the model speaks in that window the
        voice locks and our later session.update is rejected wholesale, so the
        tools are never registered for that conversation.
        """
        session = self._build_session(copy.deepcopy(_BOOTSTRAP_CLIENT_SESSION))
        return json.dumps({"type": "session.update", "session": session})

    def build_voice_update(self, voice: str) -> str:
        """Serialise a voice-only session.update in the GA shape."""
        return json.dumps({"type": "session.update", "session": _to_ga_session({"voice": voice})})

    async def _emit_session_identifiers(
        self,
        client_ws: web.WebSocketResponse,
        event_type: str,
        identifiers: SessionIdentifiers | None,
    ) -> None:
        if identifiers is None:
            return
        await client_ws.send_json(
            {
                "type": event_type,
                "sessionToken": identifiers.session_token,
                "roundTripIndex": identifiers.round_trip_index,
                "roundTripToken": identifiers.round_trip_token,
            }
        )

    async def _process_message_to_client(self, msg: str, client_ws: web.WebSocketResponse, server_ws: web.WebSocketResponse, tools_pending: dict[str, "RTToolCall"]) -> str | None:
        data = msg.data

        # FAST PATH: extract type via regex without full JSON parse.
        # Audio deltas are ~95% of server messages — avoid json.loads entirely.
        m = _TYPE_RE.search(data)
        if m is not None and m.group(1) in _PASSTHROUGH_SERVER_TYPES:
            # Translate GA event names to legacy names for client compatibility
            event_type = m.group(1)
            legacy_name = _GA_TO_LEGACY_EVENTS.get(event_type)
            if legacy_name is not None:
                data = data.replace(f'"{event_type}"', f'"{legacy_name}"', 1)
            return data

        message = json.loads(data)
        updated_message = data
        session_id = self._session_map.get(client_ws)
        if message is not None:
            match message["type"]:
                case "session.created":
                    session = message["session"]
                    # Hide the instructions, tools and max tokens from clients, if we ever allow client-side 
                    # tools, this will need updating
                    session["instructions"] = ""
                    session["tools"] = []
                    session["voice"] = self.voice_choice
                    session["tool_choice"] = "none"
                    session["max_response_output_tokens"] = None
                    updated_message = json.dumps(message)
                    if session_id is not None:
                        identifiers = order_state_singleton.get_session_identifiers(session_id)
                        await self._emit_session_identifiers(client_ws, "extension.session_metadata", identifiers)

                case "response.output_item.added":
                    if "item" in message and message["item"]["type"] == "function_call":
                        item = message["item"]
                        call_id = item.get("call_id")
                        if call_id and call_id not in tools_pending:
                            tools_pending[call_id] = RTToolCall(call_id, "")
                        updated_message = None

                case "conversation.item.created" | "conversation.item.added":
                    if "item" in message and message["item"]["type"] == "function_call":
                        item = message["item"]
                        if item["call_id"] not in tools_pending:
                            tools_pending[item["call_id"]] = RTToolCall(item["call_id"], message.get("previous_item_id", ""))
                        else:
                            # Upgrade fallback from output_item.added with the correct previous_item_id
                            tools_pending[item["call_id"]] = RTToolCall(item["call_id"], message.get("previous_item_id", ""))
                        updated_message = None
                    elif "item" in message and message["item"]["type"] == "function_call_output":
                        updated_message = None

                case "response.function_call_arguments.delta":
                    updated_message = None
                
                case "response.function_call_arguments.done":
                    updated_message = None

                case "response.output_item.done":
                    if "item" in message and message["item"]["type"] == "function_call":
                        item = message["item"]
                        tool_call = tools_pending.get(item["call_id"])
                        if tool_call is None:
                            logger.warning("Tool call %s not found in pending tools", item["call_id"])
                            updated_message = None
                        else:
                            tool = self.tools.get(item["name"])
                            if tool is None:
                                logger.error("Unknown tool requested: %s", item["name"])
                                updated_message = None
                            else:
                                args = item["arguments"]
                                if item["name"] in ["update_order", "get_order"]:
                                    result = await tool.target(json.loads(args), session_id)
                                else:
                                    result = await tool.target(json.loads(args))
                                await server_ws.send_json({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": item["call_id"],
                                        "output": result.to_text() if result.destination == ToolResultDirection.TO_SERVER else ""
                                    }
                                })
                                if result.destination == ToolResultDirection.TO_CLIENT:
                                    await client_ws.send_json({
                                        "type": "extension.middle_tier_tool_response",
                                        "previous_item_id": tool_call.previous_id,
                                        "tool_name": item["name"],
                                        "tool_result": result.to_text()
                                    })
                                updated_message = None

                case "response.done":
                    if tools_pending:
                        tools_pending.clear()
                        await server_ws.send_json({
                            "type": "response.create"
                        })
                    if "response" in message:
                        replace = False
                        try:
                            for i in range(len(message["response"]["output"]) - 1, -1, -1):
                                if message["response"]["output"][i]["type"] == "function_call":
                                    message["response"]["output"].pop(i)
                                    replace = True
                        except IndexError as e:
                            logging.error(f"Error processing message: {e}")
                        if replace:
                            updated_message = json.dumps(message)
                    if session_id is not None:
                        identifiers = order_state_singleton.advance_round_trip(session_id)
                        await self._emit_session_identifiers(client_ws, "extension.round_trip_token", identifiers)

        return updated_message

    async def _process_message_to_server(self, msg: str, ws: web.WebSocketResponse, voice_locked: bool = False) -> str | None:
        data = msg.data

        # FAST PATH: input_audio_buffer.append is the most frequent client message.
        # Skip JSON parse entirely — it never needs modification.
        m = _TYPE_RE.search(data)
        if m is not None and m.group(1) in _PASSTHROUGH_CLIENT_TYPES:
            return data

        message = json.loads(data)
        updated_message = data
        if message is not None:
            match message["type"]:
                case "session.update":
                    message["session"] = self._build_session(message["session"], voice_locked=voice_locked)
                    updated_message = json.dumps(message)

        return updated_message

    async def _forward_messages(self, ws: web.WebSocketResponse, client_request_id: str | None = None):
        async with aiohttp.ClientSession(base_url=self.endpoint) as session:
            params = {"model": self.deployment}
            headers = {}
            # Correlates our upstream call with the browser's request in AOAI logs.
            # (`ws.headers` are the *response* headers, so read it off the request.)
            if client_request_id:
                headers["x-ms-client-request-id"] = client_request_id
            if self.key is not None:
                headers["api-key"] = self.key
            else:
                headers["Authorization"] = f"Bearer {self._token_provider()}" # NOTE: no async version of token provider, maybe refresh token on a timer?
            async with session.ws_connect("/openai/v1/realtime", headers=headers, params=params) as target_ws:
                session_id = self._session_map.get(ws)
                greeting_sent = session_id in self._sent_greeting
                # Per-connection tool call tracking (avoids cross-session interference)
                tools_pending: dict[str, RTToolCall] = {}
                # Per-connection session state. GA locks the voice once the
                # conversation holds assistant audio; `session_configured` is
                # set by the first upstream `session.updated`.
                assistant_audio_seen = False
                session_configured = asyncio.Event()

                # Configure the upstream session before relaying a single
                # browser frame, so no socket ever runs on service defaults.
                await target_ws.send_str(self.build_bootstrap_session_update())

                async def send_greeting_once(trigger: str):
                    nonlocal greeting_sent
                    # Wait for the service to confirm our instructions/tools/voice
                    # before asking it to speak. Greeting on defaults would lock
                    # the default voice and let the browser's session.update be
                    # rejected with cannot_update_voice.
                    try:
                        await asyncio.wait_for(session_configured.wait(), timeout=_SESSION_CONFIGURED_TIMEOUT_SEC)
                    except TimeoutError:
                        logger.warning("No session.updated within %.1fs — greeting anyway (trigger=%s)",
                                       _SESSION_CONFIGURED_TIMEOUT_SEC, trigger)
                    greeting_sent = True
                    if session_id is not None:
                        self._sent_greeting.add(session_id)
                    await target_ws.send_json({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": _GREETING_TEXT}
                            ]
                        }
                    })
                    await target_ws.send_json({"type": "response.create"})

                async def from_client_to_server():
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            # Intercept extension.set_voice — don't forward to OpenAI
                            if _MARKER_SET_VOICE in msg.data:
                                try:
                                    ext_msg = json.loads(msg.data)
                                    if ext_msg.get("type") == "extension.set_voice":
                                        new_voice = ext_msg.get("voice", "")
                                        if new_voice in _VALID_VOICES:
                                            previous_voice = self.voice_choice
                                            self.voice_choice = new_voice
                                            if assistant_audio_seen:
                                                # GA would reject the update (cannot_update_voice).
                                                logger.info("[VOICE] Voice change %s → %s applies next conversation "
                                                            "(assistant audio already present)", previous_voice, new_voice)
                                            else:
                                                logger.info("[VOICE] Voice change: %s → %s", previous_voice, new_voice)
                                                await target_ws.send_str(self.build_voice_update(new_voice))
                                        continue
                                except (json.JSONDecodeError, KeyError):
                                    pass

                            new_msg = await self._process_message_to_server(msg, ws, voice_locked=assistant_audio_seen)
                            if new_msg is not None:
                                await target_ws.send_str(new_msg)
                            # The browser has configured its session: greet once the
                            # service confirms. Awaited here so no browser audio is
                            # relayed ahead of the greeting.
                            if not greeting_sent and _MARKER_SESSION_UPDATE in msg.data:
                                await send_greeting_once("client-session.update")
                        else:
                            logger.warning("Unexpected message type from client: %s", msg.type)
                    
                    # Client disconnected — close Azure OpenAI connection quickly
                    if target_ws and not target_ws.closed:
                        logger.info("Closing OpenAI's realtime socket connection.")
                        try:
                            await asyncio.wait_for(target_ws.close(), timeout=3)
                        except TimeoutError:
                            logger.warning("Timed out closing Azure OpenAI connection")
                        
                async def from_server_to_client():
                    nonlocal assistant_audio_seen
                    async for msg in target_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = msg.data
                            if _MARKER_AUDIO_DELTA in data or _MARKER_AUDIO_DELTA_LEGACY in data:
                                assistant_audio_seen = True
                            elif _MARKER_SESSION_UPDATED in data:
                                session_configured.set()
                            new_msg = await self._process_message_to_client(msg, ws, target_ws, tools_pending)
                            if new_msg is not None:
                                if ws.closed:
                                    break
                                await ws.send_str(new_msg)
                        else:
                            logger.warning("Unexpected message type from server: %s", msg.type)

                try:
                    await asyncio.gather(from_client_to_server(), from_server_to_client())
                except (ConnectionResetError, ConnectionError,
                        aiohttp.ClientError, asyncio.CancelledError):
                    # Ignore errors from the client disconnecting (e.g. browser refresh)
                    pass
                except Exception:
                    logger.exception("Unexpected error in realtime message forwarding")
                finally:
                    if session_id is not None:
                        order_state_singleton.delete_session(session_id)
                        self._sent_greeting.discard(session_id)
                    # Clean up the session map when the connection is closed
                    if ws in self._session_map:
                        del self._session_map[ws]

    async def _websocket_handler(self, request: web.Request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        # Create a new session for each WebSocket connection
        session_id = order_state_singleton.create_session()
        self._session_map[ws] = session_id

        await self._forward_messages(ws, client_request_id=request.headers.get("x-ms-client-request-id"))
        return ws
    
    def attach_to_app(self, app: web.Application, path: str) -> None:
        app.router.add_get(path, self._websocket_handler)
