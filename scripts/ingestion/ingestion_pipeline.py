#!/usr/bin/env python3
"""Staged Ingestion Pipeline for OHM — ADR-016

Five-stage pipeline with agent gates at each level:
1. INGEST: Fetch + parse + deduplicate (zero tokens)
2. TRIAGE: Cheap model filters relevant/novel items (~50 tokens each)
3. SOURCE: Auto-create source nodes for passed items (zero tokens)
4. ASSESS: Domain agent reads full article, writes observations (~300 tokens)
5. SYNTHESIZE: Strong agent identifies patterns from clusters (~1000 tokens, rare)

Token-value ladder: match model cost to decision value at each stage.
Most items die at Stage 2 for ~50 tokens. Only survivors reach Stage 4+.

Usage:
    python3 ingestion_pipeline.py --stage fetch          # Stage 1 only
    python3 ingestion_pipeline.py --stage triage          # Stage 1-2
    python3 ingestion_pipeline.py --stage source          # Stage 1-3
    python3 ingestion_pipeline.py --stage assess          # Stage 1-4 (needs agent)
    python3 ingestion_pipeline.py --stage full           # All stages
    python3 ingestion_pipeline.py --queue-status         # Check queue depths
    python3 ingestion_pipeline.py --drain-triage        # Process triage queue
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ── Configuration ────────────────────────────────────────────────────────────

QUEUE_DIR = Path("/var/lib/ohm/ingestion")
OHM_URL = os.environ.get("OHM_URL", "http://127.0.0.1:8710")
OHM_TOKEN = os.environ.get("OHM_TOKEN", "")

# Auto-load token from ohmd config if not set
if not OHM_TOKEN:
    _cfg_path = "/etc/ohm/ohmd.json"
    if os.path.exists(_cfg_path):
        try:
            with open(_cfg_path) as _f:
                _cfg = json.load(_f)
            _tokens = _cfg.get("tokens", {})
            if isinstance(_tokens, dict):
                OHM_TOKEN = list(_tokens.values())[0] if _tokens else ""
            elif isinstance(_tokens, list):
                OHM_TOKEN = _tokens[0] if _tokens else ""
        except Exception:
            pass

# Triage model (cheap, fast)
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "ollama/glm-5:cloud")
# Assessment model (domain-aware, medium cost)
ASSESS_MODEL = os.environ.get("ASSESS_MODEL", "ollama/glm-5:cloud")
# Synthesis model (strong, expensive)
SYNTHESIS_MODEL = os.environ.get("SYNTHESIS_MODEL", "ollama/glm-5:cloud")

# Tracked domains for triage relevance check
TRACKED_DOMAINS = [
    "hormuz", "iran", "oil", "oil price", "brent", "WTI", "strait",
    "warsh", "fed", "federal reserve", "rate hike", "interest rate", "PCE",
    "abraham accords", "saudi", "israel", "lebanon", "hezbollah", "ceasefire",
    "agent governance", "AI governance", "AI agent", "identity security",
    "demand rationing", "transit", "shipping", "tanker",
    "AND-gate", "OR-gate", "doom loop",
]

# ── Queue Management ────────────────────────────────────────────────────────


def _ensure_queues():
    """Create queue directories if they don't exist."""
    for stage in ("raw", "triage_pass", "triage_fail", "source_created", "assessed"):
        (QUEUE_DIR / stage).mkdir(parents=True, exist_ok=True)


def _queue_path(stage: str, item_id: str) -> Path:
    return QUEUE_DIR / stage / f"{item_id}.json"


def _write_queue_item(stage: str, item: dict):
    item_id = item.get("id", hashlib.md5(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16])
    item["id"] = item_id
    path = _queue_path(stage, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(item, f, indent=2, default=str)
    return item_id


def _read_queue_items(stage: str) -> list[dict]:
    stage_dir = QUEUE_DIR / stage
    if not stage_dir.exists():
        return []
    items = []
    for p in sorted(stage_dir.glob("*.json")):
        try:
            items.append(json.loads(p.read_text()))
        except Exception:
            pass
    return items


def _move_queue_item(item_id: str, from_stage: str, to_stage: str):
    src = _queue_path(from_stage, item_id)
    dst = _queue_path(to_stage, item_id)
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)


def queue_status():
    """Print current queue depths."""
    _ensure_queues()
    print("Ingestion Queue Status:")
    print(f"  {'Stage':<20} {'Count':>6}")
    print(f"  {'─' * 26}")
    for stage in ("raw", "triage_pass", "triage_fail", "source_created", "assessed"):
        items = _read_queue_items(stage)
        label = stage.replace("_", " ").title()
        print(f"  {label:<20} {len(items):>6}")


# ── Stage 1: Ingest (Fetch + Parse + Deduplicate) ────────────────────────────


