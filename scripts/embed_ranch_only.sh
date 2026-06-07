#!/bin/bash
# Generate embeddings using ranch Ollama ONLY (0.09s per embedding vs 10s on local CPU)
# Runs batch_size=5 to avoid DuckDB concurrency issues
# Designed to be run repeatedly until complete

AUTH="Authorization: Bearer ohm-metis-u0-KEjbnU_WfJnmNq7rbzQ"
URL="http://127.0.0.1:8710/admin/embeddings?batch_size=5&delay_ms=100&ollama_url=http://100.104.126.80:11434"

RESPONSE=$(curl -s -m 120 "$URL" -H "$AUTH" 2>&1)

if [ -z "$RESPONSE" ]; then
    echo "$(date): Empty response (daemon may be busy)"
    exit 1
fi

UPDATED=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('updated','0'))" 2>/dev/null || echo "?")
REMAINING=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('remaining','?'))" 2>/dev/null || echo "?")
FAILED=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('failed','0'))" 2>/dev/null || echo "?")

echo "$(date): Updated=$UPDATED, Failed=$FAILED, Remaining=$REMAINING"

# If remaining is 0, we're done
if [ "$REMAINING" = "0" ]; then
    echo "All embeddings generated! Removing cron job."
    crontab -l 2>/dev/null | grep -v embed_ | crontab -
fi