"""Guard the azd service name <-> infra parameter wiring (ported from Sonic ecc9c55).

azd exports SERVICE_<NAME>_RESOURCE_EXISTS for each service in azure.yaml
(name upper-cased, '-' -> '_'). If main.parameters.json reads a variable for a
service that doesn't exist, `exists` is always false and every `azd provision`
redeploys the container app with the helloworld placeholder image. Dunkin read
SERVICE_WEB_* while its only service is named `backend`.
"""

import json
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
AZURE_YAML = REPO / "azure.yaml"
PARAMS = REPO / "infra" / "main.parameters.json"
MAIN_BICEP = REPO / "infra" / "main.bicep"
UPSERT_BICEP = REPO / "infra" / "core" / "host" / "container-app-upsert.bicep"

_EXISTS_VAR = re.compile(r"\$\{SERVICE_([A-Z0-9_]+)_RESOURCE_EXISTS(?:=[^}]*)?\}")
_SERVICE_TAG = re.compile(r"'azd-service-name'\s*:\s*'([^']+)'")


def _azd_env_name(service: str) -> str:
    return service.upper().replace("-", "_")


class AzdServiceWiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.services = yaml.safe_load(AZURE_YAML.read_text(encoding="utf-8"))["services"]
        cls.params = json.loads(PARAMS.read_text(encoding="utf-8"))["parameters"]
        cls.main_bicep = MAIN_BICEP.read_text(encoding="utf-8")

    def _exists_vars(self) -> dict[str, str]:
        found = {}
        for param, spec in self.params.items():
            match = _EXISTS_VAR.search(json.dumps(spec.get("value", "")))
            if match:
                found[param] = match.group(1)
        return found

    def test_resource_exists_vars_name_real_azd_services(self):
        exists_vars = self._exists_vars()
        self.assertTrue(exists_vars, "no SERVICE_*_RESOURCE_EXISTS mapping found in main.parameters.json")
        known = {_azd_env_name(s): s for s in self.services}
        for param, env_service in exists_vars.items():
            self.assertIn(
                env_service, known,
                f"{param} reads SERVICE_{env_service}_RESOURCE_EXISTS, but azure.yaml services are "
                f"{sorted(self.services)} -> azd never sets it, so `exists` is always false and "
                "provision falls back to the helloworld image")

    def test_every_containerapp_service_has_an_exists_mapping(self):
        mapped = set(self._exists_vars().values())
        for name, svc in self.services.items():
            if svc.get("host") == "containerapp":
                self.assertIn(_azd_env_name(name), mapped,
                              f"containerapp service '{name}' has no SERVICE_*_RESOURCE_EXISTS parameter")

    def test_backend_exists_defaults_false_for_first_provision(self):
        self.assertEqual(self.params["webAppExists"]["value"], "${SERVICE_BACKEND_RESOURCE_EXISTS=false}")

    def test_bicep_service_tags_match_azure_yaml(self):
        tags = set(_SERVICE_TAG.findall(self.main_bicep))
        self.assertTrue(tags, "no azd-service-name tag found in main.bicep")
        self.assertLessEqual(tags, set(self.services), "azd-service-name tag not declared in azure.yaml")

    def test_backend_upsert_module_receives_the_exists_flag(self):
        """The mapped parameter must actually reach container-app-upsert's `exists`."""
        module = re.search(r"module acaBackend 'core/host/container-app-upsert\.bicep' = \{(.*?)\n\}",
                           self.main_bicep, re.S)
        self.assertIsNotNone(module, "acaBackend container-app-upsert module not found in main.bicep")
        body = module.group(1)
        self.assertRegex(body, r"\n\s*exists: webAppExists\n")
        self.assertIn("'azd-service-name': 'backend'", body)
        self.assertRegex(self.main_bicep, r"\nparam webAppExists bool\n")

    def test_upsert_reuses_the_running_image_when_it_exists(self):
        upsert = UPSERT_BICEP.read_text(encoding="utf-8")
        self.assertIn("exists ? existingApp.properties.template.containers[0].image", upsert)


if __name__ == "__main__":
    unittest.main()
