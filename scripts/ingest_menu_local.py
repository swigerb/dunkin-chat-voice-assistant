#!/usr/bin/env python3
"""Ingest Dunkin menu items from menuItems.json into a local ChromaDB collection.

Uses ONNX Runtime with all-MiniLM-L6-v2 for embedding generation.  Run once
(or whenever the menu changes) to rebuild the local vector store that the
backend queries when USE_LOCAL_PIPELINE=true.

Usage:
    python scripts/ingest_menu_local.py

Environment variables (all optional):
    CHROMA_DATA_PATH       – ChromaDB persist directory (default: app/backend/chroma_data)
    CHROMA_COLLECTION_NAME – collection name (default: menu_items)
    MENU_ITEMS_PATH        – path to menuItems.json
"""

import json
import logging
import os
import sys
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CHROMA_DATA_PATH = os.environ.get(
    "CHROMA_DATA_PATH",
    str(Path(__file__).resolve().parent.parent / "app" / "backend" / "chroma_data"),
)
COLLECTION_NAME = os.environ.get("CHROMA_COLLECTION_NAME", "menu_items")


def find_menu_json() -> Path:
    """Locate the menuItems.json file."""
    candidates = [
        Path(os.environ.get("MENU_ITEMS_PATH", "")),
        Path(__file__).resolve().parent.parent / "app" / "frontend" / "src" / "data" / "menuItems.json",
        Path(__file__).resolve().parent.parent / "app" / "backend" / "data" / "menuItems.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    print("Could not find menuItems.json. Set MENU_ITEMS_PATH env var.", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    menu_path = find_menu_json()
    logger.info("Reading menu data from %s", menu_path)

    with menu_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for category_entry in data.get("menuItems", []):
        category = category_entry.get("category", "Unknown")
        for item in category_entry.get("items", []):
            name = item.get("name", "Unknown")
            doc_id = name.lower().replace(" ", "-").replace("'", "")

            sizes_str = ", ".join(
                f"{s['size']}: ${s['price']}" for s in item.get("sizes", [])
            )

            doc_text = (
                f"{name}. {item.get('description', '')}. "
                f"{item.get('longDescription', '')} "
                f"Category: {category}. Sizes: {sizes_str}. "
                f"Caffeine: {item.get('caffeineContent', 'N/A')}. "
                f"Popularity: {item.get('popularity', 'N/A')}."
            )

            meta = {
                "name": name,
                "category": category,
                "description": item.get("description", ""),
                "longDescription": item.get("longDescription", ""),
                "sizes": sizes_str,
                "origin": item.get("origin", ""),
                "caffeineContent": item.get("caffeineContent", ""),
                "brewingMethod": item.get("brewingMethod", ""),
                "popularity": item.get("popularity", ""),
            }

            ids.append(doc_id)
            documents.append(doc_text)
            metadatas.append(meta)

    logger.info("Prepared %d menu items for ingestion", len(ids))

    embedding_fn = ONNXMiniLM_L6_V2()
    logger.info("Using ONNX MiniLM-L6-v2 embedding function")

    logger.info("Initializing ChromaDB at %s", CHROMA_DATA_PATH)
    client = chromadb.PersistentClient(path=CHROMA_DATA_PATH)

    # Delete existing collection if present and recreate
    try:
        client.delete_collection(COLLECTION_NAME)
        logger.info("Deleted existing collection '%s'", COLLECTION_NAME)
    except ValueError:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )

    batch_size = 50
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i : i + batch_size]
        batch_docs = documents[i : i + batch_size]
        batch_meta = metadatas[i : i + batch_size]
        collection.add(ids=batch_ids, documents=batch_docs, metadatas=batch_meta)
        logger.info("Ingested batch %d-%d", i, i + len(batch_ids))

    logger.info(
        "Done! %d items in collection '%s' at %s",
        collection.count(),
        COLLECTION_NAME,
        CHROMA_DATA_PATH,
    )


if __name__ == "__main__":
    main()
