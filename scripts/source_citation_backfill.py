#!/usr/bin/env python3
"""Source Citation Backfill — OHM-wdrg.3 & OHM-wdrg.7

Creates source nodes for external outlets referenced in observations,
then creates L2 REFERENCES edges from concept nodes to those sources.

Categories:
  - Named external (41%): guardian, reuters, al-monitor, bloomberg, etc.
  - Agent-authored (23%): metis, clio, socrates, etc.
  - Generic labels (23%): synthesis, analysis, web_research — leave as-is
  - No source (11%): no source field at all — leave as-is

Strategy:
  1. Define canonical source outlets with URLs
  2. For each multi-source observation, parse the compound source string
  3. Create source nodes (if not exists) with source_url
  4. Create L2 REFERENCES edges from concept nodes to source nodes
  5. For agent-authored sources, create agent-type source nodes
"""

import json
import re
import sys
from collections import defaultdict
import requests

# Configuration
OHM_URL = "http://127.0.0.1:8710"
TOKEN = "ohm-metis-u0-KEjbnU_WfJnmNq7rbzQ"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv

# Canonical source outlets — each with a known URL
CANONICAL_SOURCES = {
    "reuters": {"label": "Reuters", "url": "https://www.reuters.com"},
    "guardian": {"label": "The Guardian", "url": "https://www.theguardian.com"},
    "ap": {"label": "Associated Press", "url": "https://apnews.com"},
    "nyt": {"label": "The New York Times", "url": "https://www.nytimes.com"},
    "bloomberg": {"label": "Bloomberg", "url": "https://www.bloomberg.com"},
    "wsj": {"label": "The Wall Street Journal", "url": "https://www.wsj.com"},
    "cnbc": {"label": "CNBC", "url": "https://www.cnbc.com"},
    "cnn": {"label": "CNN", "url": "https://www.cnn.com"},
    "nbc": {"label": "NBC News", "url": "https://www.nbcnews.com"},
    "cbs": {"label": "CBS News", "url": "https://www.cbsnews.com"},
    "npr": {"label": "NPR", "url": "https://www.npr.org"},
    "al-monitor": {"label": "Al-Monitor", "url": "https://www.al-monitor.com"},
    "aljazeera": {"label": "Al Jazeera", "url": "https://www.aljazeera.com"},
    "axios": {"label": "Axios", "url": "https://www.axios.com"},
    "bbc": {"label": "BBC", "url": "https://www.bbc.com"},
    "economist": {"label": "The Economist", "url": "https://www.economist.com"},
    "ft": {"label": "Financial Times", "url": "https://www.ft.com"},
    "nikkei": {"label": "Nikkei Asia", "url": "https://asia.nikkei.com"},
    "time": {"label": "Time", "url": "https://time.com"},
    "invezz": {"label": "Invezz", "url": "https://invezz.com"},
    "tokenist": {"label": "Tokenist", "url": "https://tokenist.com"},
    "motley-fool": {"label": "The Motley Fool", "url": "https://www.fool.com"},
    "247wallst": {"label": "24/7 Wall St.", "url": "https://247wallst.com"},
    "seekingalpha": {"label": "Seeking Alpha", "url": "https://seekingalpha.com"},
    "yahoo": {"label": "Yahoo Finance", "url": "https://finance.yahoo.com"},
    "msn": {"label": "MSN", "url": "https://www.msn.com"},
    "polymarket": {"label": "Polymarket", "url": "https://polymarket.com"},
    "iea": {"label": "International Energy Agency", "url": "https://www.iea.org"},
    "eia": {"label": "U.S. Energy Information Administration", "url": "https://www.eia.gov"},
    "who": {"label": "World Health Organization", "url": "https://www.who.int"},
    "barrons": {"label": "Barron's", "url": "https://www.barrons.com"},
    "the-street": {"label": "The Street", "url": "https://www.thestreet.com"},
    "oilprice": {"label": "OilPrice.com", "url": "https://oilprice.com"},
    "cfr": {"label": "Council on Foreign Relations", "url": "https://www.cfr.org"},
    "centcom": {"label": "U.S. Central Command", "url": "https://www.centcom.mil"},
    "hindu": {"label": "The Hindu", "url": "https://www.thehindu.com"},
    "washington-examiner": {"label": "Washington Examiner", "url": "https://www.washingtonexaminer.com"},
    "cru": {"label": "CRU Group", "url": "https://www.crugroup.com"},
    "windward": {"label": "Windward", "url": "https://windward.ai"},
    "rfe-rl": {"label": "Radio Free Europe/Radio Liberty", "url": "https://www.rferl.org"},
    "mehrnews": {"label": "Mehr News Agency (Iran)", "url": "https://en.mehrnews.com"},
    "irgc": {"label": "IRGC Statement", "url": "https://www.irgc.ir"},
    "kpler": {"label": "Kpler", "url": "https://www.kpler.com"},
    "vortexa": {"label": "Vortexa", "url": "https://www.vortexa.com"},
    "yara": {"label": "Yara International", "url": "https://www.yara.com"},
    "ifa": {"label": "International Fertilizer Association", "url": "https://www.ifa.fr"},
    "nomura": {"label": "Nomura", "url": "https://www.nomura.com"},
    "piper-sandler": {"label": "Piper Sandler", "url": "https://www.pipersandler.com"},
    "servicenow": {"label": "ServiceNow", "url": "https://www.servicenow.com"},
    "venturebeat": {"label": "VentureBeat", "url": "https://venturebeat.com"},
    "forbes": {"label": "Forbes", "url": "https://www.forbes.com"},
    "techrepublic": {"label": "TechRepublic", "url": "https://www.techrepublic.com"},
    "harrison": {"label": "Harrison (PraxisUWC)", "url": "https://www.praxisuwc.org"},
    "aol": {"label": "AOL", "url": "https://www.aol.com"},
    "usatoday": {"label": "USA Today", "url": "https://www.usatoday.com"},
    "nypost": {"label": "New York Post", "url": "https://nypost.com"},
    "cryptobriefing": {"label": "Crypto Briefing", "url": "https://cryptobriefing.com"},
    "beincrypto": {"label": "BeInCrypto", "url": "https://beincrypto.com"},
    "jalopnik": {"label": "Jalopnik", "url": "https://jalopnik.com"},
    "washexaminer": {"label": "Washington Examiner", "url": "https://www.washingtonexaminer.com"},
    "tfi": {"label": "Transport & Infrastructure", "url": "https://www.tfi.gov"},
    "cruz_graham_wicker": {"label": "U.S. Senators Cruz/Graham/Wicker", "url": "https://www.senate.gov"},
    "trump_truth_social": {"label": "Trump Truth Social", "url": "https://truthsocial.com"},
    "csis": {"label": "Center for Strategic and International Studies", "url": "https://www.csis.org"},
}

