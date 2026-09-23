import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import _get_bool_env


class GetBoolEnvTests(unittest.TestCase):
    """Tests for the _get_bool_env helper that parses boolean env vars."""

    def test_returns_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_get_bool_env("MISSING_VAR", False))
            self.assertTrue(_get_bool_env("MISSING_VAR", True))

    def test_truthy_values(self):
        for value in ("1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"):
            with patch.dict(os.environ, {"TEST_VAR": value}):
                self.assertTrue(_get_bool_env("TEST_VAR", False), f"Expected True for '{value}'")

    def test_falsy_values(self):
        for value in ("0", "false", "False", "no", "off", "maybe", ""):
            with patch.dict(os.environ, {"TEST_VAR": value}):
                self.assertFalse(_get_bool_env("TEST_VAR", False), f"Expected False for '{value}'")

    def test_whitespace_is_stripped(self):
        with patch.dict(os.environ, {"TEST_VAR": "  true  "}):
            self.assertTrue(_get_bool_env("TEST_VAR", False))

    def test_default_parameter_defaults_to_false(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_get_bool_env("MISSING_VAR"))


class SystemPromptTests(unittest.TestCase):

    def test_cloud_and_edge_paths_share_one_prompt(self):
        """scripts/smoke_realtime.py sends DUNKIN_SYSTEM_PROMPT, so both paths must use it."""
        import app
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("rtmt.system_message = "), 2)
        self.assertEqual(source.count("rtmt.system_message = DUNKIN_SYSTEM_PROMPT\n"), 2)
        self.assertIn("unmistakably Dunkin", app.DUNKIN_SYSTEM_PROMPT)


class CreateAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """Tests for create_app voice choice and system prompt configuration."""

    def setUp(self):
        # create_app registers a static route that requires the directory to exist
        self._static_dir = Path(__file__).resolve().parents[1] / "static"
        self._static_dir.mkdir(exist_ok=True)
        # aiohttp also needs index.html to serve the FileResponse route
        index_html = self._static_dir / "index.html"
        if not index_html.exists():
            index_html.write_text("<html></html>")
        self._created_index = not index_html.exists()

    async def _run_create_app(self, extra_env: dict | None = None):
        """Run create_app with mocked Azure services; return (class_mock, instance_mock)."""
        with patch("app.RTMiddleTier") as mock_cls, \
             patch("app.attach_tools_rtmt"), \
             patch.dict(os.environ, {
                 "RUNNING_IN_PRODUCTION": "1",
                 "AZURE_OPENAI_EASTUS2_ENDPOINT": "https://fake.openai.azure.com",
                 "AZURE_OPENAI_REALTIME_DEPLOYMENT": "gpt-4o-realtime",
                 "AZURE_OPENAI_EASTUS2_API_KEY": "fake-key",
                 "AZURE_SEARCH_API_KEY": "fake-search-key",
                 "AZURE_OPENAI_REALTIME_VOICE_CHOICE": "",
                 "AZURE_OPENAI_REALTIME_REASONING_EFFORT": "",
                 "AZURE_OPENAI_REALTIME_REASONING_MODEL": "",
                 "AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL": "",
                 **(extra_env or {}),
             }):
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            from app import create_app
            self.app = await create_app()
            return mock_cls, mock_instance

    async def test_reasoning_and_transcription_come_from_config_yaml(self):
        _, mock_instance = await self._run_create_app()
        self.assertEqual(mock_instance.reasoning_effort, "low")
        self.assertIsNone(mock_instance.reasoning_model)       # "auto"
        self.assertEqual(mock_instance.transcription_model, "whisper-1")

    async def test_reasoning_env_overrides_reach_the_middle_tier(self):
        _, mock_instance = await self._run_create_app({
            "AZURE_OPENAI_REALTIME_REASONING_EFFORT": "medium",
            "AZURE_OPENAI_REALTIME_REASONING_MODEL": "false",
            "AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL": "my-transcribe",
        })
        self.assertEqual(mock_instance.reasoning_effort, "medium")
        self.assertIs(mock_instance.reasoning_model, False)
        self.assertEqual(mock_instance.transcription_model, "my-transcribe")

    async def test_cloud_sessions_publish_to_the_crew_dashboard(self):
        _, mock_instance = await self._run_create_app()
        self.assertIs(mock_instance.sessions.dashboard, self.app["drive_thru_simulator"])

    async def test_the_local_pipeline_is_not_wired_to_the_dashboard(self):
        local_cls = MagicMock()
        local_cls.return_value = MagicMock(spec=["temperature", "system_message", "attach_to_app"])
        edge_modules = {"chromadb": MagicMock(), "chromadb.utils": MagicMock(),
                        "chromadb.utils.embedding_functions": MagicMock(),
                        "rtmt_local": MagicMock(RTLocalPipeline=local_cls)}
        with patch.dict(sys.modules, edge_modules), patch("app.attach_tools_rtmt"), \
                patch.dict(os.environ, {"USE_LOCAL_PIPELINE": "true", "RUNNING_IN_PRODUCTION": "1"}):
            from app import create_app
            app = await create_app()
        local_cls.assert_called_once()
        self.assertIn("drive_thru_simulator", app)

    async def test_default_voice_is_marin(self):
        mock_cls, _ = await self._run_create_app()
        _, kwargs = mock_cls.call_args
        self.assertEqual(kwargs["voice_choice"], "marin")

    async def test_rate_limit_recovery_comes_from_config_yaml(self):
        _, mock_instance = await self._run_create_app({"RATE_LIMIT_RECOVERY_ENABLED": ""})
        settings = mock_instance.rate_limit
        self.assertEqual((settings.enabled, settings.retry_delay, settings.second_retry_delay, settings.max_retries),
                         (True, 1.5, 4.0, 2))

    async def test_rate_limit_recovery_env_override(self):
        _, mock_instance = await self._run_create_app({"RATE_LIMIT_RECOVERY_ENABLED": "false"})
        self.assertFalse(mock_instance.rate_limit.enabled)

    async def test_default_voice_comes_from_config_yaml(self):
        with patch("app.get_config", return_value={"model": {"default_voice": "cedar"}}):
            mock_cls, _ = await self._run_create_app()
        self.assertEqual(mock_cls.call_args.kwargs["voice_choice"], "cedar")

    async def test_default_voice_falls_back_to_marin_without_config(self):
        with patch("app.get_config", return_value={}):
            mock_cls, _ = await self._run_create_app()
        self.assertEqual(mock_cls.call_args.kwargs["voice_choice"], "marin")

    async def test_voice_env_overrides_config(self):
        mock_cls, _ = await self._run_create_app({"AZURE_OPENAI_REALTIME_VOICE_CHOICE": "ash"})
        self.assertEqual(mock_cls.call_args.kwargs["voice_choice"], "ash")

    async def test_system_prompt_contains_pull_around_to_next_window(self):
        _, mock_instance = await self._run_create_app()
        self.assertIn(
            "Please pull around to the next window",
            mock_instance.system_message,
        )

    async def test_system_prompt_contains_get_order_tool_instruction(self):
        _, mock_instance = await self._run_create_app()
        self.assertIn("get_order", mock_instance.system_message)


if __name__ == "__main__":
    unittest.main()