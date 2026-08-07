"""Tests for the USE_LOCAL_PIPELINE feature flag and ChromaDB search path.

These tests verify:
1. The flag defaults to false (cloud path).
2. No edge imports (chromadb, onnxruntime, rtmt_local) occur when the flag is off.
3. The ChromaDB search function works correctly with a mocked collection.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import _get_bool_env
from rtmt import ToolResultDirection
from tools import search_chromadb


class UseLocalPipelineFlagTests(unittest.TestCase):
    """USE_LOCAL_PIPELINE must default to false."""

    def test_flag_defaults_to_false(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_get_bool_env("USE_LOCAL_PIPELINE", False))

    def test_flag_unset_defaults_to_false(self):
        env = {k: v for k, v in os.environ.items() if k != "USE_LOCAL_PIPELINE"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(_get_bool_env("USE_LOCAL_PIPELINE"))

    def test_flag_explicit_true(self):
        with patch.dict(os.environ, {"USE_LOCAL_PIPELINE": "true"}):
            self.assertTrue(_get_bool_env("USE_LOCAL_PIPELINE", False))

    def test_app_py_defaults_flag_to_false(self):
        """Mutation guard: the call site in app.py must pass default=False."""
        import inspect

        import app as app_module

        source = inspect.getsource(app_module.create_app)
        self.assertIn(
            '_get_bool_env("USE_LOCAL_PIPELINE", False)',
            source,
            "app.py must call _get_bool_env('USE_LOCAL_PIPELINE', False) — "
            "the default must be False so cloud deployments never enter the local branch",
        )


class NoEdgeImportsWhenFlagOffTests(unittest.TestCase):
    """When USE_LOCAL_PIPELINE is off, edge modules must never be imported."""

    def test_chromadb_not_imported_at_module_level(self):
        """Verify chromadb is not a top-level import of app.py or tools.py."""
        import app as app_module
        import tools as tools_module

        # Check that neither module's globals reference chromadb
        for mod in (app_module, tools_module):
            for name, obj in vars(mod).items():
                if name.startswith("_"):
                    continue
                mod_name = getattr(obj, "__module__", "") or ""
                self.assertNotIn(
                    "chromadb",
                    mod_name,
                    f"{mod.__name__}.{name} references chromadb at module level",
                )

    def test_rtmt_local_not_imported_at_module_level(self):
        """Verify rtmt_local is not a top-level import of app.py."""
        import app as app_module

        self.assertFalse(
            hasattr(app_module, "RTLocalPipeline"),
            "RTLocalPipeline should not be a top-level attribute of app.py",
        )

    def test_cloud_app_imports_cleanly_without_edge_deps(self):
        """app and tools must import without chromadb or onnxruntime installed."""
        # We can't actually uninstall packages mid-test, but we CAN verify
        # that the module-level import path succeeds (it already did if we got
        # here), and that 'chromadb' is not in sys.modules due to app/tools.
        # Reload to be sure.
        import importlib

        # Remove any cached edge modules
        for mod_name in list(sys.modules):
            if mod_name.startswith(("chromadb", "onnxruntime", "rtmt_local")):
                del sys.modules[mod_name]

        import app as app_module
        import tools as tools_module
        importlib.reload(tools_module)
        importlib.reload(app_module)

        # After reload, chromadb should NOT be in sys.modules
        # (unless some other test pulled it in, which is fine — the point is
        # our module-level code doesn't import it).
        self.assertNotIn("chromadb", dir(app_module))
        self.assertNotIn("chromadb", dir(tools_module))


class ChromaDBSearchTests(unittest.TestCase):
    """Tests for the search_chromadb function with a mocked collection."""

    def _make_mock_collection(self, ids, documents=None, metadatas=None):
        """Create a mock ChromaDB collection that returns canned results."""
        collection = MagicMock()
        collection.query.return_value = {
            "ids": [ids],
            "documents": [documents or [""] * len(ids)],
            "metadatas": [metadatas or [{}] * len(ids)],
            "distances": [[0.1] * len(ids)],
        }
        return collection

    def test_formats_results_with_separator(self):
        metadatas = [
            {"name": "Caramel Craze Latte", "category": "Signature Lattes",
             "description": "A rich latte", "sizes": "S, M, L"},
            {"name": "Glazed Donut", "category": "Donuts & Bakery",
             "description": "Classic glazed", "sizes": "Standard"},
        ]
        collection = self._make_mock_collection(
            ids=["latte-1", "donut-1"],
            metadatas=metadatas,
        )
        result = asyncio.run(search_chromadb(collection, {"query": "latte"}))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("[latte-1]", result.text)
        self.assertIn("[donut-1]", result.text)
        self.assertIn("-----", result.text)
        self.assertIn("Caramel Craze Latte", result.text)

    def test_no_results_returns_fallback_message(self):
        collection = MagicMock()
        collection.query.return_value = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        result = asyncio.run(search_chromadb(collection, {"query": "xyz"}))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("No matching menu entries found", result.text)

    def test_missing_metadata_uses_defaults(self):
        collection = self._make_mock_collection(ids=["item-3"], metadatas=[{}])
        result = asyncio.run(search_chromadb(collection, {"query": "test"}))
        self.assertIn("N/A", result.text)
        self.assertIn("[item-3]", result.text)

    def test_query_exception_returns_apology(self):
        collection = MagicMock()
        collection.query.side_effect = RuntimeError("Connection failed")
        result = asyncio.run(search_chromadb(collection, {"query": "latte"}))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("can't reach", result.text.lower())

    def test_collection_receives_correct_query(self):
        collection = self._make_mock_collection(ids=["x"])
        asyncio.run(search_chromadb(collection, {"query": "iced coffee"}))
        collection.query.assert_called_once_with(
            query_texts=["iced coffee"],
            n_results=5,
            include=["documents", "metadatas"],
        )


if __name__ == "__main__":
    unittest.main()
