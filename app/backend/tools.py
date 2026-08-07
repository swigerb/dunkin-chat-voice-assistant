import json
import logging
import os
from pathlib import Path
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizableTextQuery

from order_state import order_state_singleton
from rtmt import RTMiddleTier, Tool, ToolResult, ToolResultDirection

logger = logging.getLogger(__name__)

__all__ = ["attach_tools_rtmt"]


# Extras may only be applied to specific beverage categories.
EXTRAS_KEYWORDS = (
    "flavor swirl",
    "whipped cream",
    "extra espresso shot",
    "extra shot",
)
ALLOWED_EXTRA_CATEGORIES = {"signature lattes", "cold beverages"}
BLOCKED_EXTRA_CATEGORIES = {"donuts & bakery", "breakfast sandwiches"}


def _load_menu_category_map() -> dict[str, str]:
    env_override = (
        os.environ.get("DUNKIN_MENU_ITEMS_PATH")
        or os.environ.get("MENU_ITEMS_PATH")
    )

    candidate_paths = []
    if env_override:
        candidate_paths.append(Path(env_override))

    # Preferred: keep backend self-contained (Docker image can copy this in).
    candidate_paths.append(Path(__file__).resolve().parent / "data" / "menuItems.json")

    # Fallback: repo layout (local dev).
    candidate_paths.append(Path(__file__).resolve().parent.parent / "frontend" / "src" / "data" / "menuItems.json")

    menu_path = next((path for path in candidate_paths if path.exists()), None)
    if menu_path is None:
        return {}
    try:
        with menu_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        mapping = {}
        for category_entry in data.get("menuItems", []):
            category = category_entry.get("category", "").strip().lower()
            for item in category_entry.get("items", []):
                name = item.get("name")
                if name:
                    mapping[name.lower()] = category
        return mapping
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Failed to load menu items; falling back to keyword category inference: %s", exc)
        return {}


MENU_CATEGORY_MAP = _load_menu_category_map()


def _is_extra_item(item_name: str) -> bool:
    normalized = item_name.lower()
    return any(keyword in normalized for keyword in EXTRAS_KEYWORDS)


def _infer_category(item_name: str) -> str:
    normalized = item_name.lower()
    if normalized in MENU_CATEGORY_MAP:
        return MENU_CATEGORY_MAP[normalized]
    if "latte" in normalized:
        return "signature lattes"
    if "cold brew" in normalized or "refresher" in normalized or "cold" in normalized:
        return "cold beverages"
    if "donut" in normalized or "bagel" in normalized or "munchkins" in normalized:
        return "donuts & bakery"
    if "sandwich" in normalized or "wrap" in normalized or "croissant" in normalized:
        return "breakfast sandwiches"
    return ""



search_tool_schema = {
    "type": "function",
    "name": "search",
    "description": "Search the knowledge base. The knowledge base is in English, translate to and from English if " + \
                   "needed. Results are formatted as a source name first in square brackets, followed by the text " + \
                   "content, and a line with '-----' at the end of each result.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query"
            }
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

async def search(
    search_client: SearchClient,
    semantic_configuration: str,
    identifier_field: str,
    content_field: str,
    embedding_field: str,
    use_vector_query: bool,
    args: Any,
    use_semantic_ranker: bool = True,
) -> ToolResult:
    """Execute a hybrid Azure AI Search query with safe fallbacks."""

    query = args["query"]
    logger.info("Knowledge search requested for query '%s'", query)

    vector_queries = []
    if use_vector_query and embedding_field:
        vector_queries.append(VectorizableTextQuery(text=query, k_nearest_neighbors=50, fields=embedding_field))

    select_fields = {
        identifier_field or "id",
        content_field or "content",
        "category",
        "name",
        "description",
        "longDescription",
        "origin",
        "caffeineContent",
        "brewingMethod",
        "popularity",
        "sizes",
    }

    # Build query kwargs — only request semantic ranker when available.
    # The free search SKU has no semantic ranker and returns HTTP 400 if asked.
    search_kwargs: dict[str, Any] = {
        "search_text": query,
        "top": 5,
        "vector_queries": vector_queries or None,
        "select": list(select_fields),
    }
    if use_semantic_ranker:
        search_kwargs["query_type"] = "semantic"
        search_kwargs["semantic_configuration_name"] = semantic_configuration

    try:
        search_results = await search_client.search(**search_kwargs)
    except HttpResponseError as exc:
        # Runtime fallback: if semantic ranker was requested but fails (e.g.,
        # free tier), retry without it rather than surfacing an error.
        if use_semantic_ranker and ("semantic" in str(exc).lower() or exc.status_code == 400):
            logger.warning("Semantic ranker failed (SKU may not support it); retrying without: %s", exc)
            search_kwargs.pop("query_type", None)
            search_kwargs.pop("semantic_configuration_name", None)
            try:
                search_results = await search_client.search(**search_kwargs)
            except HttpResponseError as inner_exc:
                logger.error("Azure AI Search fallback also failed: %s", inner_exc)
                return ToolResult("I'm sorry, I can't reach our menu data right now.", ToolResultDirection.TO_SERVER)
        elif "Could not find a property named" in str(exc):
            logger.warning("Retrying search with safe literal fields after select mismatch: %s", exc)
            # Use genuinely safe literals — the configured field may itself be the
            # wrong name (truthy but invalid), so we must not reuse it.
            search_kwargs["select"] = ["id", "description"]
            search_kwargs.pop("query_type", None)
            search_kwargs.pop("semantic_configuration_name", None)
            try:
                search_results = await search_client.search(**search_kwargs)
            except HttpResponseError as inner_exc:
                logger.error("Azure AI Search fallback with safe literals also failed: %s", inner_exc)
                return ToolResult("I'm sorry, I can't reach our menu data right now.", ToolResultDirection.TO_SERVER)
        else:
            logger.error("Azure AI Search request failed: %s", exc)
            return ToolResult("I'm sorry, I can't reach our menu data right now.", ToolResultDirection.TO_SERVER)

    results = []
    async for record in search_results:
        identifier = record.get(identifier_field) or record.get("id", "unknown")
        summary = (
            f"[{identifier}]: "
            f"Name: {record.get('name', 'N/A')}, Category: {record.get('category', 'N/A')}, "
            f"Description: {record.get('description', 'N/A')}, Sizes: {record.get('sizes', 'N/A')}"
        )
        results.append(summary)

    joined_results = "\n-----\n".join(results)
    logger.debug("Search results returned %d documents", len(results))
    return ToolResult(joined_results or "No matching menu entries found.", ToolResultDirection.TO_SERVER)


