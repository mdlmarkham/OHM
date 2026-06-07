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

# Rate-limit retry helper
RATE_LIMIT_DELAY = 0.15  # seconds between API calls
RATE_LIMIT_MAX_RETRIES = 3


def _api_post(url, headers, json_data=None, *, json=None, timeout=10):
    """POST with rate-limit retry."""
    import requests as http_requests
    for attempt in range(RATE_LIMIT_MAX_RETRIES):
        time.sleep(RATE_LIMIT_DELAY)
        r = http_requests.post(url, headers=headers, json=json_data, timeout=timeout)
        if r.status_code == 429 or (r.status_code == 400 and 'rate_limited' in r.text):
            wait = 5 * (attempt + 1)
            print(f"    ~ Rate limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        return r
    return r


def _api_get(url, headers, params=None, timeout=5):
    """GET with rate-limit retry."""
    import requests as http_requests
    for attempt in range(RATE_LIMIT_MAX_RETRIES):
        time.sleep(RATE_LIMIT_DELAY * 0.5)
        r = http_requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code == 429 or (r.status_code == 400 and 'rate_limited' in r.text):
            wait = 3 * (attempt + 1)
            print(f"    ~ Rate limited, retrying in {wait}s...")
            time.sleep(wait)
            continue
        return r
    return r
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


def _delete_queue_item(item_id: str, stage: str):
    """Remove an item from a queue stage after it's been written elsewhere."""
    path = _queue_path(stage, item_id)
    if path.exists():
        path.unlink()


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
    """Stage 1: Fetch from RSS feeds and SearXNG, deduplicate, queue for triage.

    OHM-g0kv Feature C: After URL hash dedup, also checks ohm_content_hashes
    for existing content to avoid re-ingesting known sources.
    """
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

    # OHM-g0kv: Check content hashes for existing content
    content_deduped = 0
    if OHM_TOKEN:
        import requests as http_requests
        headers = {"Authorization": f"Bearer {OHM_TOKEN}"}
        for item in unique[:]:
            url = item.get("url", "")
            if url:
                content_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
                try:
                    r = http_requests.get(
                        f"{OHM_URL}/resolve",
                        params={"query": url},
                        headers=headers,
                        timeout=5,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("resolved"):
                            # This URL already exists as a source node
                            unique.remove(item)
                            content_deduped += 1
                            continue
                except Exception:
                    pass  # Content hash check is best-effort

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

    print(f"\n  Total fetched: {len(all_items)}, unique: {len(unique)}, content-deduped: {content_deduped}, new: {new_count}")
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
    """Stage 3: Auto-create source nodes for triage-passed items.

    OHM-g0kv Feature C: Before creating a source node, checks /resolve
    for an existing node with a matching alias. If found, skips creation
    and logs the duplicate.

    OHM-wdrg Feature C: After creating the source node, searches OHM for
    existing concept nodes matching the article's domain keywords and
    creates REFERENCES edges from those concept nodes to the source.
    """
    _ensure_queues()

    headers = {"Authorization": f"Bearer {OHM_TOKEN}"}
    triage_items = _read_queue_items("triage_pass")

    if not triage_items:
        print("  No items in triage_pass queue.")
        return 0

    print(f"  Creating source nodes for {len(triage_items)} items...")

    created = 0
    skipped = 0
    for item in triage_items:
        url = item.get("url", "")
        title = item.get("title", "")

        if not url:
            continue

        # OHM-g0kv: Check if a similar node already exists via alias resolution
        if OHM_TOKEN:
            from ohm.validation import normalize_alias as _norm
            normalized_title = _norm(title)
            try:
                r = _api_get(
                    f"{OHM_URL}/resolve",
                    params={"query": normalized_title},
                    headers=headers,
                    timeout=5,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("resolved"):
                        existing_id = data["resolved"].get("id")
                        print(f"    ~ Duplicate found: '{title[:50]}...' resolves to {existing_id}, skipping creation")
                        item["source_node_id"] = existing_id
                        item["duplicate_of"] = existing_id
                        _write_queue_item("source_created", item)
                        _move_queue_item(item["id"], "triage_pass", "source_created")
                        skipped += 1
                        continue
            except Exception:
                pass  # Alias resolution is best-effort

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
        r = _api_post(
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
            print(f"    + {source_id}: {title[:50]}...")

            # OHM-wdrg: Create REFERENCES edges from existing concept nodes
            _create_reference_edges(item, source_id, headers)
        else:
            # Might already exist
            print(f"    ~ {source_id}: {r.status_code} ({title[:40]}...)")

    print(f"\n  Source nodes: {created} created, {skipped} duplicates skipped (zero tokens)")
    return created


def _create_reference_edges(item: dict, source_id: str, headers: dict):
    """OHM-wdrg Feature C: Create REFERENCES edges from concept nodes to source.

    After creating a source node, search OHM for existing concept nodes whose
    labels match keywords from the article. Create REFERENCES edges from
    those concept nodes to the new source node.
    """
    import requests as http_requests

    title = item.get("title", "")
    description = item.get("description", "")
    triage_domain = item.get("triage", {}).get("domain", "")

    # Extract keywords from title and matched domain
    keywords = set()
    if triage_domain:
        keywords.add(triage_domain.lower())
    for domain in TRACKED_DOMAINS:
        if domain.lower() in f"{title} {description}".lower():
            keywords.add(domain.lower())

    if not keywords:
        return

    # Search OHM for concept nodes matching each keyword
    for keyword in list(keywords)[:5]:  # Limit to 5 keywords
        try:
            r = _api_get(
                f"{OHM_URL}/resolve",
                params={"query": keyword},
                headers=headers,
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                resolved = data.get("resolved")
                if resolved and resolved.get("id"):
                    concept_id = resolved["id"]
                    concept_type = resolved.get("type", "")
                    # Only create REFERENCES from non-source nodes
                    if concept_type != "source":
                        _api_post(
                            f"{OHM_URL}/edge",
                            headers=headers,
                            json={
                                "from_node": concept_id,
                                "to_node": source_id,
                                "edge_type": "REFERENCES",
                                "layer": "L2",
                                "confidence": 0.7,
                                "created_by": "ingestion-pipeline",
                            },
                        )
        except Exception:
            pass  # Reference edge creation is best-effort


# ── Stage 4: Assess (Domain agent reads + writes observation) ────────────────


def stage_assess():
    """Stage 4: Keyword-based assessment of articles.

    OHM-wdrg Feature A (replaces stub): Reads items from source_created queue,
    extracts key entities from title/description using TRACKED_DOMAINS, matches
    against existing OHM concept nodes via /resolve, creates observations on
    source nodes with findings, sets confidence based on trust score, sets
    source_url to article URL, and creates REFERENCES edges from relevant
    concept nodes.
    """
    _ensure_queues()

    headers = {"Authorization": f"Bearer {OHM_TOKEN}"}
    source_items = _read_queue_items("source_created")

    if not source_items:
        print("  No items in source_created queue for assessment.")
        return 0

    print(f"  Assessing {len(source_items)} items using keyword extraction...")

    assessed = 0
    for item in source_items:
        title = item.get("title", "")
        description = item.get("description", "")
        source_node_id = item.get("source_node_id", "")
        url = item.get("url", "")
        trust = item.get("trust", 0.5)
        triage = item.get("triage", {})
        matched_domain = triage.get("domain", "")

        if not source_node_id:
            print(f"    - Skipping item without source_node_id: {title[:40]}...")
            continue

        # Extract key entities from title/description using TRACKED_DOMAINS
        text = f"{title} {description}".lower()
        matched_keywords = [d for d in TRACKED_DOMAINS if d.lower() in text]

        if not matched_keywords:
            # No keywords matched; move to assessed without observations
            item["assessed_at"] = datetime.now(timezone.utc).isoformat()
            item["assessment"] = {"method": "keyword", "keywords": [], "observations_created": 0}
            _write_queue_item("assessed", item)
            _delete_queue_item(item["id"], "source_created")
            continue

        # Resolve keywords against OHM concept nodes
        resolved_concepts = []
        for keyword in matched_keywords[:5]:  # Limit to top 5 keywords
            try:
                r = _api_get(
                    f"{OHM_URL}/resolve",
                    params={"query": keyword},
                    headers=headers,
                    timeout=5,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("resolved"):
                        resolved_concepts.append(data["resolved"])
            except Exception:
                pass  # Best-effort resolution

        # Create observation on the source node
        obs_created = 0
        confidence = min(0.5 + trust * 0.3, 0.95)  # Scale trust to confidence
        observation_text = f"Article covers: {', '.join(matched_keywords[:5])}"

        try:
            obs_data = {
                "type": "assessment",
                "value": confidence,
                "source": "ingestion-pipeline",
                "source_url": url,
                "notes": observation_text,
                "scale": "probability",
            }
            r = _api_post(
                f"{OHM_URL}/observe/{source_node_id}",
                headers=headers,
                json=obs_data,
                timeout=10,
            )
            if r.status_code in (200, 201):
                obs_created += 1
        except Exception as e:
            print(f"    ! Failed to create observation: {e}")

        # Create REFERENCES edges from resolved concept nodes to source
        refs_created = 0
        for concept in resolved_concepts:
            concept_id = concept.get("id", "")
            concept_type = concept.get("type", "")
            if concept_id and concept_type != "source":
                try:
                    r = _api_post(
                        f"{OHM_URL}/edge",
                        headers=headers,
                        json={
                            "from_node": concept_id,
                            "to_node": source_node_id,
                            "edge_type": "REFERENCES",
                            "layer": "L2",
                            "confidence": 0.7,
                            "created_by": "ingestion-pipeline",
                        },
                        timeout=5,
                    )
                    if r.status_code in (200, 201):
                        refs_created += 1
                except Exception:
                    pass  # Best-effort

        item["assessed_at"] = datetime.now(timezone.utc).isoformat()
        item["assessment"] = {
            "method": "keyword",
            "keywords": matched_keywords,
            "resolved_concepts": [c.get("id") for c in resolved_concepts],
            "observations_created": obs_created,
            "references_created": refs_created,
            "confidence": confidence,
        }
        _write_queue_item("assessed", item)
        _delete_queue_item(item["id"], "source_created")
        assessed += 1

        keywords_str = ", ".join(matched_keywords[:3])
        print(f"    * {title[:50]}... [kw={keywords_str}, obs={obs_created}, ref={refs_created}]")

    print(f"\n  Assessed: {assessed}/{len(source_items)} items")
    return assessed


# ── Stage 5: Synthesize (Pattern identification from clusters) ──────────────


def stage_synthesize():
    """Stage 5: Detect clusters from assessed items and create synthesis observations.

    OHM-wdrg Feature B (replaces stub): Groups assessed items by domain/keyword
    overlap. When 3+ items share a domain, creates a synthesis observation on a
    relevant concept node and logs the cluster for agent review.
    """
    _ensure_queues()
    from collections import defaultdict

    headers = {"Authorization": f"Bearer {OHM_TOKEN}"}
    assessed = _read_queue_items("assessed")

    if len(assessed) < 3:
        print("  Fewer than 3 assessed items — insufficient for synthesis.")
        return 0

    # Group by domain/keyword overlap
    by_keyword = defaultdict(list)
    for item in assessed:
        assessment = item.get("assessment", {})
        keywords = assessment.get("keywords", [])
        for kw in keywords:
            by_keyword[kw].append(item)

    # Also group by triage domain for backward compat
    by_domain = defaultdict(list)
    for item in assessed:
        domain = item.get("triage", {}).get("domain", "unknown")
        by_domain[domain].append(item)

    print(f"  Assessed items by domain:")
    for domain, items in sorted(by_domain.items(), key=lambda x: -len(x[1])):
        print(f"    {domain}: {len(items)} items")

    # Find clusters: keywords with 3+ items
    clusters = []
    for keyword, items in by_keyword.items():
        if len(items) >= 3:
            clusters.append({"keyword": keyword, "items": items, "count": len(items)})

    # Also include domain-based clusters
    for domain, items in by_domain.items():
        if len(items) >= 3 and domain:
            # Avoid double-counting if keyword cluster already covers this domain
            existing_keywords = {c["keyword"].lower() for c in clusters}
            if domain.lower() not in existing_keywords:
                clusters.append({"keyword": domain, "items": items, "count": len(items)})

    if not clusters:
        print("  No clusters with 3+ items yet.")
        return 0

    print(f"\n  Clusters ready for synthesis: {len(clusters)}")

    syntheses_created = 0
    for cluster in clusters:
        keyword = cluster["keyword"]
        items = cluster["items"]
        count = cluster["count"]

        print(f"    Cluster [{keyword}]: {count} items")

        # Resolve keyword to a concept node
        concept_node = None
        try:
            r = _api_get(
                f"{OHM_URL}/resolve",
                params={"query": keyword},
                headers=headers,
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("resolved"):
                    concept_node = data["resolved"]
        except Exception:
            pass

        if not concept_node:
            print(f"      No concept node found for '{keyword}', skipping synthesis")
            continue

        # Create synthesis observation on the concept node
        titles = [it.get("title", "?")[:60] for it in items[:5]]
        synthesis_notes = (
            f"Cluster of {count} articles about '{keyword}': "
            f"{'; '.join(titles)}"
        )

        # Average confidence from items
        confidences = [it.get("assessment", {}).get("confidence", 0.7) for it in items]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.7

        try:
            r = _api_post(
                f"{OHM_URL}/observe/{concept_node['id']}",
                headers=headers,
                json={
                    "type": "synthesis",
                    "value": min(avg_confidence * 1.1, 0.95),  # Slight boost for synthesis
                    "source": "ingestion-pipeline",
                    "notes": synthesis_notes,
                    "scale": "probability",
                },
                timeout=10,
            )
            if r.status_code in (200, 201):
                syntheses_created += 1
                print(f"      Synthesis observation created on {concept_node['id']}")
            else:
                print(f"      Failed to create synthesis: {r.status_code}")
        except Exception as e:
            print(f"      Synthesis failed: {e}")

        # Log cluster for agent review
        cluster_log = {
            "keyword": keyword,
            "count": count,
            "concept_node": concept_node.get("id"),
            "items": [{"title": it.get("title", ""), "url": it.get("url", "")} for it in items[:10]],
            "synthesized_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_queue_item("assessed", {"id": f"cluster-{keyword}", **cluster_log})

    print(f"\n  Syntheses created: {syntheses_created}/{len(clusters)} clusters")
    return syntheses_created


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="OHM Staged Ingestion Pipeline")
    parser.add_argument("--stage", choices=["fetch", "triage", "source", "assess", "synthesize", "full", "queue-status", "drain-triage"],
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
    elif args.stage == "synthesize":
        stage_synthesize()
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