def _fetch_rss_feed(url: str, category: str = "general", trust: float = 0.5) -> list[dict]:
    """Fetch and parse an RSS feed. Returns raw items."""
    import xml.etree.ElementTree as ET

    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "OHM-Ingestion/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except Exception as e:
        print(f"  Failed to fetch {url}: {e}")
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"  Failed to parse {url}: {e}")
        return []

    items = []
    for item in root.iter("item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        pub_date = item.findtext("pubDate", "")
        description = item.findtext("description", "").strip()

        if not title or not link:
            continue

        # Deduplicate by URL hash
        url_hash = hashlib.md5(link.encode()).hexdigest()[:16]

        items.append({
            "id": url_hash,
            "title": title,
            "url": link,
            "description": description[:500],
            "pub_date": pub_date,
            "category": category,
            "trust": trust,
            "source_feed": url,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        })

    return items


def _fetch_searxng(query: str, category: str = "search") -> list[dict]:
    """Fetch results from SearXNG instance."""
    from urllib.parse import quote_plus
    searxng_url = os.environ.get("SEARXNG_URL", "http://192.168.70.101:8083")

    try:
        import urllib.request
        url = f"{searxng_url}/search?q={quote_plus(query)}&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "OHM-Ingestion/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  SearXNG failed for '{query}': {e}")
        return []

    items = []
    for r in data.get("results", []):
        link = r.get("url", "")
        title = r.get("title", "")
        if not link or not title:
            continue
        url_hash = hashlib.md5(link.encode()).hexdigest()[:16]
        items.append({
            "id": url_hash,
            "title": title,
            "url": link,
            "description": r.get("content", "")[:500],
            "pub_date": "",
            "category": category,
            "trust": 0.4,  # SearXNG results vary in trust
            "source_feed": f"searxng:{query}",
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        })

    return items


def stage_fetch():
    """Stage 1: Fetch from RSS feeds and SearXNG, deduplicate, queue for triage."""
    _ensure_queues()

    # RSS feeds to check
    rss_feeds = [
        ("https://www.investing.com/rss/news.rss", "market", 0.5),
        ("https://www.investing.com/rss/commodities.rss", "commodities", 0.5),
    ]

    # SearXNG queries for tracked domains
    searxng_queries = [
        "Hormuz strait oil shipping 2026",
        "Warsh Federal Reserve rate hike 2026",
        "Abraham Accords normalization 2026",
        "AI agent governance regulation 2026",
        "oil price Brent WTI today",
    ]

    all_items = []
    for url, cat, trust in rss_feeds:
        items = _fetch_rss_feed(url, cat, trust)
        all_items.extend(items)
        print(f"  RSS {cat}: {len(items)} items from {url[:50]}...")

    for query in searxng_queries:
        items = _fetch_searxng(query, "search")
        all_items.extend(items)
        print(f"  SearXNG '{query[:40]}...': {len(items)} items")

    # Deduplicate by ID
    seen = set()
    unique = []
    for item in all_items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)

    # Check if already in raw queue or further stages
    existing_ids = set()
    for stage in ("raw", "triage_pass", "source_created", "assessed"):
        for existing in _read_queue_items(stage):
            existing_ids.add(existing.get("id", ""))

    new_count = 0
    for item in unique:
        if item["id"] not in existing_ids:
            _write_queue_item("raw", item)
            new_count += 1

    print(f"\n  Total fetched: {len(all_items)}, unique: {len(unique)}, new: {new_count}")
    return new_count


# ── Stage 2: Triage (Cheap model filter) ────────────────────────────────────


def _triage_with_model(item: dict) -> dict:
    """Use a cheap model to determine relevance and novelty.

    Returns: {"relevant": bool, "novel": bool, "reasoning": str, "domain": str}
    Token cost: ~50 tokens per item.
    """
    import requests as http_requests

    # Build a minimal prompt
    title = item.get("title", "")
    desc = item.get("description", "")[:200]
    domains_str = ", ".join(TRACKED_DOMAINS[:15])

    prompt = f"""Is this article relevant to any of these domains: {domains_str}?
And does it likely contain NEW information (not just repeating known events)?

Article: {title}
Summary: {desc}

Answer JSON only: {{"relevant": true/false, "novel": true/false, "domain": "matched_domain_or_none", "reasoning": "one sentence"}}"""

    # Fallback to keyword matching when model is unavailable
    # For production, this would call the cheap model (GLM-5) via OpenClaw
    title_lower = (title + " " + desc).lower()
    matched_domains = [d for d in TRACKED_DOMAINS if d.lower() in title_lower]
    relevant = len(matched_domains) > 0

    return {
        "relevant": relevant,
        "novel": True,  # Novelty check requires fetching full article (Stage 4)
        "domain": matched_domains[0] if matched_domains else "",
        "reasoning": f"keyword-match: {', '.join(matched_domains[:3])}" if matched_domains else "no keyword match",
    }


