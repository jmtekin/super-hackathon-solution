#!/bin/bash
# Usage: shoot_v4.sh <base url ending in />. Renders on crib-pc (no Chrome on the VPS), copies PNGs to design/shots-v4.
set -e
K=/opt/agent/.ssh/id_ed25519_cribpc; H=jonas@100.119.168.37; D=/opt/agent/projects/restorer/design
ssh -i $K $H "cmd /c if not exist C:\ai\restorer-refs\v4 mkdir C:\ai\restorer-refs\v4"
scp -q -i $K $D/tools/shoot_v4.js $H:C:/ai/restorer-refs/shoot_v4.js
ssh -i $K $H "cd C:\ai\restorer-refs && set LIVE=$LIVE&& set NODE_PATH=C:\Users\jonas\node_modules&& node shoot_v4.js $1 C:/ai/restorer-refs/v4"
scp -q -i $K "$H:C:/ai/restorer-refs/v4/*.png" $D/shots-v4/
