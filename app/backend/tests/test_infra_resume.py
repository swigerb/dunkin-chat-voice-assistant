"""Deployment prerequisites for order resume (step 0).

Orders and the detached-session grace hold live in process memory, so every
container runs exactly one gunicorn worker and the Container App ingress pins a
browser to one replica. Because the app always sends a secrets list, an
out-of-band EasyAuth ``aad-client-secret`` must be read back and re-sent.
"""

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCKERFILES = [REPO / "app" / "Dockerfile", REPO / "app" / "Dockerfile.edge"]
MAIN_BICEP = REPO / "infra" / "main.bicep"
CONTAINER_APP_BICEP = REPO / "infra" / "core" / "host" / "container-app.bicep"
UPSERT_BICEP = REPO / "infra" / "core" / "host" / "container-app-upsert.bicep"
EDGE_DEPLOYMENTS = [REPO / "k8s" / "deployment.yaml", REPO / "flux" / "apps" / "dunkin-voice" / "deployment.yaml"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _backend_module_block(text: str) -> str:
    start = text.index("module acaBackend")
    end = min(i for i in (text.find("\nmodule ", start + 1), text.find("\nresource ", start + 1)) if i != -1)
    return text[start:end]


class DockerfileWorkerTests(unittest.TestCase):

    def test_gunicorn_runs_exactly_one_worker(self):
        for dockerfile in DOCKERFILES:
            with self.subTest(dockerfile=dockerfile.name):
                text = _read(dockerfile).replace("\\\r\n", " ").replace("\\\n", " ")
                cmd = text[text.index("CMD ["):]
                args = json.loads(cmd[len("CMD "):cmd.index("]") + 1])
                self.assertIn("gunicorn", args)
                self.assertEqual(args[args.index("--workers") + 1], "1",
                                 "a second worker has its own orders: a reconnect would miss ~50% of the time")
                self.assertEqual(args.count("--workers"), 1)
                self.assertNotIn("-w", args)

    def test_edge_deployments_run_one_replica(self):
        # The edge Service has no session affinity; resume needs every reconnect
        # to reach the pod that holds the order.
        for manifest in EDGE_DEPLOYMENTS:
            with self.subTest(manifest=str(manifest.relative_to(REPO))):
                self.assertRegex(_read(manifest), r"(?m)^\s+replicas:\s*1\s*$")


class StickyIngressTests(unittest.TestCase):

    def test_backend_container_app_uses_sticky_affinity(self):
        self.assertRegex(_backend_module_block(_read(MAIN_BICEP)), r"stickySessionsAffinity:\s*'sticky'")

    def test_affinity_reaches_the_ingress_block(self):
        self.assertRegex(_read(UPSERT_BICEP), r"stickySessionsAffinity:\s*stickySessionsAffinity")
        app = _read(CONTAINER_APP_BICEP)
        ingress = app[app.index("ingress: ingressEnabled ?"):app.index("dapr:")]
        self.assertRegex(ingress, r"stickySessions:\s*\{\s*affinity:\s*stickySessionsAffinity\s*\}")

    def test_single_revision_mode_and_api_version_support_sticky(self):
        app = _read(CONTAINER_APP_BICEP)
        self.assertRegex(app, r"param revisionMode string = 'Single'")
        self.assertNotRegex(_backend_module_block(_read(MAIN_BICEP)), r"revisionMode")
        version = re.search(r"resource app 'Microsoft\.App/containerApps@([0-9-]+)(-preview)?'", app).group(1)
        self.assertGreaterEqual(version, "2023-05-02", "ingress.stickySessions needs API 2023-05-02-preview or later")


class AuthSecretPreservationTests(unittest.TestCase):

    def test_out_of_band_auth_secret_is_named_for_preservation(self):
        block = _backend_module_block(_read(MAIN_BICEP))
        self.assertIn("preserveExistingSecretNames: enableAuth && empty(authClientSecret) ? [ 'aad-client-secret' ] : []",
                      block)
        self.assertIn("secrets: enableAuth && !empty(authClientSecret) ? { 'aad-client-secret': authClientSecret } : {}",
                      block)

    def test_upsert_merges_read_back_secrets_under_the_supplied_ones(self):
        upsert = _read(UPSERT_BICEP)
        self.assertRegex(upsert, r"param preserveExistingSecretNames array = \[\]")
        merged = upsert[upsert.index("secrets: union("):upsert.index("keyvaultIdentities: keyvaultIdentities")]
        self.assertIn("exists && !empty(preserveExistingSecretNames)", merged)
        self.assertIn("existingApp.listSecrets().value", merged)
        self.assertIn("contains(preserveExistingSecretNames, s.name) && !contains(secrets, s.name)", merged)
        # Supplied secrets come last so they win over read-back values.
        self.assertRegex(merged, r": \{\},\s*secrets\)\s*$")


if __name__ == "__main__":
    unittest.main()
