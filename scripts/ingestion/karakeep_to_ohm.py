#!/usr/bin/env python3
"""Karakeep → OHM bridge.

Reads Karakeep webhook bookmarks from the Karakeep receiver queue and writes
document items to OHM's ingestion pipeline so they are stored, extracted, and
ingested as OHM document-tree source nodes.

This is a companion to the existing Karakeep Zettelkasten processor. It does
not remove bookmarks from the Karakeep queue; it tracks its own processed IDs
so each bookmark is only enqueued to OHM once.

Usage:
    python3 scripts/ingestion/karakeep_to_ohm.py
    python3 scripts/ingestion/karakeep_to_ohm.py --once
    python3 scripts/ingestion/karakeep_to_ohm.py --karakeep-queue /path/to/bookmarks.json
    python3 scripts/ingestion/karakeep_to_ohm.py --ohm-queue-dir /var/lib/ohm/ingestion
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ohm.ingestion.document_library_bridge import queue_document_item


DEFAULT_KARAKEEP_QUEUE = Path("/root/olympus/karakeep-webhook/queue/bookmarks.json")
DEFAULT_STATE_FILE = Path("/var/lib/ohm/ingestion/karakeep_to_ohm_state.json")
DEFAULT_OHM_QUEUE_DIR = Path("/var/lib/ohm/ingestion")

# URL extensions and content types that are worth storing as documents.
DOCUMENT_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}


def _load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {"processed_ids": []}
    try:
        with open(state_file) as f:
            return json.load(f)
    except Exception:
        return {"processed_ids": []}


def _save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _is_document_url(url: str) -> bool:
    """Return True if the URL points to content worth storing as a document."""
    if not url:
        return False
    lowered = url.split("?")[0].split("#")[0].lower()
    return any(lowered.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


def _extract_tags(bookmark: dict[str, Any]) -> list[str]:
    """Build OHM tags from a Karakeep bookmark."""
    tags: list[str] = ["karakeep"]
    karakeep_tags = bookmark.get("tags", []) or []
    for tag in karakeep_tags:
        if isinstance(tag, dict):
            name = tag.get("name") or tag.get("id")
        else:
            name = str(tag)
        if name:
            tags.append(name.lower().replace(" ", "_"))
    theme = bookmark.get("theme")
    if theme:
        tags.append(theme.lower().replace("/", "_").replace(" ", "_"))
    return tags


def _extract_url(bookmark: dict[str, Any]) -> str | None:
    """Get the canonical URL from a Karakeep bookmark payload."""
    if isinstance(bookmark, dict):
        content = bookmark.get("content", {})
        if isinstance(content, dict):
            url = content.get("url") or bookmark.get("url")
            if url:
                return url
        return bookmark.get("url")
    return None


def _convert_bookmark_to_document_item(
    bookmark: dict[str, Any],
    queue_dir: Path,
    source: str = "karakeep",
) -> str | None:
    """Convert a Karakeep bookmark into an OHM document queue item.

    Returns the OHM queue item ID, or None if the bookmark should be skipped.
    """
    url = _extract_url(bookmark)
    if not url:
        return None

    # Only queue documents/PDFs/web pages. Plain articles without a file
    # extension are better handled by the existing RSS/article source pipeline.
    if not _is_document_url(url):
        return None

    title = bookmark.get("title", "Untitled") or "Untitled"
    filename = Path(url.split("?")[0].split("#")[0]).name or "document"
    ext = Path(filename).suffix.lower()
    content_type = DOCUMENT_EXTENSIONS.get(ext)

    tags = _extract_tags(bookmark)
    metadata = {
        "karakeep_id": bookmark.get("id"),
        "publisher": bookmark.get("publisher"),
        "author": bookmark.get("author"),
        "date": bookmark.get("date"),
    }

    item_id = queue_document_item(
        queue_dir,
        title=title,
        url=url,
        filename=filename,
        content_type=content_type,
        source=source,
        tags=tags,
        metadata=metadata,
        target_stage="triage_pass",
    )
    return item_id


def run_bridge(
    *,
    karakeep_queue: Path,
    ohm_queue_dir: Path,
    state_file: Path,
    dry_run: bool = False,
) -> int:
    """Read Karakeep queue, convert document bookmarks to OHM document items."""
    state = _load_state(state_file)
    processed_ids: set[str] = set(state.get("processed_ids", []))

    if not karakeep_queue.exists():
        print(f"  Karakeep queue not found: {karakeep_queue}")
        return 0

    try:
        with open(karakeep_queue) as f:
            data = json.load(f)
    except Exception as e:
        print(f"  Failed to read Karakeep queue: {e}")
        return 0

    # The receiver writes a list of {id, received, bookmark} entries.
    entries: list[dict[str, Any]] = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and "bookmarks" in data:
        # Some export/sync formats have a top-level bookmarks list.
        entries = [{"bookmark": b} for b in data["bookmarks"]]

    new_ids: list[str] = []
    queued = 0
    skipped = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        bookmark = entry.get("bookmark", entry)
        bookmark_id = bookmark.get("id")
        if not bookmark_id:
            continue

        if bookmark_id in processed_ids:
            skipped += 1
            continue

        title = bookmark.get("title", "Untitled")[:60]
        url = _extract_url(bookmark)

        if not _is_document_url(url or ""):
            print(f"    ~ Skip (not a document): {title}")
            processed_ids.add(bookmark_id)
            new_ids.append(bookmark_id)
            continue

        if dry_run:
            print(f"    [dry-run] Would queue: {title} ({url})")
        else:
            item_id = _convert_bookmark_to_document_item(bookmark, ohm_queue_dir)
            if item_id:
                print(f"    + Queued OHM document: {title} (item={item_id})")
                queued += 1
            else:
                print(f"    ~ Skip: {title}")

        processed_ids.add(bookmark_id)
        new_ids.append(bookmark_id)

    if not dry_run and new_ids:
        state["processed_ids"] = sorted(processed_ids)
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        _save_state(state_file, state)

    print(f"\n  Karakeep→OHM: {queued} queued, {skipped} already processed, {len(entries)} total")
    return queued


def main():
    parser = argparse.ArgumentParser(description="Karakeep to OHM document bridge")
    parser.add_argument("--karakeep-queue", type=Path, default=DEFAULT_KARAKEEP_QUEUE)
    parser.add_argument("--ohm-queue-dir", type=Path, default=DEFAULT_OHM_QUEUE_DIR)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval-seconds", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Karakeep→OHM bridge starting")
    print(f"  Karakeep queue: {args.karakeep_queue}")
    print(f"  OHM queue dir:  {args.ohm_queue_dir}")
    print(f"  State file:     {args.state_file}")

    while True:
        run_bridge(
            karakeep_queue=args.karakeep_queue,
            ohm_queue_dir=args.ohm_queue_dir,
            state_file=args.state_file,
            dry_run=args.dry_run,
        )

        if args.once:
            break

        print(f"\n  Sleeping {args.interval_seconds}s...")
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
