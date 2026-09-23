"""Generate the pre-recorded rate-limit apology clips.

When the model is rate-limited the middle tier sends the browser
`extension.rate_limited` and the browser plays a LOCAL clip ("Sorry, give me
just a second.") - it can't ask the model, the model is what's rate-limited.
This script records that clip once per UI language with the live realtime
model and the app's default voice, and writes 24 kHz mono PCM16 wavs to
app/frontend/public/audio/apology-<lng>.wav. Re-run it if the default voice
changes.

Usage (from the repo root, with `az login` / `azd auth login` done):

    python scripts/generate_apology_clips.py                     # azd env / env vars
    python scripts/generate_apology_clips.py --deployment gpt-realtime-2.1 --voice marin

Endpoint/deployment/tenant/subscription resolve like scripts/smoke_realtime.py.
A clip is only written if the model's transcript of what it said matches the
phrase, so a paraphrase or an answer never ships.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import re
import sys
import time
import unicodedata
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp  # noqa: E402
from smoke_realtime import (  # noqa: E402
    REPO_ROOT,
    SmokeError,
    _azd_env_values,
    _next_event,
    get_auth_headers,
    realtime_url,
    resolve_setting,
)

OUT_DIR = REPO_ROOT / "app" / "frontend" / "public" / "audio"
SAMPLE_RATE = 24000

# Keep in step with app/frontend/src/lib/apology-clip.ts APOLOGY_CLIP_LANGUAGES.
PHRASES = {
    "en": ("English", "Sorry, give me just a second."),
    "es": ("Spanish", "Perdón, dame un segundito."),
    "fr": ("French", "Pardon, juste une petite seconde."),
    "ja": ("Japanese", "すみません、少々お待ちください。"),
}

SESSION_INSTRUCTIONS = (
    "You are the voice of a friendly Dunkin' drive-thru crew member. "
    "You only read out the exact line you are given, nothing more."
)


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\W_]+", "", text)


async def synthesize(url: str, headers: dict, voice: str, language: str, phrase: str,
                     timeout: float) -> tuple[bytes, str]:
    pcm = bytearray()
    transcript = ""
    async with aiohttp.ClientSession() as http, http.ws_connect(url, headers=headers) as ws:
        await ws.send_json({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": SESSION_INSTRUCTIONS,
            "output_modalities": ["audio"],
            "audio": {"input": {"turn_detection": None},
                      "output": {"voice": voice, "format": {"type": "audio/pcm", "rate": SAMPLE_RATE}}}}})
        # The line goes in the response instructions, not a user turn, so the
        # model recites it instead of answering it (see smoke_realtime.py).
        await ws.send_json({"type": "response.create", "response": {
            "instructions": (f"In {language}, say exactly this line, word for word, and nothing else, "
                             f"in a warm, apologetic, upbeat drive-thru tone: \"{phrase}\"")}})
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            event = await _next_event(ws, remaining)
            if event is None:
                continue
            kind = event.get("type")
            if kind == "response.output_audio.delta":
                pcm += base64.b64decode(event["delta"])
            elif kind == "response.output_audio_transcript.done":
                transcript = event.get("transcript") or ""
            elif kind == "response.done":
                response = event.get("response") or {}
                if response.get("status") == "failed":
                    raise SmokeError(f"response failed: {response.get('status_details')}")
                break
            elif kind == "error":
                raise SmokeError(f"error: {event.get('error')}")
    return bytes(pcm), transcript


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


async def run(args) -> int:
    azd = _azd_env_values()
    endpoint = resolve_setting("AZURE_OPENAI_EASTUS2_ENDPOINT", args.endpoint, azd)
    deployment = resolve_setting("AZURE_OPENAI_REALTIME_DEPLOYMENT", args.deployment, azd)
    if not endpoint or not deployment:
        print("error: set --endpoint/--deployment or AZURE_OPENAI_EASTUS2_ENDPOINT/AZURE_OPENAI_REALTIME_DEPLOYMENT")
        return 2
    headers = get_auth_headers(resolve_setting("AZURE_TENANT_ID", args.tenant, azd),
                               resolve_setting("AZURE_SUBSCRIPTION_ID", args.subscription, azd))
    url = realtime_url(endpoint, deployment)
    languages = args.languages or list(PHRASES)
    failed = 0
    for lng in languages:
        language, phrase = PHRASES[lng]
        for attempt in range(1, args.attempts + 1):
            try:
                pcm, transcript = await synthesize(url, headers, args.voice, language, phrase, args.timeout)
            except SmokeError as exc:
                print(f"{lng}: attempt {attempt}: {exc}")
                await asyncio.sleep(5 * attempt)
                continue
            seconds = len(pcm) / (SAMPLE_RATE * 2)
            if pcm and _normalise(transcript) == _normalise(phrase):
                path = OUT_DIR / f"apology-{lng}.wav"
                write_wav(path, pcm)
                print(f"{lng}: wrote {path.relative_to(REPO_ROOT)} ({seconds:.2f}s): {transcript!r}")
                break
            print(f"{lng}: attempt {attempt}: said {transcript!r} ({seconds:.2f}s), expected {phrase!r}")
        else:
            failed += 1
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", help="Azure OpenAI endpoint (default: AZURE_OPENAI_EASTUS2_ENDPOINT)")
    parser.add_argument("--deployment", help="Realtime deployment (default: AZURE_OPENAI_REALTIME_DEPLOYMENT)")
    parser.add_argument("--voice", default="marin", help="Voice (default: marin, the app default)")
    parser.add_argument("--tenant", help="Entra tenant of the Azure OpenAI resource (default: AZURE_TENANT_ID)")
    parser.add_argument("--subscription", help="Subscription whose `az` sign-in to use (default: AZURE_SUBSCRIPTION_ID)")
    parser.add_argument("--languages", nargs="*", choices=sorted(PHRASES), help="Only these languages")
    parser.add_argument("--attempts", type=int, default=3, help="Tries per language (default 3)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait per clip (default 30)")
    args = parser.parse_args(argv)
    # The es/fr/ja transcripts are printed; don't die on a cp1252 console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        return asyncio.run(run(args))
    except SmokeError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
