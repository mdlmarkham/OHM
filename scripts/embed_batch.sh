#!/bin/bash
# Generate embeddings in batches of 3 nodes
# Run via cron every 2 minutes until all nodes have embeddings

AUTH="Authorization: Bearer ohm-metis-u0-KEjbnU_WfJnmNq7rbzQ"
URL="http://127.0.0.1:8710/admin/embeddings?batch_size=10&delay_ms=50"

RESULT=$(curl -s -m 120 "$URL" -H "$AUTH")
UPDATED=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('updated','0'))" 2>/dev/null)
REMAINING=$(echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('remaining','0'))" 2>/dev/null)

echo "$(date): Updated=$UPDATED, Remaining=$REMAINING"

# If all nodes have embeddings, disable cron
if [ "$REMAINING" = "0" ]; then
    echo "All embeddings generated. Removing cron job."
    crontab -l 2>/dev/null | grep -v embed_batch | crontab -
fi