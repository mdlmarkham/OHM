#!/bin/bash
# Generate embeddings using BOTH local and ranch Ollama in parallel
# This doubles throughput: ~20 nodes per 2-minute cycle instead of 10
# Run via cron every 2 minutes until all nodes have embeddings

AUTH="Authorization: Bearer ohm-metis-u0-KEjbnU_WfJnmNq7rbzQ"
LOCAL_URL="http://127.0.0.1:8710/admin/embeddings?batch_size=10&delay_ms=50"
RANCH_URL="http://127.0.0.1:8710/admin/embeddings?batch_size=10&delay_ms=50&ollama_url=http://100.104.126.80:11434"

# Run both in parallel
LOCAL_OUT=$(mktemp)
RANCH_OUT=$(mktemp)

curl -s -m 120 "$LOCAL_URL" -H "$AUTH" > "$LOCAL_OUT" &
LOCAL_PID=$!

curl -s -m 120 "$RANCH_URL" -H "$AUTH" > "$RANCH_OUT" &
RANCH_PID=$!

# Wait for both
wait $LOCAL_PID 2>/dev/null
wait $RANCH_PID 2>/dev/null

# Parse results
LOCAL_UPDATED=$(python3 -c "import json,sys; d=json.load(open('$LOCAL_OUT')); print(d.get('updated','0'))" 2>/dev/null || echo "0")
LOCAL_REMAINING=$(python3 -c "import json,sys; d=json.load(open('$LOCAL_OUT')); print(d.get('remaining','0'))" 2>/dev/null || echo "?")
RANCH_UPDATED=$(python3 -c "import json,sys; d=json.load(open('$RANCH_OUT')); print(d.get('updated','0'))" 2>/dev/null || echo "0")
RANCH_REMAINING=$(python3 -c "import json,sys; d=json.load(open('$RANCH_OUT')); print(d.get('remaining','0'))" 2>/dev/null || echo "?")

echo "$(date): Local=$LOCAL_UPDATED, Ranch=$RANCH_UPDATED, Remaining≈$LOCAL_REMAINING"

# Cleanup
rm -f "$LOCAL_OUT" "$RANCH_OUT"

# If all nodes have embeddings, disable cron
if [ "$LOCAL_REMAINING" = "0" ] && [ "$RANCH_REMAINING" = "0" ]; then
    echo "All embeddings generated. Removing cron job."
    crontab -l 2>/dev/null | grep -v embed_batch | crontab -
fi