def stage_triage():
    """Stage 2: Run triage filter on raw queue items."""
    _ensure_queues()
    raw_items = _read_queue_items("raw")

    if not raw_items:
        print("  No items in raw queue to triage.")
        return 0

    print(f"  Triaging {len(raw_items)} raw items...")

    passed = 0
    failed = 0
    for item in raw_items:
        result = _triage_with_model(item)

        # Update item with triage result
        item["triage"] = result
        item["triaged_at"] = datetime.now(timezone.utc).isoformat()

        # Both relevant AND novel → pass
        if result["relevant"] and result["novel"]:
            _write_queue_item("triage_pass", item)
            _move_queue_item(item["id"], "raw", "triage_pass")
            passed += 1
            domain = result.get("domain", "?")
            print(f"    ✓ {item['title'][:50]}... [domain={domain}]")
        else:
            _write_queue_item("triage_fail", item)
            _move_queue_item(item["id"], "raw", "triage_fail")
            failed += 1
            reason = result.get("reasoning", "?")[:50]
            print(f"    ✗ {item['title'][:50]}... [{reason}]")

    print(f"\n  Triage: {passed} passed, {failed} filtered (token cost: ~{len(raw_items)*50} tokens)")
    return passed


def _is_reference_page(title: str, url: str) -> bool:
    """Filter out encyclopedic/reference pages that aren't news or analysis."""
    ref_indicators = [
        "wikipedia", "wikimedia", "chart - live", "price chart",
        "price - chart - historical", "about - iea", "investing.com canada",
        "spot prices for crude", "stock price, quote",
        "- price - chart", "complete guide to", "what is ai governance",
        "abraham accords - wikipedia", "abraham accords | peace",
        "abraham accords - middle east", "abraham accords - united states",
        "crude oil price, oil, energy",
        "eu artificial intelligence act | up-to-date",
        "partnership on ai", "elham fakhro",
    ]
    combined = (title + " " + url).lower()
    return any(ind in combined for ind in ref_indicators)


def drain_triage():
    """Process triage pass queue using keyword fallback (no model needed)."""
    _ensure_queues()
    raw_items = _read_queue_items("raw")

    if not raw_items:
        print("  No items in raw queue.")
        return 0

    print(f"  Keyword-triaging {len(raw_items)} raw items...")

    passed = 0
    for item in raw_items:
        title = item.get("title", "").lower()
        desc = item.get("description", "").lower()
        url = item.get("url", "")
        text = f"{title} {desc}"

        # Filter out reference pages
        if _is_reference_page(title, url):
            item["triage"] = {
                "relevant": False,
                "novel": False,
                "domain": "",
                "reasoning": "reference page filtered",
            }
            item["triaged_at"] = datetime.now(timezone.utc).isoformat()
            _write_queue_item("triage_fail", item)
            _move_queue_item(item["id"], "raw", "triage_fail")
            continue

        # Keyword match against tracked domains
        matched_domains = [d for d in TRACKED_DOMAINS if d.lower() in text]
        relevant = len(matched_domains) > 0

        item["triage"] = {
            "relevant": relevant,
            "novel": True,
            "domain": matched_domains[0] if matched_domains else "",
            "reasoning": f"keyword-match: {', '.join(matched_domains[:3])}" if matched_domains else "no keyword match",
        }
        item["triaged_at"] = datetime.now(timezone.utc).isoformat()

        if relevant:
            _write_queue_item("triage_pass", item)
            _move_queue_item(item["id"], "raw", "triage_pass")
            passed += 1
            print(f"    ✓ {item['title'][:60]} [{', '.join(matched_domains[:3])}]")
        else:
            _write_queue_item("triage_fail", item)
            _move_queue_item(item["id"], "raw", "triage_fail")

    print(f"\n  Keyword triage: {passed}/{len(raw_items)} passed (zero tokens)")
    return passed


# ── Stage 3: Source Node Creation ────────────────────────────────────────────


