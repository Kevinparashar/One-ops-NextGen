#!/usr/bin/env bash
# Generate Python bindings from the .proto contracts (ADR-0001).
#
# Run from the repo root:  bash dev/gen_proto.sh
# CI runs this and fails the build if `git diff` shows the generated code is
# stale — generated bindings are checked in so a consumer never needs protoc.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROTO_DIR="proto"
OUT_DIR="src/oneops/codec/generated"
PYTHON="${PYTHON:-.venv/bin/python}"

mkdir -p "$OUT_DIR"

# Compile every .proto under proto/ . `--python_out` only (no gRPC services —
# transport is NATS, not gRPC; protobuf is used purely as the codec).
"$PYTHON" -m grpc_tools.protoc \
  --proto_path="$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  $(find "$PROTO_DIR" -name '*.proto')

# Make the generated package importable.
find "$OUT_DIR" -type d -exec touch {}/__init__.py \;

echo "generated → $OUT_DIR"
find "$OUT_DIR" -name '*_pb2.py' | sort
