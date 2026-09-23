import asyncio
import copy
import json
import logging
import os
import re
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from enum import Enum
from typing import Any

import aiohttp
from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

from config_loader import get_config
from order_state import SessionIdentifiers, order_state_singleton
from session_manager import (
    SESSION_ENDED_CLOSE_CODE,
    SESSION_ENDED_CLOSE_REASON,
    SUPERSEDED_CLOSE_CODE,
    SUPERSEDED_CLOSE_REASON,
    SessionManager,
    resume_id_fingerprint,
)

logger = logging.getLogger("coffee-chat")

__all__ = ["RTMiddleTier", "RTToolCall", "RateLimitSettings", "Tool", "ToolResult", "ToolResultDirection",
           "configure_realtime_model", "deployment_supports_reasoning", "is_rate_limit_error",
           "normalize_reasoning_effort", "parse_reasoning_model", "parse_retry_hint"]


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
# `reasoning` ({effort}) and `parallel_tool_calls` exist only for reasoning
# realtime models (gpt-realtime-2 / 2.1). gpt-realtime-1.5 rejects the whole
# session.update if they are present, so RTMiddleTier only sets them when the
# deployment is a reasoning model (see `RTMiddleTier._reasoning_model`).
_GA_SESSION_TOP_LEVEL = frozenset({
    "type", "model", "instructions", "tools", "tool_choice",
    "max_output_tokens", "output_modalities", "audio", "tracing",
    "include", "prompt", "truncation",
    "reasoning", "parallel_tool_calls",
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
_MARKER_AUDIO_APPEND = '"input_audio_buffer.append"'
_MARKER_SPEECH_STARTED = '"input_audio_buffer.speech_started"'
_MARKER_TRANSCRIPTION_COMPLETED = '"conversation.item.input_audio_transcription.completed"'
_MARKER_RESUME = '"extension.resume"'
_MARKER_END_SESSION = '"extension.end_session"'
_MARKER_RESPONSE_CREATE = '"response.create"'

# Fire-and-forget tasks (e.g. closing a superseded socket) kept alive until done.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _close_superseded(stale_ws: web.WebSocketResponse) -> None:
    try:
        await stale_ws.close(code=SUPERSEDED_CLOSE_CODE, message=SUPERSEDED_CLOSE_REASON.encode())
    except Exception:
        pass


def _extension_type(data: str, marker: str) -> str | None:
    """The `type` of a client frame that contains `marker`, else None (cheap for audio frames)."""
    if marker not in data:
        return None
    try:
        message = json.loads(data)
    except ValueError:
        return None
    return message.get("type") if isinstance(message, dict) else None


def _presented_resume_id(data: str) -> object:
    try:
        message = json.loads(data)
    except ValueError:
        return None
    return message.get("resume_id") if isinstance(message, dict) else None

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

# permessage-deflate on the browser socket; off unless config.yaml
# `connection.ws_compression` is true. aiohttp 3.14.2/3.14.3 kill the socket
# (1002) on the first compressed frame after an initial PONG (aio-libs/aiohttp#13274).
def ws_compression_enabled(cfg: dict) -> bool:
    return bool((cfg.get("connection") or {}).get("ws_compression", False))


_WS_COMPRESS = ws_compression_enabled(get_config())

_GREETING_TEXT = "Please greet the guest with: 'Welcome to Dunkin! How may I help you today?'"


# Values accepted by gpt-realtime-2.1 for `reasoning.effort`.
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
# Config values that mean "do not send `reasoning` at all".
_REASONING_DISABLED_VALUES = frozenset({"", "off", "disabled", "false", "null"})

# Realtime model families that are NOT reasoning models. gpt-realtime-1.5 answers
# `reasoning` (any effort, even "none") and `parallel_tool_calls: true` with
# `invalid_value` "Unsupported option for this model" -- and drops the whole
# session.update, tools included. The dated `gpt-realtime-2025-08-28` snapshot is
# the original non-reasoning gpt-realtime, not gpt-realtime-2.
_NON_REASONING_DEPLOYMENT_RE = re.compile(
    r"^(gpt-4o.*|gpt-realtime(-mini.*|-1(\.\d+)?(-.*)?|-\d{4}-\d{2}-\d{2})?)$",
    re.IGNORECASE,
)


def deployment_supports_reasoning(deployment: str | None) -> bool:
    """Best-effort check from the deployment name, used only when
    `model.reasoning_model` is "auto". azd names deployments after the model, so
    a rollback to `gpt-realtime-1.5` is recognised. Unrecognised custom names
    are assumed to support reasoning; if they don't, the rejected session.update
    is caught by the fallback in RTMiddleTier and reasoning is switched off for
    the rest of the process."""
    if not isinstance(deployment, str) or not deployment.strip():
        return True
    return _NON_REASONING_DEPLOYMENT_RE.match(deployment.strip()) is None


def parse_reasoning_model(value: Any) -> bool | None:
    """`model.reasoning_model` / AZURE_OPENAI_REALTIME_REASONING_MODEL:
    True / False force it; None ("auto", empty, unknown) infers it from the
    deployment name."""
    if isinstance(value, bool):
        return value
    text = "" if value is None else str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    if text not in ("", "auto", "null", "none"):
        logger.warning("Ignoring unknown reasoning_model %r (expected auto|true|false)", value)
    return None


def normalize_reasoning_effort(value: Any) -> str | None:
    """Map a configured effort to the wire value, or None to omit `reasoning`.

    Empty / "off" / "disabled" omit the field (YAML `off` parses to False,
    which lands here as "false"). "none" is a real effort level on
    gpt-realtime-2.1 (no reasoning tokens) and is sent as-is.
    """
    if value is None:
        return None
    effort = str(value).strip().lower()
    if effort in _REASONING_DISABLED_VALUES:
        return None
    if effort not in REASONING_EFFORTS:
        logger.warning("Ignoring unknown reasoning effort %r (expected one of %s)", value, sorted(REASONING_EFFORTS))
        return None
    return effort


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


def _new_event_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# The fallback carries only what the conversation cannot work without. No voice
# (cannot_update_voice), no audio config, no reasoning -- the usual suspects when
# GA rejects an update.
_FALLBACK_SESSION_KEYS = ("type", "instructions", "tools", "tool_choice")


class _SessionUpdateGuard:
    """Tracks the session.updates sent on ONE upstream socket so a rejection can
    be correlated back to them.

    GA rejects an invalid session.update wholesale and reports it only as an
    `error` event. Most rejections echo our `event_id` in `error.event_id`, but
    some do not (gpt-realtime-1.5 rejecting `reasoning` returns no event_id and
    no param; `cannot_update_voice` has neither either), so an uncorrelated
    invalid_request_error that arrives while one of our updates is still
    unacknowledged is attributed to the oldest one -- the service processes
    client events in order.
    """

    _MAX_TRACKED = 64

    def __init__(self) -> None:
        # event_id -> event_id of the original if this is a fallback, else None
        self._sent: OrderedDict[str, str | None] = OrderedDict()
        self._payloads: dict[str, dict] = {}
        self._in_flight: deque[str] = deque()
        self._fallback_sent_for: set[str] = set()
        # Result of the latest correlate() call.
        self.last_correlated: str | None = None

    def stamp(self, message: dict, fallback_of: str | None = None) -> dict:
        """Ensure `message` carries an event_id and start tracking it."""
        event_id = message.get("event_id") or _new_event_id("dunkin_fallback" if fallback_of else "dunkin_su")
        message["event_id"] = event_id
        self._sent[event_id] = fallback_of
        self._payloads[event_id] = message.get("session") or {}
        self._in_flight.append(event_id)
        while len(self._sent) > self._MAX_TRACKED:
            old, _ = self._sent.popitem(last=False)
            self._payloads.pop(old, None)
        return message

    def track(self, payload: str, fallback_of: str | None = None) -> str:
        """`stamp` for an already-serialised session.update."""
        message = json.loads(payload)
        had_id = bool(message.get("event_id"))
        self.stamp(message, fallback_of)
        return payload if had_id else json.dumps(message)

    def on_session_updated(self) -> None:
        if self._in_flight:
            self._in_flight.popleft()

    def correlate(self, error_event: dict) -> str | None:
        """Return the event_id of our session.update this error rejects, or None."""
        self.last_correlated = self._correlate(error_event)
        return self.last_correlated

    def _correlate(self, error_event: dict) -> str | None:
        err = error_event.get("error") or {}
        event_id = err.get("event_id")
        if event_id:
            if event_id not in self._sent:
                return None
            try:
                self._in_flight.remove(event_id)
            except ValueError:
                pass
            return event_id
        param = err.get("param") or ""
        if (self._in_flight and err.get("type") == "invalid_request_error"
                and (not param or param.startswith("session"))):
            return self._in_flight.popleft()
        return None

    def original_of(self, event_id: str) -> str | None:
        return self._sent.get(event_id)

    def payload_of(self, event_id: str) -> dict:
        return self._payloads.get(event_id, {})

    def claim_fallback(self, event_id: str) -> bool:
        """True exactly once per original session.update."""
        if event_id in self._fallback_sent_for:
            return False
        self._fallback_sent_for.add(event_id)
        return True


# ── Rate-limit recovery ────────────────────────────────────────────────────
# The demos share Azure OpenAI quota. A rate-limited response produces no
# output, so without this the guest just hears silence.

_RETRY_HINT_RE = re.compile(
    r"(?:try\s+again|retry)\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?)\b",
    re.IGNORECASE)
_FIRST_RETRY_CLAMP = (0.5, 5.0)
_SECOND_RETRY_CLAMP = (2.0, 8.0)


def parse_retry_hint(message: Any) -> float | None:
    """Seconds from "try again in 2.5s" / "retry after 800 ms"; None if absent."""
    match = _RETRY_HINT_RE.search(message) if isinstance(message, str) else None
    if match is None:
        return None
    value = float(match.group(1))
    return value / 1000.0 if match.group(2).lower().startswith("m") else value


def is_rate_limit_error(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    return any("rate_limit" in str(error.get(key) or "").lower() for key in ("code", "type"))


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


class RateLimitSettings:
    def __init__(self, enabled: bool = True, retry_delay: float = 1.5, second_retry_delay: float = 4.0,
                 max_retries: int = 2) -> None:
        self.enabled = enabled
        self.retry_delay = retry_delay
        self.second_retry_delay = second_retry_delay
        self.max_retries = max_retries

    @classmethod
    def from_config(cls, cfg: dict | None, environ: Any = None) -> "RateLimitSettings":
        """`resilience.rate_limit` from config.yaml; RATE_LIMIT_RECOVERY_ENABLED overrides `enabled`."""
        env = os.environ if environ is None else environ
        cfg = cfg or {}
        enabled = bool(cfg.get("enabled", True))
        override = (env.get("RATE_LIMIT_RECOVERY_ENABLED") or "").strip().lower()
        if override:
            enabled = override in {"1", "true", "yes", "on"}
        return cls(enabled=enabled,
                   retry_delay=float(cfg.get("retry_delay_seconds", 1.5)),
                   second_retry_delay=float(cfg.get("second_retry_delay_seconds", 4)),
                   max_retries=int(cfg.get("max_retries", 2)))


class _RateLimitRecovery:
    """Retry ladder for ONE upstream socket, per failed response:

    1st rate-limited failure -> silent `response.create` after `retry_delay`
    2nd -> browser `extension.rate_limited` attempt 1 (apology clip) + retry 2
    3rd -> browser `extension.rate_limited` final; no more retries.

    A pending retry is dropped if the guest starts speaking or another response
    starts: VAD creates a fresh response, and a stale one must not stack on it.
    Only `response.create` is resent, never a tool call, so a tool follow-up
    that is retried doesn't run the tool again.
    """

    def __init__(self, settings: RateLimitSettings, sleep: Callable[[float], Any] = asyncio.sleep,
                 session_id: str | None = None) -> None:
        self.settings = settings
        self._sleep = sleep
        self._session_id = session_id
        self.failures = 0
        self._retry: asyncio.Task | None = None

    @property
    def retry_pending(self) -> bool:
        return self._retry is not None and not self._retry.done()

    def cancel(self, reason: str) -> bool:
        """Drop a pending retry; True if one was pending."""
        if not self.retry_pending:
            return False
        self._retry.cancel()
        self._retry = None
        logger.info("Rate-limit retry cancelled: %s (session=%s)", reason, self._session_id)
        return True

    def on_guest_speech(self) -> None:
        self.cancel("guest started speaking")
        self.failures = 0

    def on_response_created(self) -> None:
        # Our own retry's response.created arrives after the retry was sent, so
        # a still-pending retry means someone else (VAD) started this response.
        if self.cancel("a new response started"):
            self.failures = 0

    def on_response_finished(self) -> None:
        self.failures = 0

    async def on_rate_limited(self, error: dict, server_ws, client_ws) -> None:
        self.cancel("superseded by a new rate-limit failure")
        self.failures += 1
        attempt = self.failures
        hint = parse_retry_hint(error.get("message"))
        logger.warning("Response rate-limited (failure %d/%d): code=%s type=%s retry_hint=%s message=%s (session=%s)",
                       attempt, self.settings.max_retries + 1, error.get("code"), error.get("type"),
                       f"{hint:g}s" if hint is not None else None, error.get("message"), self._session_id)
        if attempt > self.settings.max_retries:
            logger.warning("Rate-limit retries exhausted; asking the guest to repeat (session=%s)", self._session_id)
            self.failures = 0
            await self._notify(client_ws, {"type": "extension.rate_limited", "attempt": self.settings.max_retries,
                                           "final": True})
            return
        if attempt == 1:
            delay = _clamp(hint, _FIRST_RETRY_CLAMP) if hint is not None else self.settings.retry_delay
        else:
            await self._notify(client_ws, {"type": "extension.rate_limited", "attempt": attempt - 1})
            delay = _clamp(hint, _SECOND_RETRY_CLAMP) if hint is not None else self.settings.second_retry_delay
        self._retry = asyncio.ensure_future(self._retry_after(delay, server_ws, attempt))

    @staticmethod
    async def _notify(client_ws, event: dict) -> None:
        if getattr(client_ws, "closed", False) is not True:
            await client_ws.send_json(event)

    async def _retry_after(self, delay: float, server_ws, attempt: int) -> None:
        await self._sleep(delay)
        if getattr(server_ws, "closed", False) is True:
            return
        logger.warning("Retrying rate-limited response (retry %d after %.2fs, session=%s)",
                       attempt, delay, self._session_id)
        await server_ws.send_json({"type": "response.create"})


class ToolResultDirection(Enum):
    TO_SERVER = 1
    TO_CLIENT = 2

class ToolResult:
    text: str
    destination: ToolResultDirection
    server_text: str | None

    def __init__(self, text: str, destination: ToolResultDirection, server_text: str | None = None):
        self.text = text
        self.destination = destination
        # What the model sees for a TO_CLIENT result (the browser gets `text`).
        self.server_text = server_text

    def to_text(self) -> str:
        if self.text is None:
            return ""
        return self.text if isinstance(self.text, str) else json.dumps(self.text)

    def model_output(self) -> str:
        """The function_call_output sent back to the model."""
        if self.destination == ToolResultDirection.TO_SERVER:
            return self.to_text()
        return self.server_text or ""

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
    # Input transcription model (a deployment name on Azure). None keeps the
    # client's value.
    transcription_model: str | None = None
    # reasoning.effort for reasoning realtime models; None omits the field.
    reasoning_effort: str | None = None
    parallel_tool_calls: bool | None = None
    # Whether the deployment is a reasoning model (accepts `reasoning` and
    # `parallel_tool_calls`). None = infer from the deployment name.
    reasoning_model: bool | None = None

    def __init__(self, endpoint: str, deployment: str, credentials: AzureKeyCredential | DefaultAzureCredential, voice_choice: str | None = None):
        self.endpoint = endpoint
        self.deployment = deployment
        self.voice_choice = voice_choice
        self.tools = {}
        self._token_provider = None
        # Socket -> order session, guest activity and the idle close (4000).
        self._sessions = SessionManager()
        # Flipped if the deployment rejects `reasoning` at runtime despite the
        # name check / switch; from then on it is never sent again.
        self._reasoning_rejected = False
        # Rate-limit retry ladder (config.yaml resilience.rate_limit); `_sleep`
        # is injectable so tests don't really wait.
        self.rate_limit = RateLimitSettings()
        self._sleep: Callable[[float], Any] = asyncio.sleep
        if voice_choice is not None:
            logger.info("Realtime voice choice set to %s", voice_choice)
        if isinstance(credentials, AzureKeyCredential):
            self.key = credentials.key
        else:
            self._token_provider = get_bearer_token_provider(credentials, "https://cognitiveservices.azure.com/.default")
            self._token_provider() # Warm up during startup so we have a token cached when the first request arrives

    @property
    def sessions(self) -> SessionManager:
        return self._sessions

    @property
    def _session_map(self) -> dict[web.WebSocketResponse, str]:
        return self._sessions._session_map

    def _reasoning_model(self) -> bool:
        """Whether reasoning-model-only fields may be sent upstream at all.

        A runtime rejection always wins; then the explicit `reasoning_model`
        switch; then the deployment-name check."""
        if self._reasoning_rejected:
            return False
        if self.reasoning_model is not None:
            return self.reasoning_model
        return deployment_supports_reasoning(self.deployment)

    def reasoning_enabled(self) -> bool:
        """Whether `reasoning` will be sent upstream."""
        return normalize_reasoning_effort(self.reasoning_effort) is not None and self._reasoning_model()

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
        if self.transcription_model:
            transcription = session.get("input_audio_transcription")
            session["input_audio_transcription"] = {
                **(transcription if isinstance(transcription, dict) else {}),
                "model": self.transcription_model,
            }
        # Server-owned: never trust a client-supplied value for these, since
        # an unsupported one takes the tools down with it.
        session.pop("reasoning", None)
        session.pop("parallel_tool_calls", None)
        if self._reasoning_model():
            if (effort := normalize_reasoning_effort(self.reasoning_effort)) is not None:
                session["reasoning"] = {"effort": effort}
            if self.parallel_tool_calls is not None:
                session["parallel_tool_calls"] = bool(self.parallel_tool_calls)
        # Translate to the GA shape so the browser contract is unchanged
        # and unsupported legacy keys are dropped rather than rejected.
        ga_session = _to_ga_session(session)
        if voice_locked and _strip_output_voice(ga_session):
            logger.info("session.update: assistant audio already present — omitting voice so the update is not rejected")
        return ga_session

    def build_bootstrap_session_update(self, event_id: str | None = None) -> str:
        """Serialise the session.update sent as the very first frame on every
        upstream socket, before any browser traffic is relayed.

        Without it the upstream session runs on service defaults (no tools,
        generic instructions, server VAD auto-responding) until the browser's
        own session.update arrives -- and if the model speaks in that window the
        voice locks and our later session.update is rejected wholesale, so the
        tools are never registered for that conversation.
        """
        session = self._build_session(copy.deepcopy(_BOOTSTRAP_CLIENT_SESSION))
        return json.dumps({"type": "session.update", "event_id": event_id or _new_event_id("dunkin_bootstrap"),
                           "session": session})

    def build_voice_update(self, voice: str, event_id: str | None = None) -> str:
        """Serialise a voice-only session.update in the GA shape."""
        return json.dumps({"type": "session.update", "event_id": event_id or _new_event_id("dunkin_voice"),
                           "session": _to_ga_session({"voice": voice})})

    def build_fallback_session_update(self, event_id: str | None = None) -> str:
        """Serialise the minimal session.update sent when GA rejects one of ours.

        Only `type`, `instructions`, `tools` and `tool_choice` -- whatever field
        got the original rejected, the crew member keeps its tools and persona.
        """
        full = self._build_session({})
        # The key filter drops `audio` (voice included) and `reasoning`.
        session = {key: full[key] for key in _FALLBACK_SESSION_KEYS if key in full}
        return json.dumps({"type": "session.update", "event_id": event_id or _new_event_id("dunkin_fallback"),
                           "session": session})

    async def _recover_rejected_session_update(self, message: dict, server_ws, guard: "_SessionUpdateGuard | None",
                                               session_id: str | None) -> bool:
        """Handle an upstream `error` that rejects one of our session.updates.

        Returns True if the error was consumed (a fallback was sent), False if
        it should reach the browser: unrelated errors, and a rejected fallback.
        """
        if guard is None:
            return False
        event_id = guard.correlate(message)
        if event_id is None:
            return False
        err = message.get("error") or {}
        code, param, text = err.get("code"), err.get("param"), err.get("message")
        original = guard.original_of(event_id)
        if original is not None or not guard.claim_fallback(event_id):
            logger.error(
                "Fallback session.update %s (for %s) was ALSO rejected: code=%s param=%s message=%s -- "
                "tools may NOT be registered for this conversation (session=%s)",
                event_id, original, code, param, text, session_id)
            return False
        logger.error(
            "Upstream REJECTED session.update %s: code=%s param=%s message=%s -- resending a minimal "
            "session.update (instructions + tools only) so the tools survive (session=%s)",
            event_id, code, param, text, session_id)
        rejected = guard.payload_of(event_id)
        if (("reasoning" in rejected or "parallel_tool_calls" in rejected)
                and (not param or param.startswith(("session.reasoning", "session.parallel_tool_calls")))):
            self._reasoning_rejected = True
            logger.error("Deployment %s rejected reasoning-model options; no longer sending `reasoning` / "
                         "`parallel_tool_calls` from this process. Set model.reasoning_effort to \"\" for this "
                         "deployment.", getattr(self, "deployment", "?"))
        fallback = guard.track(self.build_fallback_session_update(), fallback_of=event_id)
        await server_ws.send_str(fallback)
        return True

    async def _emit_session_identifiers(
        self,
        client_ws: web.WebSocketResponse,
        event_type: str,
        identifiers: SessionIdentifiers | None,
        extra: dict | None = None,
    ) -> None:
        if identifiers is None:
            return
        await client_ws.send_json(
            {
                "type": event_type,
                "sessionToken": identifiers.session_token,
                "roundTripIndex": identifiers.round_trip_index,
                "roundTripToken": identifiers.round_trip_token,
                **(extra or {}),
            }
        )

    def new_rate_limit_recovery(self, session_id: str | None = None) -> "_RateLimitRecovery | None":
        if not self.rate_limit.enabled:
            return None
        return _RateLimitRecovery(self.rate_limit, sleep=self._sleep, session_id=session_id)

    async def _process_message_to_client(self, msg: str, client_ws: web.WebSocketResponse, server_ws: web.WebSocketResponse, tools_pending: dict[str, "RTToolCall"], guard: "_SessionUpdateGuard | None" = None, recovery: "_RateLimitRecovery | None" = None,
                                         on_session_created: Callable[[], Any] | None = None) -> str | None:
        data = msg.data

        # FAST PATH: extract type via regex without full JSON parse.
        # Audio deltas are ~95% of server messages — avoid json.loads entirely.
        m = _TYPE_RE.search(data)
        if m is not None and m.group(1) in _PASSTHROUGH_SERVER_TYPES:
            # Translate GA event names to legacy names for client compatibility
            event_type = m.group(1)
            if recovery is not None and event_type == "input_audio_buffer.speech_started":
                recovery.on_guest_speech()
            legacy_name = _GA_TO_LEGACY_EVENTS.get(event_type)
            if legacy_name is not None:
                data = data.replace(f'"{event_type}"', f'"{legacy_name}"', 1)
            return data

        message = json.loads(data)
        updated_message = data
        session_id = self._session_map.get(client_ws)
        if message is not None:
            match message["type"]:
                case "error":
                    # A rejected session.update of ours is recovered here (minimal
                    # fallback) instead of surfacing as a user-facing failure.
                    if await self._recover_rejected_session_update(message, server_ws, guard, session_id):
                        return None
                    correlated = guard is not None and guard.last_correlated is not None
                    if recovery is not None and not correlated and is_rate_limit_error(message.get("error")):
                        await recovery.on_rate_limited(message.get("error") or {}, server_ws, client_ws)
                        return None
                    logger.error("OpenAI Realtime API error: %s", json.dumps(message, default=str)[:1000])

                case "response.created":
                    if recovery is not None:
                        recovery.on_response_created()

                case "conversation.item.input_audio_transcription.failed":
                    # e.g. DeploymentNotFound when the configured transcription
                    # model has no Azure deployment: the session.update was
                    # accepted, but no guest speech is ever transcribed.
                    logger.error("Input audio transcription failed (model=%s): %s", self.transcription_model,
                                 json.dumps(message.get("error"), default=str)[:500])

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
                    if on_session_created is not None:
                        # The forwarder announces the session (metadata or resume)
                        # once it knows whether this socket is resuming.
                        await on_session_created()
                    elif session_id is not None:
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
                                        "output": result.model_output()
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
                    response = message.get("response") or {}
                    failure = (response.get("status_details") or {}).get("error") \
                        if response.get("status") == "failed" else None
                    if recovery is not None and is_rate_limit_error(failure):
                        # No output was produced; any tool outputs are already in
                        # the conversation, so the retry alone is the follow-up.
                        tools_pending.clear()
                        await recovery.on_rate_limited(failure, server_ws, client_ws)
                    elif recovery is not None:
                        recovery.on_response_finished()
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
                    # Remember what the crew member said, for rehydrating a resumed session.
                    if session_id is not None and "response" in message:
                        spoken = " ".join(
                            (content.get("transcript") or content.get("text") or "").strip()
                            for out_item in message["response"].get("output") or []
                            if out_item.get("type") == "message"
                            for content in out_item.get("content") or [])
                        self._sessions.record_turn(session_id, "crew", spoken)
                    if session_id is not None:
                        identifiers = order_state_singleton.advance_round_trip(session_id)
                        await self._emit_session_identifiers(client_ws, "extension.round_trip_token", identifiers)

        return updated_message

    async def _process_message_to_server(self, msg: str, ws: web.WebSocketResponse, voice_locked: bool = False, guard: "_SessionUpdateGuard | None" = None) -> str | None:
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
                    # Every session.update carries an event_id so a rejection can
                    # be correlated and recovered (see _recover_rejected_session_update).
                    if guard is not None:
                        guard.stamp(message)
                    else:
                        message.setdefault("event_id", _new_event_id("dunkin_su"))
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
            # compress=0: Azure OpenAI declines deflate anyway; don't offer it. (aiohttp's
            # current default, pinned so a future default change can't turn it on.)
            async with session.ws_connect("/openai/v1/realtime", headers=headers, params=params, compress=0) as target_ws:
                session_id = self._sessions.get_session_id(ws)
                greeting_sent = self._sessions.has_sent_greeting(session_id)
                # Per-connection tool call tracking (avoids cross-session interference)
                tools_pending: dict[str, RTToolCall] = {}
                # Per-connection session state. GA locks the voice once the
                # conversation holds assistant audio; `session_configured` is
                # set by the first upstream `session.updated`.
                assistant_audio_seen = False
                session_configured = asyncio.Event()
                guard = _SessionUpdateGuard()
                recovery = self.new_rate_limit_recovery(session_id)

                # ── Resume handshake (one decision per socket) ──
                # A resume is honoured only as the first client frame. Until that
                # decision is made (first frame, or first_frame_timeout), the
                # announcement is held back so a socket gets exactly one of
                # extension.session_metadata / extension.session_resumed.
                resume_decided = asyncio.Event()
                upstream_created = False
                announced = False
                # Silent-guest nudge after a mid-conversation resume (once per resume).
                nudge_task: asyncio.Task | None = None

                # Configure the upstream session before relaying a single
                # browser frame, so no socket ever runs on service defaults.
                await target_ws.send_str(guard.track(self.build_bootstrap_session_update()))

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
                    self._sessions.mark_greeting_sent(session_id)
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

                async def announce_fresh():
                    """Send extension.session_metadata (with a resume id) once the resume
                    decision is made and the upstream session exists."""
                    nonlocal announced
                    if announced or not resume_decided.is_set() or not upstream_created or session_id is None:
                        return
                    announced = True
                    identifiers = order_state_singleton.get_session_identifiers(session_id)
                    resume_id = self._sessions.issue_resume_id(session_id)
                    await self._emit_session_identifiers(ws, "extension.session_metadata", identifiers,
                                                         extra={"resumeId": resume_id} if resume_id else None)

                async def on_session_created():
                    nonlocal upstream_created
                    upstream_created = True
                    await announce_fresh()

                async def first_frame_deadline():
                    await asyncio.sleep(self._sessions.first_frame_timeout_seconds)
                    if not resume_decided.is_set():
                        resume_decided.set()
                        await announce_fresh()

                async def nudge_after_silence():
                    """If the guest says nothing for nudge_after_seconds after a resume, have
                    the crew member ask once whether they need anything else. Waits for the
                    same session.updated confirmation as the greeting. Not guest activity."""
                    await asyncio.sleep(self._sessions.nudge_after_seconds)
                    await session_configured.wait()
                    if recovery is not None and recovery.retry_pending:
                        # A rate-limited response is about to be retried; a nudge now
                        # would stack a second response on top of it.
                        logger.info("Resume nudge skipped: a rate-limit retry is pending (session=%s)", session_id)
                        return
                    logger.info("Guest silent %.0fs after resume; crew nudges (session=%s)",
                                self._sessions.nudge_after_seconds, session_id)
                    await target_ws.send_str(self._sessions.build_nudge_item())
                    await target_ws.send_json({"type": "response.create"})

                def cancel_nudge(reason: str) -> None:
                    nonlocal nudge_task
                    if nudge_task is not None and not nudge_task.done():
                        nudge_task.cancel()
                        logger.info("Resume nudge cancelled: %s (session=%s)", reason, session_id)
                    nudge_task = None

                async def handle_resume(data: str):
                    nonlocal session_id, announced, greeting_sent, nudge_task
                    presented = _presented_resume_id(data)
                    outcome = self._sessions.resume(ws, presented)
                    resume_decided.set()
                    if not outcome.accepted:
                        logger.info("Resume rejected (reason=%s, resume id %s); starting fresh session %s",
                                    outcome.reason, resume_id_fingerprint(presented), session_id)
                        await ws.send_json({"type": "extension.resume_rejected", "reason": outcome.reason})
                        await announce_fresh()
                        return
                    session_id = outcome.session_id
                    if recovery is not None:
                        recovery._session_id = session_id
                    if outcome.stale_ws is not None:
                        _spawn(_close_superseded(outcome.stale_ws))
                    identifiers = order_state_singleton.get_session_identifiers(session_id)
                    announced = True
                    await ws.send_json({
                        "type": "extension.session_resumed",
                        "order_summary": json.loads(order_state_singleton.get_order_summary(session_id).model_dump_json()),
                        "session_token": identifiers.session_token,
                        "round_trip_index": identifiers.round_trip_index,
                        "round_trip_token": identifiers.round_trip_token,
                        "resume_id": outcome.resume_id,
                    })
                    if not outcome.conversation_started:
                        return                  # never greeted: the normal greeting still runs
                    # Mid-conversation: no greeting, no "welcome back". Brief the new
                    # upstream (after the bootstrap session.update, before any
                    # response.create) and stay silent until the guest speaks.
                    greeting_sent = True
                    await target_ws.send_str(self._sessions.build_rehydration_item(session_id))
                    logger.info("Resumed session %s rehydrated (%d recent turns); greeting suppressed",
                                session_id, len(self._sessions.recent_turns(session_id)))
                    if self._sessions.nudge_after_seconds > 0:
                        nudge_task = asyncio.ensure_future(nudge_after_silence())

                async def reject_late_resume(data: str):
                    nonlocal announced
                    logger.info("Resume rejected (reason=not_first_frame, resume id %s); session %s continues",
                                resume_id_fingerprint(_presented_resume_id(data)), session_id)
                    await ws.send_json({"type": "extension.resume_rejected", "reason": "not_first_frame"})
                    # The browser drops its stored id on any rejection, so re-announce
                    # this socket's own session (with a rotated id) if already announced.
                    if announced:
                        announced = False
                        await announce_fresh()

                async def from_client_to_server():
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            # Resume handshake: only the very first client frame may resume.
                            if not resume_decided.is_set():
                                if _extension_type(msg.data, _MARKER_RESUME) == "extension.resume":
                                    await handle_resume(msg.data)
                                    continue
                                resume_decided.set()
                                await announce_fresh()
                            # Resume frames are not guest activity: a drop must not
                            # extend the idle budget.
                            if _extension_type(msg.data, _MARKER_RESUME) == "extension.resume":
                                await reject_late_resume(msg.data)
                                continue
                            if _extension_type(msg.data, _MARKER_END_SESSION) == "extension.end_session":
                                logger.info("Guest ended session %s", session_id)
                                self._sessions.end_session(session_id, "guest ended the session")
                                await ws.close(code=SESSION_ENDED_CLOSE_CODE, message=SESSION_ENDED_CLOSE_REASON.encode())
                                break
                            # Guest activity drives the idle clock. Mic frames stream
                            # constantly (silence included), so they don't count; the
                            # guest actually speaking does (speech_started/transcripts
                            # from upstream), and so does any control frame (a tap).
                            if _MARKER_AUDIO_APPEND not in msg.data:
                                self._sessions.touch_activity(session_id)
                                if _MARKER_RESPONSE_CREATE in msg.data:
                                    cancel_nudge("guest-initiated response")
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
                                                await target_ws.send_str(guard.track(self.build_voice_update(new_voice)))
                                        continue
                                except (json.JSONDecodeError, KeyError):
                                    pass

                            new_msg = await self._process_message_to_server(msg, ws, voice_locked=assistant_audio_seen,
                                                                            guard=guard)
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
                                guard.on_session_updated()
                                session_configured.set()
                            elif _MARKER_SPEECH_STARTED in data:
                                self._sessions.touch_activity(session_id)
                                cancel_nudge("guest speech")
                            elif _MARKER_TRANSCRIPTION_COMPLETED in data:
                                self._sessions.touch_activity(session_id)
                                cancel_nudge("guest transcript")
                                try:
                                    self._sessions.record_turn(session_id, "guest", json.loads(data).get("transcript"))
                                except (ValueError, AttributeError):
                                    pass
                            new_msg = await self._process_message_to_client(msg, ws, target_ws, tools_pending, guard,
                                                                            recovery, on_session_created=on_session_created)
                            if new_msg is not None:
                                if ws.closed:
                                    break
                                await ws.send_str(new_msg)
                        else:
                            logger.warning("Unexpected message type from server: %s", msg.type)

                deadline_task = asyncio.ensure_future(first_frame_deadline())
                try:
                    await asyncio.gather(from_client_to_server(), from_server_to_client())
                except (ConnectionResetError, ConnectionError,
                        aiohttp.ClientError, asyncio.CancelledError):
                    # Ignore errors from the client disconnecting (e.g. browser refresh)
                    pass
                except Exception:
                    logger.exception("Unexpected error in realtime message forwarding")
                finally:
                    deadline_task.cancel()
                    cancel_nudge("socket closed")
                    if recovery is not None:
                        recovery.cancel("connection closed")
                    self._sessions.detach_session(ws, session_id, reason=f"client close code={ws.close_code}")

    async def _websocket_handler(self, request: web.Request):
        ws = web.WebSocketResponse(compress=_WS_COMPRESS)
        await ws.prepare(request)

        # A new order session for each WebSocket connection.
        self._sessions.create_session(ws)

        try:
            await self._forward_messages(ws, client_request_id=request.headers.get("x-ms-client-request-id"))
        finally:
            # Covers an upstream connect failure, which never reaches the
            # forwarder's own cleanup. A no-op if that already ran.
            self._sessions.detach_session(ws, self._sessions.get_session_id(ws),
                                          reason=f"handler exit code={ws.close_code}")
        return ws

    async def _start_background_tasks(self, _app: web.Application) -> None:
        self._sessions.start_idle_checker()

    async def _stop_background_tasks(self, _app: web.Application) -> None:
        await self._sessions.stop_idle_checker()

    def attach_to_app(self, app: web.Application, path: str) -> None:
        app.router.add_get(path, self._websocket_handler)
        app.on_startup.append(self._start_background_tasks)
        app.on_cleanup.append(self._stop_background_tasks)


def configure_realtime_model(rtmt: RTMiddleTier, model_cfg: dict, environ: Any = None) -> RTMiddleTier:
    """Apply the reasoning / transcription settings from `config.yaml` `model:`
    plus their env overrides to `rtmt`. An empty env value means "use config".

    Shared by app.py and scripts/smoke_realtime.py so the smoke check sends
    exactly the session the app sends.
    """
    env = os.environ if environ is None else environ
    rtmt.transcription_model = (env.get("AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL")
                                or model_cfg.get("transcription_model") or "whisper-1")
    effort = env.get("AZURE_OPENAI_REALTIME_REASONING_EFFORT")
    rtmt.reasoning_effort = normalize_reasoning_effort(effort if effort else model_cfg.get("reasoning_effort"))
    parallel = model_cfg.get("parallel_tool_calls")
    rtmt.parallel_tool_calls = None if parallel is None else bool(parallel)
    switch = env.get("AZURE_OPENAI_REALTIME_REASONING_MODEL")
    rtmt.reasoning_model = parse_reasoning_model(switch if switch else model_cfg.get("reasoning_model"))
    if rtmt.reasoning_effort is not None and not rtmt._reasoning_model():
        logger.info("Deployment %s is not treated as a reasoning model (reasoning_model=%s); `reasoning` "
                    "(effort=%s) will not be sent", rtmt.deployment,
                    "auto" if rtmt.reasoning_model is None else rtmt.reasoning_model, rtmt.reasoning_effort)
    return rtmt
