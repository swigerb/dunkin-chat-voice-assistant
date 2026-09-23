import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizableTextQuery

from config_loader import get_config
from order_state import order_state_singleton
from rtmt import RTMiddleTier, Tool, ToolResult, ToolResultDirection

logger = logging.getLogger(__name__)

__all__ = ["attach_tools_rtmt", "search_chromadb", "update_order", "MAX_QUANTITY_PER_ITEM", "MAX_TOTAL_ITEMS"]

_config = get_config()
_biz_cfg = _config.get("business_rules", {})

MAX_QUANTITY_PER_ITEM: int = _biz_cfg.get("max_item_quantity", 10)
MAX_TOTAL_ITEMS: int = _biz_cfg.get("max_order_items", 25)


# Extras may only be applied to specific beverage categories.
EXTRAS_KEYWORDS = (
    "flavor swirl",
    "whipped cream",
    "extra espresso shot",
    "extra shot",
)
ALLOWED_EXTRA_CATEGORIES = {"signature lattes", "cold beverages"}
BLOCKED_EXTRA_CATEGORIES = {"donuts & bakery", "breakfast sandwiches"}

# Menu name and price of each extra, for the corrected call suggested when the
# model folds an extra into the drink's name ("Latte with extra espresso shot").
EXTRA_MENU_ITEMS = (
    ("extra espresso shot", "Extra Espresso Shot", 1.00),
    ("extra shot", "Extra Espresso Shot", 1.00),
    ("whipped cream", "Whipped Cream", 0.50),
    ("flavor swirl", "Flavor Swirl", 0.75),
)
_COMBINED_NAME_RE = re.compile(r"\s+(?:with|plus|and|\+|&)\s+", re.IGNORECASE)


def _rejected(reason: str, message: str, item_name: str, *, next_step: str,
              suggested_calls: list[dict] | None = None) -> ToolResult:
    """An update_order the guard refused. Sent to the model (never the browser)
    as explicit JSON so it can't read the refusal as a success: at effort
    "none" gpt-realtime-2.1 told guests "All set" after a plain-text refusal."""
    payload: dict[str, Any] = {
        "status": "rejected",
        "item_added": False,
        "item_name": item_name,
        "reason": reason,
        "message": message,
        "instructions": "Nothing was added to the order. Do not tell the guest it was added or say 'all set'. " + next_step,
    }
    if suggested_calls:
        payload["suggested_calls"] = suggested_calls
    return ToolResult(json.dumps(payload, ensure_ascii=False), ToolResultDirection.TO_SERVER)


