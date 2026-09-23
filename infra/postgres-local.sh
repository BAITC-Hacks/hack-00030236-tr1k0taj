#!/usr/bin/env bash
set -euo pipefail
umask 077

pg_bin="${PG_BIN:-${HOME}/.local/share/voice-router/Postgres.app/Contents/Versions/17/bin}"
pg_data="${PG_LOCAL_DATA:-/tmp/voice-router-pg17}"
pg_port="${PG_LOCAL_PORT:-55432}"
export PGPASSWORD="${PG_LOCAL_PASSWORD:-voice_router}"

if [[ ! -x "${pg_bin}/pg_ctl" ]]; then
    echo "PostgreSQL 17 not found. Set PG_BIN to its bin directory (pg_trgm and vector required)." >&2
    exit 1
fi

case "${1:-start}" in
    start)
        if [[ ! -f "${pg_data}/PG_VERSION" ]]; then
            mkdir -p "${pg_data}"
            "${pg_bin}/initdb" -D "${pg_data}" --username=voice_router \
                --auth-local=trust --auth-host=scram-sha-256 \
                --pwfile=<(printf '%s\n' "${PGPASSWORD}") --encoding=UTF8 --locale=en_US.UTF-8
        fi
        if ! "${pg_bin}/pg_ctl" -D "${pg_data}" status >/dev/null 2>&1; then
            "${pg_bin}/pg_ctl" -D "${pg_data}" -l "${pg_data}/server.log" \
                -o "-h 127.0.0.1 -p ${pg_port} -k ${pg_data}" -w start
        fi
        if [[ "$("${pg_bin}/psql" -h 127.0.0.1 -p "${pg_port}" -U voice_router -d postgres \
            -Atc "SELECT 1 FROM pg_database WHERE datname = 'voice_router'")" != 1 ]]; then
            "${pg_bin}/createdb" -h 127.0.0.1 -p "${pg_port}" -U voice_router voice_router
        fi
        "${pg_bin}/psql" -h 127.0.0.1 -p "${pg_port}" -U voice_router -d voice_router \
            -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector;'
        ;;
    stop)
        "${pg_bin}/pg_ctl" -D "${pg_data}" -m fast -w stop
        ;;
    psql)
        shift
        exec "${pg_bin}/psql" -h 127.0.0.1 -p "${pg_port}" -U voice_router -d voice_router "$@"
        ;;
    *)
        echo "Usage: $0 {start|stop|psql [arguments...]}" >&2
        exit 2
        ;;
esac