update_order_tool_schema = {
    "type": "function",
    "name": "update_order",
    "description": "Update the current order by adding or removing items.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": { 
                "type": "string", 
                "description": "Action to perform: 'add' or 'remove'.", 
                "enum": ["add", "remove"]
            },
            "item_name": { 
                "type": "string", 
                "description": "Name of the item to update, e.g., 'Cappuccino'."
            },
            "size": { 
                "type": "string", 
                "description": "Size of the item to update, e.g., 'Large'."
            },
            "quantity": { 
                "type": "integer", 
                "description": "Quantity of the item to update. Represents the number of items."
            },
            "price": { 
                "type": "number", 
                "description": "Price of a single item to add. Required only for 'add' action. Note: This is the price per individual item, not the total price for the quantity."
            }
        },
        "required": ["action", "item_name", "size", "quantity"],
        "additionalProperties": False
    }
}

async def update_order(args, session_id: str) -> ToolResult:
    """Update the current order by adding or removing items."""

    logger.info("Updating order for session %s with payload %s", session_id, args)

    item_name = args["item_name"]
    if args["action"] == "add" and _is_extra_item(item_name):
        current_items = order_state_singleton.get_order_summary(session_id).items
        has_allowed_base = False
        has_blocked_base = False

        for order_item in current_items:
            category = _infer_category(order_item.item)
            if category in ALLOWED_EXTRA_CATEGORIES:
                has_allowed_base = True
            if category in BLOCKED_EXTRA_CATEGORIES:
                has_blocked_base = True

        if not has_allowed_base:
            apology = (
                "I can add extras to signature lattes or cold beverages, "
                "but not to donuts or breakfast sandwiches."
            )
            if has_blocked_base:
                apology = (
                    "I can add extras to signature lattes or cold beverages, "
                    "but I can't add them to donuts or breakfast sandwiches."
                )
            logger.info("Blocked extra '%s' for session %s", item_name, session_id)
            return ToolResult(apology, ToolResultDirection.TO_SERVER)

    order_state_singleton.handle_order_update(
        session_id,
        args["action"],
        item_name,
        args["size"],
        args.get("quantity", 0),
        args.get("price", 0.0),
    )

    order_summary = order_state_singleton.get_order_summary(session_id)
    json_order_summary = order_summary.model_dump_json()
    logger.debug("Session %s order summary after update: %s", session_id, json_order_summary)

    return ToolResult(json_order_summary, ToolResultDirection.TO_CLIENT)


get_order_tool_schema = {
    "type": "function",
    "name": "get_order",
    "description": "Retrieve the current order summary.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False
    }
}

async def get_order(_args: Any, session_id: str) -> ToolResult:
    """Retrieve the current order summary."""

    logger.info("Retrieving order summary for session %s", session_id)
    order_summary = order_state_singleton.get_order_summary(session_id)
    return ToolResult(order_summary.model_dump_json(), ToolResultDirection.TO_SERVER)


def attach_tools_rtmt(
    rtmt: RTMiddleTier,
    credentials: AzureKeyCredential | DefaultAzureCredential,
    search_endpoint: str,
    search_index: str,
    semantic_configuration: str,
    identifier_field: str,
    content_field: str,
    embedding_field: str,
    title_field: str,
    use_vector_query: bool,
    use_semantic_ranker: bool = True,
) -> None:
    """Attach search and order tools to the RTMiddleTier instance."""

    if not isinstance(credentials, AzureKeyCredential):
        credentials.get_token("https://search.azure.com/.default")  # warm up prior to first call
    search_client = SearchClient(search_endpoint, search_index, credentials, user_agent="RTMiddleTier")

    rtmt.tools["search"] = Tool(schema=search_tool_schema, target=lambda args: search(search_client, semantic_configuration, identifier_field, content_field, embedding_field, use_vector_query, args, use_semantic_ranker))
    rtmt.tools["update_order"] = Tool(schema=update_order_tool_schema, target=lambda args, session_id: update_order(args, session_id))
    rtmt.tools["get_order"] = Tool(schema=get_order_tool_schema, target=lambda args, session_id: get_order(args, session_id))