# Agent-authored sources — map to agent IDs
AGENT_SOURCES = {
    "metis": "agent-metis",
    "metis-synthesis": "agent-metis",
    "metis_research": "agent-metis",
    "metis_pattern_analysis": "agent-metis",
    "metis_response_to_socrates": "agent-metis",
    "metis_bayesian_analysis": "agent-metis",
    "metis-test": "agent-metis",
    "metis_test": "agent-metis",
    "metis-heartbeat": "agent-metis",
    "metis-critical-review": "agent-metis",
    "clio-research-2026-05-26": "agent-clio",
    "clio_research_20260524": "agent-clio",
    "clio_research": "agent-clio",
    "hephaestus_kill_chain_review": "agent-hephaestus",
    "hephaestus_audit": "agent-hephaestus",
    "hephaestus-synthesis": "agent-hephaestus",
    "mara_companion": "agent-mara",
    "mara-synthesis": "agent-mara",
    "deepthought-synthesis": "agent-deepthought",
    "socrates": "agent-socrates",
    "mentor": "agent-atlas",
    "evening_heartbeat": "agent-metis",
    "nova-career-analysis": "agent-nova",
    "nova-hollins-analysis": "agent-nova",
    "nova-tribal-research": "agent-nova",
}

# Generic process labels — not mapped to source nodes
GENERIC_SOURCES = {"synthesis", "analysis", "web_research", "research", "market_data", "test", "test-script", "multi_source_may24", "multi_source_2026_05_27"}


def parse_compound_source(source_str: str | None) -> list[str]:
    """Parse a compound source string like 'guardian_reuters_ap' into individual outlets."""
    if not source_str:
        return []

    # Known compound patterns with dates
    source_str = re.sub(r'_\d{4}_\d{2}_\d{2}$', '', source_str)  # Remove date suffix
    source_str = re.sub(r'_may\d{4}$', '', source_str)  # Remove may2026 suffix
    source_str = re.sub(r'-may-\d{4}$', '', source_str)  # Remove -may-2026 suffix
    source_str = re.sub(r'-may\d{4}$', '', source_str)  # Remove -may2026 suffix
    source_str = re.sub(r'_may\d{2}$', '', source_str)  # Remove _may24 suffix
    source_str = re.sub(r'_apr\d{4}$', '', source_str)  # Remove _apr2026 suffix
    source_str = re.sub(r'_day\d+$', '', source_str)  # Remove _day96 suffix
    source_str = re.sub(r'_dec\d{4}$', '', source_str)  # Remove _dec2025 suffix
    source_str = re.sub(r'_\d{8}$', '', source_str)  # Remove _20260524 suffix

    # Handle hyphenated compounds
    parts = re.split(r'[-_]', source_str)

    # Try to match known canonical sources from the parts
    matched = []
    remaining = list(parts)

    # Greedy matching — try longer names first
    for name in sorted(CANONICAL_SOURCES.keys(), key=len, reverse=True):
        name_variants = [name, name.replace("-", "_"), name.replace("-", "")]
        for variant in name_variants:
            if variant in source_str.lower():
                if name not in matched:
                    matched.append(name)
                break

    return matched


