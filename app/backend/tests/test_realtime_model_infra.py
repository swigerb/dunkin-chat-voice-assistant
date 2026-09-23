"""The realtime model / reasoning settings must line up across infra, azd and the edge manifests.

A drift here fails silently at deploy time: bicep would create (or the app would
target) the wrong deployment, or an azd override would never reach the app.
"""

import json
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
BICEP = (REPO / "infra" / "main.bicep").read_text(encoding="utf-8")
PARAMETERS = json.loads((REPO / "infra" / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
AZURE_YAML = yaml.safe_load((REPO / "azure.yaml").read_text(encoding="utf-8"))

REALTIME_MODEL = "gpt-realtime-2.1"
REALTIME_VERSION = "2026-07-07"

# bicep param -> env var the backend reads (app/backend/rtmt.py configure_realtime_model)
OVERRIDES = {
    "openAiRealtimeReasoningEffort": "AZURE_OPENAI_REALTIME_REASONING_EFFORT",
    "openAiRealtimeReasoningModel": "AZURE_OPENAI_REALTIME_REASONING_MODEL",
    "openAiRealtimeTranscriptionModel": "AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL",
}


def _bicep_block(start_marker: str, text: str = BICEP) -> str:
    start = text.index(start_marker)
    depth = 0
    for i in range(start, len(text)):
        if text[i] in "[{(":
            depth += 1
        elif text[i] in "]})":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unterminated block after {start_marker!r}")


class RealtimeDeploymentTests(unittest.TestCase):

    def test_bicep_realtime_deployment_is_2_1_global_standard(self):
        deployments = _bicep_block("var openAiDeployments = [")
        first = _bicep_block("{", deployments)
        self.assertRegex(first, rf"name: '{re.escape(REALTIME_MODEL)}'\n")
        self.assertRegex(first, rf"model: \{{\s*format: 'OpenAI'\s*name: '{re.escape(REALTIME_MODEL)}'\s*"
                                rf"version: '{REALTIME_VERSION}'")
        self.assertRegex(first, r"sku: \{\s*name: 'GlobalStandard'")

    def test_shared_openai_account_is_never_redeclared_when_reused(self):
        """Dunkin reuses Sonic's AOAI account; redeclaring its deployments would mutate Sonic's."""
        self.assertRegex(BICEP, r"module openAi '[^']+' = if \(!reuseExistingOpenAi\) \{")
        self.assertIn("AZURE_OPENAI_REALTIME_DEPLOYMENT: reuseExistingOpenAi ? openAiRealtimeDeployment : "
                      "openAiDeployments[0].name", BICEP)

    def test_env_sample_targets_2_1(self):
        sample = (REPO / "app" / "backend" / ".env-sample").read_text(encoding="utf-8")
        self.assertIn(f"AZURE_OPENAI_REALTIME_DEPLOYMENT={REALTIME_MODEL}\n", sample)
        self.assertIn(f"AZURE_OPENAI_REALTIME_CHAT_DEPLOYMENT_VERSION={REALTIME_VERSION}\n", sample)


class ReasoningOverrideWiringTests(unittest.TestCase):

    def test_each_override_flows_azd_to_bicep_to_app_env(self):
        env_block = _bicep_block("env: union(")
        pipeline_vars = set(AZURE_YAML["pipeline"]["variables"])
        for param, env_var in OVERRIDES.items():
            with self.subTest(param=param):
                self.assertRegex(BICEP, rf"\nparam {param} string = ''\n")
                self.assertEqual(PARAMETERS[param]["value"], "${" + env_var + "}")
                # Only set on the container when non-empty, so config.yaml applies otherwise.
                self.assertIn(f"empty({param}) ? {{}} : {{ {env_var}: {param} }}", env_block)
                self.assertIn(env_var, pipeline_vars)


class EdgeManifestTests(unittest.TestCase):
    """Azure Local edge manifests (cloud-voice path) must target the same model."""

    def _data(self, relpath):
        return yaml.safe_load((REPO / relpath).read_text(encoding="utf-8"))["data"]

    def test_edge_configmaps_target_2_1(self):
        for relpath in ("flux/apps/dunkin-voice/configmap.yaml", "k8s/configmap.yaml"):
            with self.subTest(relpath=relpath):
                self.assertEqual(self._data(relpath)["AZURE_OPENAI_REALTIME_DEPLOYMENT"], REALTIME_MODEL)

    def test_edge_gating_unchanged(self):
        self.assertEqual(self._data("flux/apps/dunkin-voice/configmap.yaml")["USE_LOCAL_PIPELINE"], "false")


if __name__ == "__main__":
    unittest.main()
