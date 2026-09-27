#!/bin/bash
# Called by qa-gate after every configured command passes in the current checkout.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python "$SCRIPT_DIR/qa-gate-state.py" record
