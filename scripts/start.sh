#!/usr/bin/env bash
# Start the vc-canvas server. Defaults to port 8770 on 127.0.0.1.
set -euo pipefail
HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$HERE"
exec python3 vc_gallery_serve.py "$@"
