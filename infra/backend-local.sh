#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${HOME}/.local/bin:${PATH}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://voice_router:voice_router@127.0.0.1:55432/voice_router}"
export DATASETS_DIR="${DATASETS_DIR:-${project_root}/datasets}"

cd "${project_root}/backend"
exec uv run --frozen "$@"
