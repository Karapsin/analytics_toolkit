#!/usr/bin/env bash
set -Eeuo pipefail

install -m 600 /certs/server.key "${MASTER_DATA_DIRECTORY}/server.key"
install -m 644 /certs/server.crt "${MASTER_DATA_DIRECTORY}/server.crt"
install -m 644 /certs/ca.crt "${MASTER_DATA_DIRECTORY}/ca.crt"

cat >>"${MASTER_DATA_DIRECTORY}/postgresql.conf" <<EOF
ssl = on
ssl_cert_file = '${MASTER_DATA_DIRECTORY}/server.crt'
ssl_key_file = '${MASTER_DATA_DIRECTORY}/server.key'
ssl_ca_file = '${MASTER_DATA_DIRECTORY}/ca.crt'
EOF

pg_hba_tmp=$(mktemp)
{
    printf '%s\n' 'hostssl all all 0.0.0.0/0 md5 clientcert=1'
    printf '%s\n' 'hostssl all all ::0/0 md5 clientcert=1'
    cat "${MASTER_DATA_DIRECTORY}/pg_hba.conf"
} >"${pg_hba_tmp}"
mv "${pg_hba_tmp}" "${MASTER_DATA_DIRECTORY}/pg_hba.conf"

gpstop -ar
sleep 10
PGPASSWORD="${GREENPLUM_PASSWORD}" psql \
    -h 127.0.0.1 \
    -U "${GREENPLUM_USER}" \
    -d "${GREENPLUM_DATABASE_NAME}" \
    -Atqc 'SELECT 1' >/dev/null
touch /data/.auth-tls-ready
