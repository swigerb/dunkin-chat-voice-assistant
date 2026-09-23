"""scripts/smoke_realtime.py: sends what the app sends, and never fails `azd up`."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer

REPO = Path(__file__).resolve().parents[3]
sys.path.append(str(REPO / "app" / "backend"))
sys.path.append(str(REPO / "scripts"))

import smoke_realtime  # noqa: E402

from rtmt import RTMiddleTier  # noqa: E402

CLEAN_ENV = {
    "AZURE_OPENAI_REALTIME_VOICE_CHOICE": "",
    "AZURE_OPENAI_REALTIME_REASONING_EFFORT": "",
    "AZURE_OPENAI_REALTIME_REASONING_MODEL": "",
    "AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL": "",
}


class EchoingRealtime:
    """Fake /openai/v1/realtime that applies session.updates and echoes the
    resulting session, like GA does, unless told to reject or drop fields."""

    def __init__(self):
        self.reject_keys: set[str] = set()
        self.drop_tools = False
        self.received: list[dict] = []
        self.auth: list[str | None] = []

    def app(self):
        app = web.Application()
        app.router.add_get("/openai/v1/realtime", self.handler)
        return app

    async def handler(self, request):
        self.auth.append(request.headers.get("api-key"))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        session: dict = {"type": "realtime", "tools": [], "tool_choice": "auto", "instructions": ""}
        async for msg in ws:
            event = json.loads(msg.data)
            self.received.append(event)
            if event["type"] != "session.update":
                continue
            bad = sorted(k for k in event["session"] if k in self.reject_keys)
            if bad:
                await ws.send_json({"type": "error", "error": {
                    "type": "invalid_request_error", "code": "invalid_value", "param": f"session.{bad[0]}",
                    "event_id": event.get("event_id"), "message": "Unsupported option."}})
                continue
            session.update(event["session"])
            echoed = dict(session)
            if self.drop_tools:
                echoed["tools"] = []
            await ws.send_json({"type": "session.updated", "session": echoed})
        return ws


def _run_app_and_capture_rtmt(extra_env=None):
    """Run app.create_app() on the cloud path with the REAL RTMiddleTier and
    tools; return the middle tier it builds."""
    captured = []

    def capture(self, app, path):
        captured.append(self)

    env = {
        "RUNNING_IN_PRODUCTION": "1",
        "AZURE_OPENAI_EASTUS2_ENDPOINT": "https://fake.openai.azure.com",
        "AZURE_OPENAI_REALTIME_DEPLOYMENT": "gpt-realtime-2.1",
        "AZURE_OPENAI_EASTUS2_API_KEY": "fake-key",
        "AZURE_SEARCH_API_KEY": "fake-search-key",
        "AZURE_SEARCH_ENDPOINT": "https://fake.search.windows.net",
        "AZURE_SEARCH_INDEX": "menu",
        **CLEAN_ENV,
        **(extra_env or {}),
    }
    static_dir = REPO / "app" / "backend" / "static"
    static_dir.mkdir(exist_ok=True)
    if not (static_dir / "index.html").exists():
        (static_dir / "index.html").write_text("<html></html>")
    with patch.dict(os.environ, env), patch.object(RTMiddleTier, "attach_to_app", capture):
        from app import create_app
        asyncio.run(create_app())
    return captured[0]


class PayloadParityTests(unittest.TestCase):
    """The smoke check is only worth anything if it sends what the app sends."""

    def _session(self, rtmt):
        return json.loads(rtmt.build_bootstrap_session_update())["session"]

    def test_bootstrap_matches_the_app(self):
        app_rtmt = _run_app_and_capture_rtmt()
        smoke_rtmt = smoke_realtime.build_middle_tier("https://fake.openai.azure.com", "gpt-realtime-2.1",
                                                      environ=dict(CLEAN_ENV))
        app_session, smoke_session = self._session(app_rtmt), self._session(smoke_rtmt)
        self.assertEqual(smoke_session, app_session)
        self.assertEqual(sorted(t["name"] for t in smoke_session["tools"]), sorted(smoke_realtime.EXPECTED_TOOLS))
        self.assertIn("Dunkin", smoke_session["instructions"])
        self.assertEqual(smoke_session["reasoning"], {"effort": "low"})
        self.assertEqual(smoke_session["audio"]["output"]["voice"], "marin")

    def test_env_overrides_apply_like_the_app(self):
        env = {"AZURE_OPENAI_REALTIME_REASONING_EFFORT": "medium", "AZURE_OPENAI_REALTIME_VOICE_CHOICE": "cedar"}
        app_rtmt = _run_app_and_capture_rtmt(env)
        smoke_rtmt = smoke_realtime.build_middle_tier("https://fake.openai.azure.com", "gpt-realtime-2.1",
                                                      environ={**CLEAN_ENV, **env})
        self.assertEqual(self._session(smoke_rtmt), self._session(app_rtmt))
        self.assertEqual(self._session(smoke_rtmt)["reasoning"], {"effort": "medium"})

    def test_payloads_cover_bootstrap_browser_and_fallback(self):
        rtmt = smoke_realtime.build_middle_tier("https://x", "gpt-realtime-2.1", environ=dict(CLEAN_ENV))
        payloads = dict(smoke_realtime.session_update_payloads(rtmt))
        self.assertEqual(list(payloads), ["bootstrap", "relayed browser session.update", "minimal fallback"])
        self.assertEqual(json.loads(payloads["minimal fallback"])["session"],
                         json.loads(rtmt.build_fallback_session_update())["session"])
        relayed = json.loads(payloads["relayed browser session.update"])["session"]
        self.assertEqual(relayed["audio"]["input"]["turn_detection"]["silence_duration_ms"], 500)


class CheckSessionTests(unittest.TestCase):

    def _good(self):
        return {"tools": [{"name": n} for n in smoke_realtime.EXPECTED_TOOLS], "tool_choice": "auto",
                "instructions": "x", "reasoning": {"effort": "low"}, "audio": {"output": {"voice": "marin"}}}

    def test_pass(self):
        self.assertEqual(smoke_realtime.check_session("b", self._good(), self._good(), None, {"effort": "low"}), [])

    def test_each_failure_mode_is_reported(self):
        cases = {
            "rejected": (self._good(), None, {"error": {"code": "invalid_value", "param": "session.reasoning"}}),
            "tools": (self._good(), {**self._good(), "tools": [{"name": "search"}]}, None),
            "tool_choice": (self._good(), {**self._good(), "tool_choice": "none"}, None),
            "instructions": (self._good(), {**self._good(), "instructions": ""}, None),
            "reasoning": (self._good(), {**self._good(), "reasoning": {"effort": "none"}}, None),
            "voice": (self._good(), {**self._good(), "audio": {"output": {"voice": "alloy"}}}, None),
        }
        for name, (sent, echoed, error) in cases.items():
            with self.subTest(name):
                failures = smoke_realtime.check_session("b", sent, echoed, error, {"effort": "low"})
                self.assertEqual(len(failures), 1, failures)


class SynthesizeTests(unittest.IsolatedAsyncioTestCase):
    """The test audio must be the phrase read aloud, not the model's reply to it."""

    async def test_phrase_is_sent_as_response_instructions_not_a_user_turn(self):
        received = []

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                event = json.loads(msg.data)
                received.append(event)
                if event["type"] == "response.create":
                    await ws.send_json({"type": "response.output_audio.delta", "delta": "AAAA"})
                    await ws.send_json({"type": "response.done"})
            return ws

        app = web.Application()
        app.router.add_get("/openai/v1/realtime", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            url = smoke_realtime.realtime_url(str(server.make_url("")), "gpt-realtime-2.1")
            pcm = await smoke_realtime._synthesize(url, {}, smoke_realtime.TRANSCRIPTION_PHRASE, 5)
        finally:
            await server.close()

        self.assertEqual(pcm, b"\x00\x00\x00")
        kinds = [e["type"] for e in received]
        self.assertNotIn("conversation.item.create", kinds)
        create = next(e for e in received if e["type"] == "response.create")
        self.assertIn(f'"{smoke_realtime.TRANSCRIPTION_PHRASE}"', create["response"]["instructions"])
        self.assertIn("word for word", create["response"]["instructions"])
        self.assertIsNone(received[0]["session"]["audio"]["input"]["turn_detection"])


class LiveShapeTests(unittest.IsolatedAsyncioTestCase):
    """End to end against a fake GA endpoint (no Azure)."""

    async def asyncSetUp(self):
        self.fake = EchoingRealtime()
        self.server = TestServer(self.fake.app())
        await self.server.start_server()
        self.endpoint = str(self.server.make_url("/"))
        env = patch.dict(os.environ, CLEAN_ENV)
        env.start()
        self.addCleanup(env.stop)

    async def asyncTearDown(self):
        await self.server.close()

    async def _run(self):
        return await smoke_realtime.run(self.endpoint, "gpt-realtime-2.1", voice=None, timeout=5,
                                        skip_transcription=True, headers={"api-key": "k"})

    async def test_passes_when_the_session_is_accepted(self):
        self.assertEqual(await self._run(), 0)
        updates = [e for e in self.fake.received if e["type"] == "session.update"]
        self.assertEqual(len(updates), 3)
        self.assertEqual(self.fake.auth, ["k"])

    async def test_fails_when_reasoning_is_rejected(self):
        self.fake.reject_keys = {"reasoning"}
        self.assertEqual(await self._run(), 1)

    async def test_fails_when_tools_do_not_register(self):
        self.fake.drop_tools = True
        self.assertEqual(await self._run(), 1)

    async def test_unreachable_endpoint_is_could_not_run(self):
        await self.server.close()
        with self.assertRaises(smoke_realtime.SmokeError):
            await self._run()


class MainTests(unittest.TestCase):

    def test_missing_settings_exit_2(self):
        with patch.dict(os.environ, {"AZURE_OPENAI_EASTUS2_ENDPOINT": "", "AZURE_OPENAI_REALTIME_DEPLOYMENT": ""}), \
                patch.object(smoke_realtime, "_azd_env_values", return_value={}):
            self.assertEqual(smoke_realtime.main([]), 2)

    def test_smoke_error_exit_2(self):
        async def boom(*_a, **_k):
            raise smoke_realtime.SmokeError("no token")
        with patch.object(smoke_realtime, "run", boom):
            self.assertEqual(smoke_realtime.main(["--endpoint", "https://x", "--deployment", "d"]), 2)

    def test_azd_values_fill_unset_app_settings(self):
        # Every env var create_app reads for the realtime session.
        app_settings = ["AZURE_OPENAI_REALTIME_REASONING_EFFORT", "AZURE_OPENAI_REALTIME_REASONING_MODEL",
                        "AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL", "AZURE_OPENAI_REALTIME_VOICE_CHOICE"]
        seen = {}

        async def fake_run(*_a, **_k):
            seen.update({n: os.environ.get(n) for n in app_settings})
            return 0
        azd = {"AZURE_OPENAI_EASTUS2_ENDPOINT": "https://x", "AZURE_OPENAI_REALTIME_DEPLOYMENT": "gpt-realtime-2.1",
               "AZURE_OPENAI_REALTIME_REASONING_EFFORT": "medium", "AZURE_OPENAI_REALTIME_REASONING_MODEL": "false",
               "AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL": "whisper-1", "AZURE_OPENAI_REALTIME_VOICE_CHOICE": "cedar"}
        names = ["AZURE_OPENAI_EASTUS2_ENDPOINT", "AZURE_OPENAI_REALTIME_DEPLOYMENT", *app_settings]
        saved = {n: os.environ.pop(n, None) for n in names}
        try:
            with patch.object(smoke_realtime, "_azd_env_values", return_value=azd), \
                    patch.object(smoke_realtime, "run", fake_run):
                self.assertEqual(smoke_realtime.main([]), 0)
        finally:
            for n in names:
                os.environ.pop(n, None)
                if saved[n] is not None:
                    os.environ[n] = saved[n]
        self.assertEqual(seen, {n: azd[n] for n in app_settings})


class TenantTests(unittest.TestCase):
    """The token must come from the resource's tenant, not the active `az` or `azd` default."""

    NAMES = ["AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_OPENAI_EASTUS2_ENDPOINT",
             "AZURE_OPENAI_REALTIME_DEPLOYMENT"]
    AZD = {"AZURE_OPENAI_EASTUS2_ENDPOINT": "https://x", "AZURE_OPENAI_REALTIME_DEPLOYMENT": "d",
           "AZURE_TENANT_ID": "azd-tenant", "AZURE_SUBSCRIPTION_ID": "azd-sub"}
    EXPLICIT = ["--endpoint", "https://x", "--deployment", "d"]

    def _main_identity(self, argv, env, azd):
        seen = {}

        async def fake_run(*_a, **kwargs):
            seen["identity"] = (kwargs.get("tenant_id"), kwargs.get("subscription_id"))
            return 0
        saved = {n: os.environ.pop(n, None) for n in self.NAMES}
        try:
            os.environ.update(env)
            with patch.object(smoke_realtime, "_azd_env_values", return_value=azd), \
                    patch.object(smoke_realtime, "run", fake_run):
                self.assertEqual(smoke_realtime.main(argv), 0)
        finally:
            for n in self.NAMES:
                os.environ.pop(n, None)
                if saved[n] is not None:
                    os.environ[n] = saved[n]
        return seen["identity"]

    def test_azd_env_identity_is_used(self):
        self.assertEqual(self._main_identity([], {}, self.AZD), ("azd-tenant", "azd-sub"))

    def test_azd_env_identity_is_used_even_with_explicit_endpoint_and_deployment(self):
        self.assertEqual(self._main_identity(self.EXPLICIT, {}, self.AZD), ("azd-tenant", "azd-sub"))

    def test_env_then_cli_override_azd(self):
        env = {"AZURE_TENANT_ID": "env-tenant", "AZURE_SUBSCRIPTION_ID": "env-sub"}
        self.assertEqual(self._main_identity([], env, self.AZD), ("env-tenant", "env-sub"))
        self.assertEqual(self._main_identity(["--tenant", "cli-tenant", "--subscription", "cli-sub"], env, self.AZD),
                         ("cli-tenant", "cli-sub"))

    def test_each_value_falls_back_to_azd_independently(self):
        self.assertEqual(self._main_identity(["--tenant", "cli-tenant"], {}, self.AZD), ("cli-tenant", "azd-sub"))
        self.assertEqual(self._main_identity([], {"AZURE_SUBSCRIPTION_ID": "env-sub"}, self.AZD),
                         ("azd-tenant", "env-sub"))

    def test_nothing_anywhere_is_none(self):
        self.assertEqual(self._main_identity(self.EXPLICIT, {}, {}), (None, None))

    def test_run_passes_identity_to_auth(self):
        seen = {}

        def fake_auth(*args):
            seen["args"] = args
            raise smoke_realtime.SmokeError("stop here")
        with patch.object(smoke_realtime, "get_auth_headers", fake_auth), \
                self.assertRaises(smoke_realtime.SmokeError):
            asyncio.run(smoke_realtime.run("https://x", "d", voice=None, timeout=1, skip_transcription=True,
                                           tenant_id="t", subscription_id="s"))
        self.assertEqual(seen["args"], ("t", "s"))

    def _auth(self, tenant_id, subscription_id, fail=()):
        """Returns (headers or SmokeError, credentials built, credentials asked for a token)."""
        built, asked = [], []

        class FakeCred:
            def __init__(self, kind, **kwargs):
                self.kind = kind
                built.append((kind, kwargs.get("tenant_id") or kwargs.get("subscription")))

            def get_token(self, *scopes, **_kw):
                asked.append(self.kind)
                if self.kind in fail:
                    raise RuntimeError(f"{self.kind} said no\nsecond line")
                return SimpleNamespace(token=f"tok-{self.kind}", expires_on=0)

        def az(**k):
            return FakeCred("az-sub" if k.get("subscription") else "az", **k)

        import azure.identity as identity
        with patch.dict(os.environ, {"AZURE_OPENAI_EASTUS2_API_KEY": ""}), \
                patch.object(identity, "AzureDeveloperCliCredential", lambda **k: FakeCred("azd", **k)), \
                patch.object(identity, "AzureCliCredential", az), \
                patch.object(identity, "DefaultAzureCredential", lambda **k: FakeCred("default", **k)):
            try:
                result = smoke_realtime.get_auth_headers(tenant_id, subscription_id)
            except smoke_realtime.SmokeError as exc:
                result = exc
        return result, built, asked

    def test_subscription_first_then_tenant_pinned_clis(self):
        headers, built, asked = self._auth("tenant-x", "sub-y")
        self.assertEqual(built, [("az-sub", "sub-y"), ("azd", "tenant-x"), ("az", "tenant-x")])
        self.assertEqual(asked, ["az-sub"])
        self.assertEqual(headers, {"Authorization": "Bearer " + "tok-az-sub"})

    def test_hard_failure_moves_on_to_the_next_credential(self):
        headers, _, asked = self._auth("tenant-x", "sub-y", fail=("az-sub", "azd"))
        self.assertEqual(asked, ["az-sub", "azd", "az"])
        self.assertEqual(headers, {"Authorization": "Bearer " + "tok-az"})

    def test_all_failing_reports_every_credential(self):
        err, _, asked = self._auth("tenant-x", "sub-y", fail=("az-sub", "azd", "az"))
        self.assertIsInstance(err, smoke_realtime.SmokeError)
        self.assertEqual(asked, ["az-sub", "azd", "az"])
        self.assertIn("az-sub said no", str(err))
        self.assertIn("azd said no", str(err))
        self.assertNotIn("second line", str(err))

    def test_tenant_only_pins_both_clis(self):
        _, built, _ = self._auth("tenant-x", None)
        self.assertEqual(built, [("azd", "tenant-x"), ("az", "tenant-x")])

    def test_subscription_only(self):
        _, built, _ = self._auth(None, "sub-y")
        self.assertEqual(built, [("az-sub", "sub-y")])

    def test_nothing_falls_back_to_default_credential(self):
        headers, built, _ = self._auth(None, None)
        self.assertEqual(built, [("default", None)])
        self.assertEqual(headers, {"Authorization": "Bearer " + "tok-default"})


class PostdeployHookTests(unittest.TestCase):
    """An anonymous external `azd up` must never fail because of the smoke check."""

    def test_azure_yaml_postdeploy_is_non_fatal_and_non_interactive(self):
        hooks = yaml.safe_load((REPO / "azure.yaml").read_text(encoding="utf-8"))["hooks"]
        for platform, script in (("windows", "./scripts/smoke_realtime.ps1"), ("posix", "./scripts/smoke_realtime.sh")):
            with self.subTest(platform):
                hook = hooks["postdeploy"][platform]
                self.assertEqual(hook["run"], script)
                self.assertIs(hook["continueOnError"], True)
                self.assertIs(hook["interactive"], False)
                self.assertTrue((REPO / script).is_file())

    def _wrapper_text(self, name):
        return (REPO / "scripts" / name).read_text(encoding="utf-8")

    def test_wrappers_always_exit_0(self):
        for name in ("smoke_realtime.ps1", "smoke_realtime.sh"):
            with self.subTest(name):
                exits = [line.strip() for line in self._wrapper_text(name).splitlines()
                         if line.strip().startswith("exit")]
                self.assertEqual(set(exits), {"exit 0"})
                self.assertIn("DUNKIN_SKIP_REALTIME_SMOKE", self._wrapper_text(name))

    def test_sh_wrapper_is_lf_and_executable(self):
        raw = (REPO / "scripts" / "smoke_realtime.sh").read_bytes()
        self.assertNotIn(b"\r\n", raw)
        mode = subprocess.run(["git", "ls-files", "-s", "scripts/smoke_realtime.sh"], cwd=REPO,
                              capture_output=True, text=True).stdout
        if mode:  # tracked
            self.assertTrue(mode.startswith("100755"), mode)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_ps1_skip_switch(self):
        out = subprocess.run(["pwsh", "-NoProfile", "-File", str(REPO / "scripts" / "smoke_realtime.ps1")],
                             env={**os.environ, "DUNKIN_SKIP_REALTIME_SMOKE": "true"},
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0)
        self.assertIn("skipped (DUNKIN_SKIP_REALTIME_SMOKE=true)", out.stdout)

    @unittest.skipUnless(shutil.which("pwsh") and os.name == "nt" and sys.prefix != sys.base_prefix,
                         "needs pwsh and a Windows venv")
    def test_ps1_exits_0_when_the_check_fails(self):
        """Point the wrapper at a Python that exits 1: warning, still exit 0."""
        fake_root = REPO / "app" / "backend" / "tests" / "_smoke_wrapper_tmp"
        (fake_root / "scripts").mkdir(parents=True, exist_ok=True)
        venv = fake_root / ".venv" / "Scripts"
        venv.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, fake_root, ignore_errors=True)
        shutil.copy(REPO / "scripts" / "smoke_realtime.ps1", fake_root / "scripts" / "smoke_realtime.ps1")
        (fake_root / "scripts" / "smoke_realtime.py").write_text("import sys; sys.exit(1)\n")
        # A copy of this venv's launcher + pyvenv.cfg is a working interpreter.
        shutil.copy(sys.executable, venv / "python.exe")
        shutil.copy(Path(sys.prefix) / "pyvenv.cfg", fake_root / ".venv" / "pyvenv.cfg")
        out = subprocess.run(["pwsh", "-NoProfile", "-File", str(fake_root / "scripts" / "smoke_realtime.ps1")],
                             env={k: v for k, v in os.environ.items() if k != "DUNKIN_SKIP_REALTIME_SMOKE"},
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("REALTIME SMOKE CHECK FAILED", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
