#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
POLICY=$ROOT/builder/asahi-cache-hit-policy.sh

source "$POLICY"

asahi_validate_cache_hit_requirement ""
asahi_validate_cache_hit_requirement configured-target
if asahi_validate_cache_hit_requirement sealed-release-package; then
  echo "unsupported cache-hit boundary was accepted" >&2
  exit 1
fi

asahi_cache_hit_required configured-target base-images
asahi_cache_hit_required configured-target configured-target
if asahi_cache_hit_required configured-target finalized-boot; then
  echo "configured-target boundary leaked into finalized boot" >&2
  exit 1
fi
if asahi_cache_hit_required "" base-images; then
  echo "empty cache-hit boundary required an upstream hit" >&2
  exit 1
fi

echo "ok - configured-target cache-hit policy stops before rebuild/install"
