#!/usr/bin/env bash
# =============================================================================
# Xaloqi TestLab
# scripts/dogfood_against_eds.sh
#
# PURPOSE: Run xaloqi.tester.conformance's fixture-independent vectors
#          against real, unmodified EDS (examples/basic_ecu_cda_demo), over
#          a real vxcan pair, inside a Docker container -- the same
#          container pattern Xaloqi/EDS's scripts/demo_can_sovd_cda_interop.sh
#          already proved works with no sudo (docker run --cap-add=NET_ADMIN).
#
#          This is the "dogfood against real EDS" half of this corpus's own
#          dual-proof story -- NOT a claim this corpus only works against
#          EDS. See tests/test_conformance.py::test_full_uds_sid_suite_against_
#          independent_reference_stub for the independent-implementation
#          half of that same proof.
#
# USAGE:
#   bash scripts/dogfood_against_eds.sh /path/to/EDS/checkout
#
# EXIT CODES: 0 if every vector run passed; 1 otherwise or on setup failure.
# =============================================================================
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 /path/to/EDS/checkout" >&2
  exit 1
fi
EDS_ROOT="$(cd "$1" && pwd)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This script lives at <package root>/scripts/ -- "<package root>" is core/
# in the private TestLab monorepo and the repo root in the public
# xaloqi-testlab-core export (core/ is stripped on export, this script
# moves with it, and the relative layout above it is identical either way).
PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CACHE_DIR="${XALOQI_DEMO_CACHE_DIR:-$HOME/.cache/xaloqi-eds-demo}"
EDS_WEST_DIR="$CACHE_DIR/eds-west-workspace"
BUILDER_IMAGE="xaloqi-eds-demo-builder:native-sim"
RUN_IMAGE="ubuntu:24.04"

echo "============================================================"
echo "  xaloqi.tester.conformance <-> real EDS, over real CAN"
echo "============================================================"

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found" >&2; exit 1; }
docker version >/dev/null 2>&1 || { echo "ERROR: docker not usable without sudo" >&2; exit 1; }

# --- Build the EDS builder image (shared cache with Xaloqi/EDS's own demo) -
docker build -t "$BUILDER_IMAGE" -f - "$EDS_ROOT" > "$CACHE_DIR/builder-image.log" 2>&1 <<'DOCKERFILE_EOF' \
  || { echo "ERROR: builder image build failed -- see $CACHE_DIR/builder-image.log" >&2; exit 1; }
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y --no-install-recommends -qq \
      build-essential gcc-multilib g++-multilib cmake ninja-build \
      device-tree-compiler python3 python3-pip python3-venv \
      git wget xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages --no-cache-dir west pyyaml jinja2 pyelftools
DOCKERFILE_EOF

# --- Build examples/basic_ecu_cda_demo for native_sim, containerized ------
mkdir -p "$EDS_WEST_DIR"
docker run --rm \
  -e ZEPHYR_TOOLCHAIN_VARIANT=host \
  -v "$EDS_WEST_DIR":/west \
  -v "$EDS_ROOT":/west/EDS:ro \
  "$BUILDER_IMAGE" bash -c '
    set -e
    cd /west
    if [ ! -d .west ]; then
      west init -l EDS
      west update --narrow --fetch-opt=--depth=1
    fi
    west build -b native_sim EDS/examples/basic_ecu_cda_demo -d build_demo --pristine -- \
      -DDIAG_SKIP_CODEGEN=ON \
      -DEXTRA_CONF_FILE=/west/EDS/boards/native_sim/native_sim_realcan.conf \
      -DDTC_OVERLAY_FILE=/west/EDS/boards/native_sim/native_sim_realcan.overlay
  ' > "$CACHE_DIR/eds-build.log" 2>&1 \
  || { echo "ERROR: EDS build failed -- see $CACHE_DIR/eds-build.log" >&2; tail -40 "$CACHE_DIR/eds-build.log" >&2; exit 1; }
EDS_BINARY="$EDS_WEST_DIR/build_demo/zephyr/zephyr.exe"
test -x "$EDS_BINARY" || { echo "ERROR: $EDS_BINARY missing after build" >&2; exit 1; }
echo "EDS built: $EDS_BINARY"

# --- vxcan pair + conformance CLI, inside one container -------------------
CONTAINER_NAME="xaloqi-conformance-dogfood-$$"
cleanup() {
  docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$CONTAINER_NAME" --cap-add=NET_ADMIN --cap-add=NET_RAW \
  "$RUN_IMAGE" sleep infinity > /dev/null

docker exec "$CONTAINER_NAME" bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  dpkg --add-architecture i386
  apt-get update -qq
  apt-get install -y -qq iproute2 libc6:i386 python3 python3-pip > /dev/null
  pip install --break-system-packages --quiet --no-cache-dir python-can
' > "$CACHE_DIR/dogfood-container-setup.log" 2>&1 \
  || { echo "ERROR: container setup failed -- see $CACHE_DIR/dogfood-container-setup.log" >&2; exit 1; }

docker cp "$EDS_BINARY" "$CONTAINER_NAME:/eds-demo-eds.exe"
docker exec "$CONTAINER_NAME" chmod +x /eds-demo-eds.exe
docker cp "$PACKAGE_ROOT/xaloqi" "$CONTAINER_NAME:/usr/lib/python3/dist-packages/xaloqi"
docker cp "$PACKAGE_ROOT/examples/conformance_bindings" "$CONTAINER_NAME:/bindings"

docker exec "$CONTAINER_NAME" bash -c '
  set -e
  ip link add dev vxcan0 type vxcan peer name vxcan1
  ip link set vxcan0 up
  ip link set vxcan1 up
  ip link property add dev vxcan1 altname zcan0
'

docker exec -d "$CONTAINER_NAME" bash -c "/eds-demo-eds.exe > /tmp/eds.log 2>&1"
sleep 1

echo
echo "--- isotp suite (fixture-independent vectors only) ---"
ISOTP_RC=0
docker exec "$CONTAINER_NAME" python3 -m xaloqi.tester.conformance isotp \
  --interface vxcan0 --can-id 0x7E0 --bindings-dir /bindings \
  || ISOTP_RC=$?

echo
echo "--- uds-sid suite (fixture-independent vectors only) ---"
UDS_RC=0
docker exec "$CONTAINER_NAME" python3 -m xaloqi.tester.conformance uds-sid \
  --interface vxcan0 --rx-id 0x7E8 --tx-id 0x7E0 --bindings-dir /bindings \
  || UDS_RC=$?

echo
if [ "$ISOTP_RC" -eq 0 ] && [ "$UDS_RC" -eq 0 ]; then
  echo "DOGFOOD AGAINST REAL EDS: all fixture-independent vectors passed."
  exit 0
else
  echo "DOGFOOD AGAINST REAL EDS: failures above (isotp rc=$ISOTP_RC, uds-sid rc=$UDS_RC)." >&2
  echo "--- EDS log ---" >&2
  docker exec "$CONTAINER_NAME" cat /tmp/eds.log >&2 || true
  exit 1
fi
