import json
import glob
import os

for f in sorted(glob.glob("/var/lib/ohm/ingestion/source_created/*.json"), key=os.path.getmtime, reverse=True)[:6]:
    with open(f) as fh:
        d = json.load(fh)
    print(f"--- {d.get('id')}")
    print(f"title: {d.get('title')}")
    print(f"url: {d.get('url')}")
    print(f"keywords: {d.get('keywords')}")
    print(f"summary: {d.get('summary', '')[:350]}...")
    print()