def stage_source():
    """Stage 3: Auto-create source nodes for triage-passed items."""
    _ensure_queues()
    import requests as http_requests

    headers = {"Authorization": f"Bearer {OHM_TOKEN}"}
    triage_items = _read_queue_items("triage_pass")

    if not triage_items:
        print("  No items in triage_pass queue.")
        return 0

    print(f"  Creating source nodes for {len(triage_items)} items...")

    created = 0
    for item in triage_items:
        url = item.get("url", "")
        title = item.get("title", "")

        if not url:
            continue

        # Derive source node ID from domain
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
        except Exception:
            domain = "unknown"

        # Create a unique source node ID
        source_id = f"src-{hashlib.md5(url.encode()).hexdigest()[:10]}"
        feed_url = item.get("source_feed", "")
        category = item.get("category", "general")
        trust = item.get("trust", 0.5)

        # Create source node in OHM
        r = http_requests.post(
            f"{OHM_URL}/node",
            headers=headers,
            json={
                "id": source_id,
                "label": title[:100],
                "type": "source",
                "source_url": url,
                "tags": ["feed-ingest", category, domain],
                "metadata": {
                    "feed": feed_url,
                    "trust": trust,
                    "triage_domain": item.get("triage", {}).get("domain", ""),
                },
                "created_by": "ingestion-pipeline",
            },
        )

        if r.status_code in (200, 201):
            created += 1
            item["source_node_id"] = source_id
            _write_queue_item("source_created", item)
            _move_queue_item(item["id"], "triage_pass", "source_created")
            print(f"    ✓ {source_id}: {title[:50]}...")
        else:
            # Might already exist
            print(f"    ~ {source_id}: {r.status_code} ({title[:40]}...)")

    print(f"\n  Source nodes created: {created}/{len(triage_items)} (zero tokens)")
    return created


# ── Stage 4: Assess (Domain agent reads + writes observation) ────────────────


def stage_assess():
    """Stage 4: Domain agent assesses article and writes observation to OHM.

    This is the expensive gate — only items that pass triage reach here.
    Token cost: ~300 tokens per item.
    """
    _ensure_queues()
    import requests as http_requests

    headers = {"Authorization": f"Bearer {OHM_TOKEN}"}
    source_items = _read_queue_items("source_created")

    if not source_items:
        print("  No items in source_created queue for assessment.")
        return 0

    print(f"  Assessing {len(source_items)} items (this requires agent intelligence)...")

    # This stage is intentionally left for agent implementation
    # It should be called by the domain agent (Clio/Metis) during heartbeat
    # The agent reads each item, fetches the full article, and writes:
    # 1. Observation with source_url pointing to the source node
    # 2. Optional: new edges if causal links are discovered

    print("  Stage 4 is agent-driven. Items queued for agent processing:")
    for item in source_items[:10]:
        title = item.get("title", "?")[:60]
        domain = item.get("triage", {}).get("domain", "?")
        src_id = item.get("source_node_id", "?")
        print(f"    [{domain}] {title} (source: {src_id})")

    return len(source_items)


# ── Stage 5: Synthesize (Pattern identification from clusters) ──────────────


def stage_synthesize():
    """Stage 5: Identify patterns from clusters of assessed items.

    This is the most expensive stage (~1000 tokens) but runs rarely.
    Triggered when 3+ assessed items share a domain or theme.
    """
    _ensure_queues()
    assessed = _read_queue_items("assessed")

    if len(assessed) < 3:
        print("  Fewer than 3 assessed items — insufficient for synthesis.")
        return 0

    # Group by domain
    from collections import defaultdict
    by_domain = defaultdict(list)
    for item in assessed:
        domain = item.get("triage", {}).get("domain", "unknown")
        by_domain[domain].append(item)

    print(f"  Assessed items by domain:")
    for domain, items in sorted(by_domain.items(), key=lambda x: -len(x[1])):
        print(f"    {domain}: {len(items)} items")

    clusters_ready = [d for d, items in by_domain.items() if len(items) >= 3]
    if clusters_ready:
        print(f"\n  Clusters ready for synthesis: {clusters_ready}")
        print("  (Synthesis is agent-driven — run via Clio or Metis)")
    else:
        print("  No clusters with 3+ items yet.")

    return len(clusters_ready)


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="OHM Staged Ingestion Pipeline")
    parser.add_argument("--stage", choices=["fetch", "triage", "source", "assess", "full", "queue-status", "drain-triage"],
                        default="queue-status")
    parser.add_argument("--ohm-url", default=OHM_URL)
    parser.add_argument("--ohm-token", default=OHM_TOKEN)
    args = parser.parse_args()

    # Update globals for downstream stages
    if args.ohm_url:
        globals()['OHM_URL'] = args.ohm_url
    if args.ohm_token:
        globals()['OHM_TOKEN'] = args.ohm_token

    if args.stage == "queue-status":
        queue_status()
    elif args.stage == "fetch":
        stage_fetch()
    elif args.stage == "triage":
        stage_triage()
    elif args.stage == "drain-triage":
        drain_triage()
    elif args.stage == "source":
        stage_source()
    elif args.stage == "assess":
        stage_assess()
    elif args.stage == "full":
        print("=== Stage 1: Fetch ===")
        stage_fetch()
        print("\n=== Stage 2: Triage ===")
        drain_triage()
        print("\n=== Stage 3: Source Node Creation ===")
        stage_source()
        print("\n=== Stage 4: Assess (agent-driven) ===")
        stage_assess()
        print("\n=== Stage 5: Synthesize (agent-driven) ===")
        stage_synthesize()


if __name__ == "__main__":
    main()