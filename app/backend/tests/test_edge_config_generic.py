"""The edge GitOps / kubectl manifests must not ship anyone's real Azure resources.

Every operator applies flux/ (or k8s/ via scripts/deploy-edge.*) to their own
cluster; a committed endpoint, registry, domain or account name points that
cluster at somebody else's resource. Operator values are placeholders
(`<your-...>` in flux/, `${VAR}` rendered by the deploy scripts in k8s/).
"""
import re
import subprocess
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
MANIFEST_DIRS = ("flux", "k8s")
# The edge scripts and docs operators follow; they may only show generic examples.
EDGE_FILES = ("scripts/deploy-edge.ps1", "scripts/deploy-edge.sh", "docs/azure-local-deployment.md",
              "docs/foundry-local-architecture.md")

# Any Azure OpenAI / Cognitive Services host whose account label is not a placeholder.
AOAI_HOST_RE = re.compile(r"([a-z0-9][a-z0-9-]*)\.(openai|cognitiveservices)\.azure\.com", re.I)
GUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# Values that shipped in Sprint 2 from the contributor's own lab.
CONTRIBUTOR_VALUES = ("acx-dunkin-edge", "cadunkinacr", "adaptivecloudlab", "mgodfre3")
# Generic examples allowed in comments/docs.
ALLOWED_EXAMPLE_ACCOUNTS = {"my-dunkin-openai"}


def _manifest_files():
    for folder in MANIFEST_DIRS:
        yield from sorted(p for p in (REPO / folder).rglob("*") if p.suffix in (".yaml", ".yml"))


def _offending_aoai_hosts(text: str, allowed=frozenset()) -> list[str]:
    return [m.group(0) for m in AOAI_HOST_RE.finditer(text) if m.group(1).lower() not in allowed]


class EdgeConfigIsGenericTests(unittest.TestCase):

    def test_manifest_dirs_exist(self):
        files = list(_manifest_files())
        self.assertGreater(len(files), 10)
        self.assertIn(REPO / "flux" / "apps" / "dunkin-voice" / "configmap.yaml", files)

    def test_no_hardcoded_azure_openai_host_in_manifests(self):
        for path in _manifest_files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(REPO))):
                self.assertNotIn("acx-dunkin-edge", text)
                self.assertEqual(_offending_aoai_hosts(text, ALLOWED_EXAMPLE_ACCOUNTS), [])

    def test_no_contributor_values_or_ids_in_edge_config(self):
        paths = list(_manifest_files()) + [REPO / rel for rel in EDGE_FILES]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(REPO))):
                self.assertEqual([v for v in CONTRIBUTOR_VALUES if v in text.lower()], [])
                self.assertEqual(GUID_RE.findall(text), [])

    def test_flux_endpoint_is_a_placeholder(self):
        data = yaml.safe_load((REPO / "flux/apps/dunkin-voice/configmap.yaml").read_text(encoding="utf-8"))["data"]
        self.assertEqual(data["AZURE_OPENAI_EASTUS2_ENDPOINT"], "https://<your-aoai-account>.openai.azure.com/")
        self.assertTrue(data["AZURE_OPENAI_REALTIME_DEPLOYMENT"])

    def test_env_template_covers_deploy_scripts_with_blank_values(self):
        """.env.template (referenced by the edge doc and both deploy scripts) lists every
        variable the scripts require, and ships no values: operators fill in their own."""
        template = REPO / ".env.template"
        self.assertTrue(template.is_file(), ".env.template is missing (is it git-ignored?)")
        text = template.read_text(encoding="utf-8")
        assignments = dict(line.split("=", 1) for line in text.splitlines()
                           if line.strip() and not line.lstrip().startswith("#"))
        sh = (REPO / "scripts/deploy-edge.sh").read_text(encoding="utf-8")
        required = re.search(r"REQUIRED_VARS=\(([^)]*)\)", sh).group(1).split()
        self.assertGreaterEqual(len(required), 7)
        for name in required:
            self.assertIn(name, assignments)
        self.assertEqual({k: v for k, v in assignments.items() if v.strip()}, {})
        self.assertEqual(_offending_aoai_hosts(text, {"your-aoai-account"}), [])
        self.assertEqual(GUID_RE.findall(text), [])

    def test_env_template_is_not_git_ignored(self):
        """`.env.*` is ignored (real .env files hold keys); the template must still be tracked."""
        try:
            r = subprocess.run(["git", "check-ignore", "-q", ".env.template"], cwd=REPO, capture_output=True)
        except OSError:
            self.skipTest("git not available")
        if r.returncode == 128:
            self.skipTest("not a git checkout")
        self.assertEqual(r.returncode, 1, ".env.template is git-ignored")

    def test_host_check_is_not_vacuous(self):
        self.assertEqual(_offending_aoai_hosts('ENDPOINT: "wss://someone-else.openai.azure.com/"'),
                         ["someone-else.openai.azure.com"])
        self.assertEqual(_offending_aoai_hosts("x.cognitiveservices.azure.com"), ["x.cognitiveservices.azure.com"])
        self.assertEqual(_offending_aoai_hosts("https://<your-aoai-account>.openai.azure.com/"), [])


if __name__ == "__main__":
    unittest.main()
