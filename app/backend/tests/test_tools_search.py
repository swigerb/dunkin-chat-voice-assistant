import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from rtmt import ToolResultDirection
from tools import _infer_category, _is_extra_item, search


class IsExtraItemTests(unittest.TestCase):
    def test_recognized_extras(self):
        self.assertTrue(_is_extra_item("Extra Espresso Shot"))
        self.assertTrue(_is_extra_item("Whipped Cream"))
        self.assertTrue(_is_extra_item("Flavor Swirl"))
        self.assertTrue(_is_extra_item("Extra Shot"))

    def test_case_insensitive(self):
        self.assertTrue(_is_extra_item("extra espresso shot"))
        self.assertTrue(_is_extra_item("WHIPPED CREAM"))

    def test_non_extras(self):
        self.assertFalse(_is_extra_item("Glazed Donut"))
        self.assertFalse(_is_extra_item("Caramel Craze Latte"))
        self.assertFalse(_is_extra_item("Cold Brew"))
        self.assertFalse(_is_extra_item("Bagel"))


class InferCategoryTests(unittest.TestCase):
    def test_latte_inferred(self):
        self.assertEqual(_infer_category("Caramel Craze Latte"), "signature lattes")
        self.assertEqual(_infer_category("Iced Latte"), "signature lattes")

    def test_cold_beverages_inferred(self):
        self.assertEqual(_infer_category("Original Cold Brew"), "cold beverages")
        self.assertEqual(_infer_category("Strawberry Refresher"), "cold beverages")

    def test_donuts_inferred(self):
        self.assertEqual(_infer_category("Glazed Donut"), "donuts & bakery")
        self.assertEqual(_infer_category("Everything Bagel"), "donuts & bakery")
        self.assertEqual(_infer_category("Munchkins Box"), "donuts & bakery")

    def test_sandwiches_inferred(self):
        self.assertEqual(_infer_category("Bacon Egg & Cheese on Croissant"), "breakfast sandwiches")
        self.assertEqual(_infer_category("Turkey Sausage Wrap"), "breakfast sandwiches")

    def test_unknown_returns_empty(self):
        self.assertEqual(_infer_category("Mystery Item XYZ"), "")


class SearchToolTests(unittest.TestCase):
    """Tests for the search() tool function with mocked Azure Search client."""

    def _make_mock_client(self, records):
        """Create a mock SearchClient whose .search() returns an async iterable of records."""
        client = AsyncMock()

        async def _fake_search(**kwargs):
            async def _async_iter():
                for r in records:
                    yield r
            return _async_iter()

        client.search = _fake_search
        return client

    def test_formats_results_with_separator(self):
        records = [
            {"id": "1", "name": "Caramel Craze Latte", "category": "Signature Lattes",
             "description": "A rich latte", "sizes": "S, M, L"},
            {"id": "2", "name": "Glazed Donut", "category": "Donuts & Bakery",
             "description": "Classic glazed", "sizes": "Standard"},
        ]
        client = self._make_mock_client(records)
        result = asyncio.run(search(client, "menuSemanticConfig", "id", "description", "embedding", False, {"query": "latte"}))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("[1]", result.text)
        self.assertIn("[2]", result.text)
        self.assertIn("-----", result.text)
        self.assertIn("Caramel Craze Latte", result.text)

    def test_no_results_returns_fallback_message(self):
        client = self._make_mock_client([])
        result = asyncio.run(search(client, "menuSemanticConfig", "id", "description", "embedding", False, {"query": "xyz"}))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("No matching menu entries found", result.text)

    def test_missing_fields_use_defaults(self):
        records = [{"id": "3"}]
        client = self._make_mock_client(records)
        result = asyncio.run(search(client, "menuSemanticConfig", "id", "description", "embedding", False, {"query": "test"}))
        self.assertIn("N/A", result.text)
        self.assertIn("[3]", result.text)

    def test_generic_http_error_returns_apology(self):
        from azure.core.exceptions import HttpResponseError
        client = AsyncMock()
        client.search = AsyncMock(side_effect=HttpResponseError(message="Service unavailable"))
        result = asyncio.run(search(client, "menuSemanticConfig", "id", "description", "embedding", False, {"query": "latte"}))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("can't reach", result.text.lower())

    def test_field_mismatch_triggers_fallback_retry(self):
        from azure.core.exceptions import HttpResponseError

        records = [{"id": "5", "description": "A tasty item"}]
        call_count = 0

        async def _search_with_fallback(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise HttpResponseError(message="Could not find a property named 'sizes'")
            async def _async_iter():
                for r in records:
                    yield r
            return _async_iter()

        client = AsyncMock()
        client.search = _search_with_fallback

        result = asyncio.run(search(client, "menuSemanticConfig", "id", "description", "embedding", False, {"query": "item"}))
        self.assertEqual(call_count, 2)
        self.assertIn("[5]", result.text)

    def test_field_mismatch_fallback_uses_safe_literals_not_configured_field(self):
        """Regression: when identifier_field is truthy but wrong, the fallback
        must use literal 'id'/'description' rather than the configured bad name."""
        from azure.core.exceptions import HttpResponseError

        captured_selects = []

        async def _capture_search(**kwargs):
            captured_selects.append(kwargs.get("select"))
            if len(captured_selects) == 1:
                raise HttpResponseError(message="Could not find a property named 'chunk_id'")
            async def _async_iter():
                yield {"id": "ok", "description": "found it"}
            return _async_iter()

        client = AsyncMock()
        client.search = _capture_search

        # Pass a truthy but WRONG identifier_field
        result = asyncio.run(search(
            client, "menuSemanticConfig", "chunk_id", "chunk", "text_vector", False, {"query": "test"}
        ))
        # The fallback select must be the safe literals, NOT the configured bad fields
        self.assertEqual(captured_selects[1], ["id", "description"])
        self.assertIn("[ok]", result.text)

    def test_field_mismatch_fallback_failure_returns_apology(self):
        """When both the initial search and the fallback retry fail, the tool
        must return an apology rather than raising an unhandled exception."""
        from azure.core.exceptions import HttpResponseError

        async def _always_fail(**kwargs):
            raise HttpResponseError(message="Could not find a property named 'bad_field'")

        client = AsyncMock()
        client.search = _always_fail

        result = asyncio.run(search(
            client, "menuSemanticConfig", "bad_field", "bad_content", "bad_emb", False, {"query": "latte"}
        ))
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("can't reach", result.text.lower())


if __name__ == "__main__":
    unittest.main()