def create_source_node(outlet_key: str, outlet_info: dict) -> dict | None:
    """Create a source node via the OHM API."""
    node_id = f"source-{outlet_key}"
    payload = {
        "id": node_id,
        "label": outlet_info["label"],
        "node_type": "source",
        "source_url": outlet_info["url"],
    }

    if DRY_RUN:
        print(f"  [DRY] Would create source node: {node_id} -> {outlet_info['url']}")
        return {"id": node_id, **payload}

    try:
        resp = requests.post(f"{OHM_URL}/node", json=payload, headers=HEADERS, timeout=10)
        if resp.status_code == 409:
            # Already exists, update with source_url if missing
            print(f"  Source node already exists: {node_id}")
            return {"id": node_id}
        elif resp.status_code in (200, 201):
            print(f"  ✓ Created source node: {node_id}")
            return resp.json()
        else:
            print(f"  ✗ Failed to create {node_id}: {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"  ✗ Error creating {node_id}: {e}")
        return None


def create_references_edge(from_node: str, to_source: str, confidence: float = 0.85) -> dict | None:
    """Create an L2 REFERENCES edge from a concept to a source node."""
    source_id = f"source-{to_source}"
    payload = {
        "from_node": from_node,
        "to_node": source_id,
        "edge_type": "REFERENCES",
        "layer": "L2",
        "confidence": confidence,
    }

    if DRY_RUN:
        print(f"  [DRY] Would create REFERENCES: {from_node} -> {source_id}")
        return payload

    try:
        resp = requests.post(f"{OHM_URL}/edge", json=payload, headers=HEADERS, timeout=10)
        if resp.status_code in (200, 201):
            return resp.json()
        elif resp.status_code == 409:
            return {"status": "already_exists"}
        else:
            print(f"  ✗ Failed to create edge: {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"  ✗ Error creating edge: {e}")
        return None


def main():
    print("=" * 70)
    print("OHM Source Citation Backfill")
    print("=" * 70)
    if DRY_RUN:
        print("DRY RUN — no changes will be made\n")

    # Step 1: Fetch all observations
    print("\n📊 Step 1: Fetching observations...")
    resp = requests.get(f"{OHM_URL}/observations?limit=500", headers=HEADERS, timeout=15)
    data = resp.json()
    observations = data if isinstance(data, list) else data.get("observations", [])
    print(f"  Found {len(observations)} observations")

    # Step 2: Categorize sources
    print("\n📋 Step 2: Categorizing sources...")
    external_obs = []
    agent_obs = []
    generic_obs = []
    no_source_obs = []
    compound_sources = defaultdict(list)

    for obs in observations:
        source = obs.get("source")
        node_id = obs.get("node_id")
        source_url = obs.get("source_url")

        if not source:
            no_source_obs.append(obs)
            continue
        if source in AGENT_SOURCES:
            agent_obs.append(obs)
            continue
        if source in GENERIC_SOURCES:
            generic_obs.append(obs)
            continue
        if source.startswith("Oil ") or len(source) > 60:
            # Free-text source descriptions
            generic_obs.append(obs)
            continue

        # External source — parse it
        outlets = parse_compound_source(source)
        if outlets:
            external_obs.append((obs, outlets))
            for outlet in outlets:
                compound_sources[outlet].append(node_id)
        else:
            # Unrecognized — treat as generic
            generic_obs.append(obs)

    print(f"  External sources: {len(external_obs)} observations")
    print(f"  Agent-authored: {len(agent_obs)} observations")
    print(f"  Generic/process: {len(generic_obs)} observations")
    print(f"  No source: {len(no_source_obs)} observations")

    # Step 3: Create source nodes for all referenced outlets
    print("\n🏗️ Step 3: Creating source nodes...")
    outlets_needed = set()
    for obs, outlets in external_obs:
        outlets_needed.update(outlets)

    created_sources = {}
    for outlet_key in sorted(outlets_needed):
        if outlet_key in CANONICAL_SOURCES:
            result = create_source_node(outlet_key, CANONICAL_SOURCES[outlet_key])
            created_sources[outlet_key] = result
        else:
            print(f"  ⚠ No canonical mapping for: {outlet_key}")

    # Step 4: Create REFERENCES edges for external observations that don't have source_url
    print("\n🔗 Step 4: Creating L2 REFERENCES edges...")
    edges_created = 0
    edges_skipped = 0

    # Track which edges we've already created to avoid duplicates
    existing_edges = set()
    edge_resp = requests.get(f"{OHM_URL}/edges?layer=L2&limit=500", headers=HEADERS, timeout=15)
    if edge_resp.status_code == 200:
        edge_data = edge_resp.json()
        edge_list = edge_data if isinstance(edge_data, list) else edge_data.get("edges", [])
        for e in edge_list:
            key = f"{e.get('from_node', e.get('from', ''))}:{e.get('to_node', e.get('to', ''))}"
            existing_edges.add(key)
        print(f"  Found {len(existing_edges)} existing L2 edges")

    for obs, outlets in external_obs:
        node_id = obs.get("node_id")
        for outlet_key in outlets:
            source_id = f"source-{outlet_key}"
            edge_key = f"{node_id}:{source_id}"

            if edge_key in existing_edges:
                edges_skipped += 1
                continue

            result = create_references_edge(node_id, outlet_key)
            if result:
                edges_created += 1
                existing_edges.add(edge_key)

    print(f"  ✓ Edges created: {edges_created}")
    print(f"  ○ Edges skipped (existing): {edges_skipped}")

    # Step 5: Backfill source_url on observations that have no source_url
    print("\n📝 Step 5: Backfilling source_url on observations...")
    backfilled = 0

    for obs, outlets in external_obs:
        if not obs.get("source_url") and outlets:
            # For compound sources, use the first outlet as primary source_url
            primary_outlet = outlets[0]
            if primary_outlet in CANONICAL_SOURCES:
                source_url = CANONICAL_SOURCES[primary_outlet]["url"]
                obs_id = obs.get("id", obs.get("node_id"))

                if DRY_RUN:
                    print(f"  [DRY] Would update obs {obs_id}: source_url={source_url}")
                    backfilled += 1
                    continue

                # Update observation source_url via admin bulk endpoint
                updates_payload = {"updates": [{"observation_id": obs_id, "source_url": source_url}]}
                try:
                    update_resp = requests.post(
                        f"{OHM_URL}/admin/observation-source-urls",
                        json=updates_payload,
                        headers=HEADERS,
                        timeout=10,
                    )
                    if update_resp.status_code in (200, 201):
                        backfilled += 1
                    else:
                        print(f"  ✗ Failed to update obs {obs_id}: {update_resp.status_code}")
                except Exception as e:
                    print(f"  ✗ Error updating obs {obs_id}: {e}")

    print(f"  ✓ Observations backfilled: {backfilled}")

    # Step 6: Summary
    print("\n" + "=" * 70)
    print("BACKFILL SUMMARY")
    print("=" * 70)
    print(f"  Source nodes created/verified: {len([v for v in created_sources.values() if v])}")
    print(f"  REFERENCES edges created: {edges_created}")
    print(f"  REFERENCES edges skipped: {edges_skipped}")
    print(f"  Observations backfilled with source_url: {backfilled}")
    print(f"  Observations with external sources: {len(external_obs)}")
    print(f"  Observations with agent sources: {len(agent_obs)}")
    print(f"  Observations with generic sources: {len(generic_obs)}")
    print(f"  Observations with no source: {len(no_source_obs)}")
    print(f"  Unique outlets parsed: {len(outlets_needed)}")

    if DRY_RUN:
        print("\n⚠ DRY RUN — no changes were made. Run without --dry-run to apply.")

    # Final stats
    print("\n📈 Post-backfill stats:")
    stats_resp = requests.get(f"{OHM_URL}/stats", headers=HEADERS, timeout=10)
    if stats_resp.status_code == 200:
        stats = stats_resp.json()
        l2 = stats.get("edges_by_layer", {}).get("L2", 0)
        l3 = stats.get("edges_by_layer", {}).get("L3", 0)
        sources = stats.get("nodes_by_type", {}).get("source", 0)
        ratio = f"{l3}:{l2}" if l2 > 0 else f"{l3}:0"
        print(f"  Source nodes: {sources}")
        print(f"  L2 edges: {l2}")
        print(f"  L3 edges: {l3}")
        print(f"  L3:L2 ratio: {ratio}")
        target = "✅" if l2 > 0 and l3 / l2 < 20 else "❌"
        print(f"  Target (5:1 or better): {target} (currently {l3/max(l2,1):.1f}:1)")


if __name__ == "__main__":
    main()