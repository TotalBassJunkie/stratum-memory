#!/usr/bin/env bash
# Stratum-Memory: canonical schema (WP-02), parameterized by STRATUM_VECTOR_TYPE.
#
# Runs from /docker-entrypoint-initdb.d alphabetically after 002_app_role.sql and
# before 004_rls.sql. It renders the SQL template (003_schema.sql.template) and
# pipes it through psql, substituting the embedding-type placeholders:
#   __VEC__      -> halfvec | vector
#   __VEC_OPS__  -> halfvec_cosine_ops | vector_cosine_ops
#
# STRATUM_VECTOR_TYPE defaults to "halfvec" (Pi profile). The server profile sets
# STRATUM_VECTOR_TYPE=vector so a fresh server DB gets vector(1024) columns, which
# match the ::vector casts used by the core/worker code paths.
set -euo pipefail

vec_type="${STRATUM_VECTOR_TYPE:-halfvec}"

case "$vec_type" in
    halfvec)
        vec="halfvec"
        vec_ops="halfvec_cosine_ops"
        ;;
    vector)
        vec="vector"
        vec_ops="vector_cosine_ops"
        ;;
    *)
        echo "stratum-init: unknown STRATUM_VECTOR_TYPE='${vec_type}' (expected 'halfvec' or 'vector')" >&2
        exit 1
        ;;
esac

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="${here}/003_schema.sql.template"

if [ ! -f "$template" ]; then
    echo "stratum-init: missing schema template: ${template}" >&2
    exit 1
fi

echo "stratum-init: applying 003_schema with STRATUM_VECTOR_TYPE='${vec_type}'"

sed \
    -e "s/__VEC_OPS__/${vec_ops}/g" \
    -e "s/__VEC__/${vec}/g" \
    "$template" \
    | psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