def _split_combined_extra(item_name: str) -> tuple[str, str, float] | None:
    """'Caramel Craze Latte with Extra Espresso Shot' -> ('Caramel Craze Latte',
    'Extra Espresso Shot', 1.0). None when the name is just the extra."""
    parts = _COMBINED_NAME_RE.split(item_name.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    base, extra = (p.strip() for p in parts)
    extra_lower = extra.lower()
    for keyword, menu_name, price in EXTRA_MENU_ITEMS:
        if keyword in extra_lower:
            return base, menu_name, price
    return None


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


async def search_chromadb(collection: Any, args: Any) -> ToolResult:
    """Execute a local ChromaDB vector search query (edge / Azure Local only)."""

    query = args["query"]
    logger.info("ChromaDB knowledge search requested for query '%s'", query)

    try:
        results = collection.query(
            query_texts=[query],
            n_results=5,
            include=["documents", "metadatas"],
        )
    except Exception as exc:
        logger.error("ChromaDB search failed: %s", exc)
        return ToolResult("I'm sorry, I can't reach our menu data right now.", ToolResultDirection.TO_SERVER)

    formatted = []
    if results and results.get("ids") and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            summary = (
                f"[{doc_id}]: "
                f"Name: {meta.get('name', 'N/A')}, Category: {meta.get('category', 'N/A')}, "
                f"Description: {meta.get('description', 'N/A')}, Sizes: {meta.get('sizes', 'N/A')}"
            )
            formatted.append(summary)

    joined_results = "\n-----\n".join(formatted)
    logger.debug("ChromaDB search returned %d documents", len(formatted))
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
                "description": "Name of ONE menu item, e.g., 'Cappuccino'. Extras (whipped cream, flavor swirl, extra espresso shot) are separate items: add the drink first, then the extra with its own call. The result's status is 'ok' when the order changed and 'rejected' when nothing was added."
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
    action = args["action"]
    size = args.get("size", "")
    quantity = args.get("quantity", 0)

    # ── Quantity limits (add only) ──
    if action == "add":
        current_items = order_state_singleton.get_order_summary(session_id).items

        # Per-item limit
        existing_qty = 0
        for order_item in current_items:
            if order_item.item == item_name and order_item.size == size:
                existing_qty = order_item.quantity
                break
        new_item_qty = existing_qty + quantity
        if new_item_qty > MAX_QUANTITY_PER_ITEM:
            allowed = MAX_QUANTITY_PER_ITEM - existing_qty
            if allowed <= 0:
                msg = (
                    f"That's a lot of {item_name}! We can do up to "
                    f"{MAX_QUANTITY_PER_ITEM} of any one item. You already have {existing_qty} — "
                    f"would you like to keep it at {existing_qty}?"
                )
            else:
                msg = (
                    f"That's a lot of {item_name}! We can do up to "
                    f"{MAX_QUANTITY_PER_ITEM} of any one item. I can add {allowed} more — "
                    f"would you like me to do that?"
                )
            logger.info("Per-item limit hit for '%s' in session %s (requested %d, existing %d)",
                        item_name, session_id, quantity, existing_qty)
            return _rejected("item_quantity_limit", msg, item_name,
                             next_step="Tell the guest the limit and ask how many they'd like.")

        # Total order limit
        total_qty = sum(oi.quantity for oi in current_items) + quantity
        if total_qty > MAX_TOTAL_ITEMS:
            remaining = MAX_TOTAL_ITEMS - sum(oi.quantity for oi in current_items)
            if remaining <= 0:
                msg = (
                    f"Wow, that's a big order! Our drive-thru tops out at "
                    f"{MAX_TOTAL_ITEMS} items total so we can keep things moving. "
                    f"You're already at the max — would you like to swap anything out?"
                )
            else:
                msg = (
                    f"Wow, that's a big order! Our drive-thru tops out at "
                    f"{MAX_TOTAL_ITEMS} items total so we can keep things moving. "
                    f"I can add {remaining} more — would you like me to do that?"
                )
            logger.info("Total order limit hit in session %s (would be %d items)", session_id, total_qty)
            return _rejected("order_item_limit", msg, item_name,
                             next_step="Tell the guest the limit and ask what they'd like to do.")

        # Extras validation
        if _is_extra_item(item_name):
            has_allowed_base = False
            has_blocked_base = False

            for order_item in current_items:
                category = _infer_category(order_item.item)
                if category in ALLOWED_EXTRA_CATEGORIES:
                    has_allowed_base = True
                if category in BLOCKED_EXTRA_CATEGORIES:
                    has_blocked_base = True

            if not has_allowed_base:
                combined = _split_combined_extra(item_name)
                base_category = _infer_category(combined[0]) if combined else ""
                logger.info("Blocked extra '%s' for session %s", item_name, session_id)
                if combined and base_category in ALLOWED_EXTRA_CATEGORIES:
                    base, extra, extra_price = combined
                    return _rejected(
                        "extra_in_item_name",
                        f"Extras are separate items: add the {base} first, then the {extra} as its own item.",
                        item_name,
                        next_step=("The guest already agreed to this order, so make the suggested_calls now "
                                   "(use the drink's menu price per item), then confirm."),
                        suggested_calls=[
                            {"action": "add", "item_name": base, "size": size, "quantity": quantity},
                            {"action": "add", "item_name": extra, "size": "Standard", "quantity": quantity,
                             "price": extra_price},
                        ],
                    )
                if has_blocked_base or base_category in BLOCKED_EXTRA_CATEGORIES:
                    apology = (
                        "I can add extras to signature lattes or cold beverages, "
                        "but I can't add them to donuts or breakfast sandwiches."
                    )
                else:
                    apology = (
                        "I can add extras to signature lattes or cold beverages, "
                        "but not to donuts or breakfast sandwiches."
                    )
                return _rejected(
                    "extra_without_drink", apology, item_name,
                    next_step=("Tell the guest, and offer to add the extra to a signature latte or cold beverage "
                               "(add that drink first, then the extra as its own item)."),
                )

    order_state_singleton.handle_order_update(
        session_id,
        action,
        item_name,
        size,
        quantity,
        args.get("price", 0.0),
    )

    order_summary = order_state_singleton.get_order_summary(session_id)
    json_order_summary = order_summary.model_dump_json()
    logger.debug("Session %s order summary after update: %s", session_id, json_order_summary)

    # The browser gets the full summary; the model gets an explicit success so
    # it can tell an accepted call from a rejected one.
    server_text = json.dumps({
        "status": "ok",
        "action": action,
        "item_name": item_name,
        "size": size,
        "quantity": quantity,
        "order_items": [oi.display or oi.item for oi in order_summary.items],
    }, ensure_ascii=False)
    return ToolResult(json_order_summary, ToolResultDirection.TO_CLIENT, server_text=server_text)


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
    credentials: AzureKeyCredential | DefaultAzureCredential | None = None,
    search_endpoint: str | None = None,
    search_index: str | None = None,
    semantic_configuration: str = "",
    identifier_field: str = "id",
    content_field: str = "description",
    embedding_field: str = "embedding",
    title_field: str = "name",
    use_vector_query: bool = True,
    use_semantic_ranker: bool = True,
    *,
    use_local_pipeline: bool = False,
    chroma_collection: Any = None,
) -> None:
    """Attach search and order tools to the RTMiddleTier instance.

    When *use_local_pipeline* is True, binds the ChromaDB search
    implementation (edge / Azure Local).  Otherwise binds the Azure AI
    Search implementation (cloud default).
    """

    if use_local_pipeline:
        if chroma_collection is None:
            raise ValueError("chroma_collection is required when use_local_pipeline is True")
        rtmt.tools["search"] = Tool(
            schema=search_tool_schema,
            target=lambda args: search_chromadb(chroma_collection, args),
        )
    else:
        if not isinstance(credentials, AzureKeyCredential):
            credentials.get_token("https://search.azure.com/.default")  # warm up prior to first call
        search_client = SearchClient(search_endpoint, search_index, credentials, user_agent="RTMiddleTier")
        rtmt.tools["search"] = Tool(schema=search_tool_schema, target=lambda args: search(search_client, semantic_configuration, identifier_field, content_field, embedding_field, use_vector_query, args, use_semantic_ranker))

    rtmt.tools["update_order"] = Tool(schema=update_order_tool_schema, target=lambda args, session_id: update_order(args, session_id))
    rtmt.tools["get_order"] = Tool(schema=get_order_tool_schema, target=lambda args, session_id: get_order(args, session_id))



