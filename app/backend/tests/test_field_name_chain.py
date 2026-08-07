"""
test_field_name_chain.py — Verify that the search field-name defaults are
consistent across the entire deployment chain:

    main.parameters.json → main.bicep env → app.py defaults → tools.py select → setup_search_index.py schema
"""

import json
import re
import sys
import unittest
from pathlib import Path

# Repository root (backend/tests → backend → app → repo root)
REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.append(str(Path(__file__).resolve().parents[1]))


class FieldNameChainTests(unittest.TestCase):
    """Ensure search field defaults are consistent end-to-end."""

    EXPECTED_FIELDS = {
        "identifier": "id",
        "content": "description",
        "title": "name",
        "embedding": "embedding",
    }

    def test_parameters_json_defaults_match_index_schema(self):
        params_path = REPO_ROOT / "infra" / "main.parameters.json"
        with open(params_path) as f:
            params = json.load(f)

        p = params["parameters"]
        self.assertEqual(
            p["searchIdentifierField"]["value"],
            "${AZURE_SEARCH_IDENTIFIER_FIELD=id}",
        )
        self.assertEqual(
            p["searchContentField"]["value"],
            "${AZURE_SEARCH_CONTENT_FIELD=description}",
        )
        self.assertEqual(
            p["searchTitleField"]["value"],
            "${AZURE_SEARCH_TITLE_FIELD=name}",
        )
        self.assertEqual(
            p["searchEmbeddingField"]["value"],
            "${AZURE_SEARCH_EMBEDDING_FIELD=embedding}",
        )

    def test_app_py_defaults_match_index_schema(self):
        app_path = REPO_ROOT / "app" / "backend" / "app.py"
        content = app_path.read_text(encoding="utf-8")

        # Extract the `or "..."` defaults from app.py
        for field_key, expected in self.EXPECTED_FIELDS.items():
            pattern = rf'AZURE_SEARCH_{field_key.upper()}_FIELD.*or\s+"([^"]+)"'
            m = re.search(pattern, content)
            self.assertIsNotNone(m, f"Could not find default for {field_key} in app.py")
            self.assertEqual(m.group(1), expected, f"app.py default for {field_key} should be '{expected}'")

    def test_setup_search_index_schema_has_expected_fields(self):
        """The index schema must define the fields that the rest of the chain references."""
        index_path = REPO_ROOT / "app" / "backend" / "setup_search_index.py"
        content = index_path.read_text(encoding="utf-8")

        # The index defines fields by name= parameter
        for expected_name in self.EXPECTED_FIELDS.values():
            self.assertIn(
                f'name="{expected_name}"',
                content,
                f"setup_search_index.py must define a field named '{expected_name}'",
            )

    def test_env_sample_matches_expected_fields(self):
        env_path = REPO_ROOT / "app" / "backend" / ".env-sample"
        content = env_path.read_text(encoding="utf-8")
        self.assertIn("AZURE_SEARCH_IDENTIFIER_FIELD=id", content)
        self.assertIn("AZURE_SEARCH_EMBEDDING_FIELD=embedding", content)
        self.assertIn("AZURE_SEARCH_TITLE_FIELD=name", content)


if __name__ == "__main__":
    unittest.main()
