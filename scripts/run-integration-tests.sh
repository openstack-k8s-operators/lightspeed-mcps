#!/bin/bash

FAKE_DIR="$(mktemp -d)"
uv run --locked python -m tests.integration.fake_cluster \
  --host 0.0.0.0 --port 0 --out-dir "$FAKE_DIR" \
  --advertise-host host.containers.internal --delay 0.7 \
  > "$FAKE_DIR/fake-cluster.log" 2>&1 &
FAKE_PID=$!
cleanup() {
  if [ -f "$FAKE_DIR/fake-cluster.pid" ]; then
    kill "$(cat "$FAKE_DIR/fake-cluster.pid")" 2>/dev/null || true
  fi
  kill "$FAKE_PID" 2>/dev/null || true
}
trap cleanup EXIT
# The first uv run creates the venv, so allow up to 5 minutes
for _ in $(seq 1 300); do
  if [ -f "$FAKE_DIR/clouds.yaml" ]; then break; fi
  if ! kill -0 "$FAKE_PID" 2>/dev/null; then
    cat "$FAKE_DIR/fake-cluster.log"
    echo "::error::Fake cluster died on startup"
    exit 1
  fi
  sleep 1
done
if [ ! -f "$FAKE_DIR/clouds.yaml" ]; then
  cat "$FAKE_DIR/fake-cluster.log"
  echo "::error::Fake cluster did not write clouds.yaml within 300s"
  exit 1
fi
export RHOS_MCPS_TEST_BACKEND=fake
export RHOS_MCPS_TEST_CREDS_DIR="$FAKE_DIR"
if ! uv run --locked python -m unittest discover -s tests/integration -t . -v; then
  cat "$FAKE_DIR/fake-cluster.log"
  echo "::error::Integration tests against the fake cluster failed"
  exit 1
fi
