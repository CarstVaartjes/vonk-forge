#!/bin/sh
set -eu

source=${LITELLM_DATABASE_PASSWORD_FILE:-/run/secrets/litellm-database-password}
runtime_directory=/run/vonk-postgres-secrets
target=$runtime_directory/litellm-database-password
init_source=/run/vonk-source-assets/postgres/init-databases.sh
init_target=/docker-entrypoint-initdb.d/10-vonk-forge-databases.sh

# Standalone Compose implements secrets as read-only bind mounts. Their host
# UID is not portable to the image's postgres UID, so stage the one secret
# needed by initdb before the official entrypoint drops privileges.
install -d -m 0700 -o postgres -g postgres "$runtime_directory"
install -m 0400 -o postgres -g postgres "$source" "$target"
export LITELLM_DATABASE_PASSWORD_FILE=$target

# Compose configs are host-backed on NAS platforms. Treat the signed config as
# input and give PostgreSQL a container-local, non-executable copy so host ACLs
# and mount execution policy cannot decide whether the official entrypoint can
# initialize the databases.
if [ -L "$init_source" ] || [ ! -f "$init_source" ]; then
  printf '%s\n' 'PostgreSQL database initializer source must be a regular file' >&2
  exit 1
fi
init_size=$(stat -c '%s' "$init_source") || {
  printf '%s\n' 'PostgreSQL database initializer source cannot be inspected' >&2
  exit 1
}
case "$init_size" in
  ''|*[!0-9]*)
    printf '%s\n' 'PostgreSQL database initializer source size is invalid' >&2
    exit 1
    ;;
esac
if [ "$init_size" -eq 0 ] || [ "$init_size" -gt 65536 ]; then
  printf '%s\n' 'PostgreSQL database initializer source size is invalid' >&2
  exit 1
fi
install -m 0444 -o root -g root "$init_source" "$init_target"

sentinel=${PGDATA:-/var/lib/postgresql/data}/.vonk-database-initialized
if [ -f "${PGDATA:-/var/lib/postgresql/data}/PG_VERSION" ]; then
  existing_cluster=1
else
  existing_cluster=0
  export VONK_POSTGRES_INIT_SENTINEL=$sentinel
fi

/usr/local/bin/docker-entrypoint.sh "$@" &
postgres_pid=$!

forward_signal() {
  if [ -n "${backup_pid:-}" ]; then kill -TERM "$backup_pid" 2>/dev/null || true; fi
  kill -TERM "$postgres_pid" 2>/dev/null || true
}
trap forward_signal TERM INT

ready=0
attempt=0
while [ "$attempt" -lt 240 ]; do
  if ! kill -0 "$postgres_pid" 2>/dev/null; then
    wait "$postgres_pid"
    exit $?
  fi
  if [ "$existing_cluster" -eq 1 ] || [ -f "$sentinel" ]; then
    if pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" >/dev/null 2>&1; then
      ready=1
      break
    fi
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  printf '%s\n' 'PostgreSQL did not become ready for database reconciliation' >&2
  kill -TERM "$postgres_pid" 2>/dev/null || true
  wait "$postgres_pid" || true
  exit 1
fi

if ! gosu postgres /bin/sh "$init_target"; then
  printf '%s\n' 'PostgreSQL database reconciliation failed' >&2
  kill -TERM "$postgres_pid" 2>/dev/null || true
  wait "$postgres_pid" || true
  exit 1
fi

# Dump all databases and roles, including the configured Controller database and
# LiteLLM. Publish only complete dumps; failed attempts never prune good backups.
backup_loop() (
  umask 077
  interval=${VONK_BACKUP_INTERVAL_SECONDS:-86400}
  keep=${VONK_BACKUP_KEEP:-7}
  case "$interval:$keep" in *[!0-9:]*|:*|*:) echo "Invalid backup settings" >&2; exit 1;; esac
  if [ "$interval" -eq 0 ] || [ "$keep" -eq 0 ]; then
    echo "Backup interval and retention must be positive" >&2; exit 1
  fi
  if [ ! -d /backups ]; then
    echo "PostgreSQL backup directory /backups is missing; prepare the NAS bundle before starting Compose" >&2
    exit 1
  fi
  backup_owner=$(stat -c '%u:%g' /backups 2>/dev/null || true)
  case "$backup_owner" in
    ''|*[!0-9:]*|*:*:*)
      echo "PostgreSQL backup directory owner cannot be determined" >&2
      exit 1
      ;;
  esac
  temporary=/backups/.postgres-backup.tmp
  state_temporary=/state/.last-successful-backup.epoch.tmp
  verification_temporary=/state/.last-backup-restore-verification.epoch.tmp
  cleanup() {
    rm -f "$temporary" "$temporary.gz" "$state_temporary" "$verification_temporary"
  }
  trap 'cleanup; exit 0' TERM INT
  restore_verify() (
    set -e
    restore_directory=$(mktemp -d /tmp/vonk-backup-restore.XXXXXX)
    trap 'gosu postgres pg_ctl -D "$restore_directory/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$restore_directory"' EXIT
    chown postgres:postgres "$restore_directory"
    install -d -m 0700 -o postgres -g postgres "$restore_directory/socket"
    gosu postgres initdb -D "$restore_directory/data" \
      --username=vonk_restore_admin --auth=trust >/dev/null
    gosu postgres pg_ctl -D "$restore_directory/data" \
      -o "-c listen_addresses='' -c unix_socket_directories=$restore_directory/socket -c port=55439" \
      -w start >/dev/null
    gzip -dc "$destination" | gosu postgres psql \
      -h "$restore_directory/socket" -p 55439 -U vonk_restore_admin \
      -d postgres -X -v ON_ERROR_STOP=1 >/dev/null
    gosu postgres psql -h "$restore_directory/socket" -p 55439 \
      -U vonk_restore_admin -d postgres -X -v ON_ERROR_STOP=1 \
      -tAc "SELECT count(*) FROM pg_database WHERE datname IN ('control', 'litellm')" \
      | grep -qx 2
    gosu postgres pg_ctl -D "$restore_directory/data" -m fast -w stop >/dev/null
    rm -rf "$restore_directory"
  )
  while :; do
    if gosu postgres pg_dumpall --username "$POSTGRES_USER" --database postgres > "$temporary" &&
       gzip -c "$temporary" > "$temporary.gz" && gzip -t "$temporary.gz"; then
      destination=/backups/postgres-$(date -u +%Y%m%dT%H%M%SZ).sql.gz
      mv "$temporary.gz" "$destination"
      # The entrypoint runs as root so the postgres process can write through a
      # private host bind mount. Return completed dumps to the bundle owner's
      # UID/GID so the invoking user can inspect and prune them safely.
      chown "$backup_owner" "$destination"
      chmod 0600 "$destination"
      rm -f "$temporary"
      if [ -d /step-ca-data ]; then
        ca_destination=/backups/step-ca-$(basename "$destination" .sql.gz).tar.gz
        ca_temporary=/backups/.step-ca-backup.tmp.gz
        if tar -czf "$ca_temporary" -C /step-ca-data . && gzip -t "$ca_temporary"; then
          mv "$ca_temporary" "$ca_destination"
          chown "$backup_owner" "$ca_destination"
          chmod 0600 "$ca_destination"
        else
          rm -f "$ca_temporary"
          echo "step-ca data backup failed; keeping previous success marker" >&2
          sleep 300 & wait $!
          continue
        fi
      fi
      if [ -n "${VONK_BACKUP_OFFHOST_PATH:-}" ]; then
        offhost_temporary=/offhost/.postgres-backup.tmp.gz
        ca_offhost_temporary=/offhost/.step-ca-backup.tmp.gz
        if [ ! -d /offhost ] ||
           ! cp "$destination" "$offhost_temporary" ||
           ! gzip -t "$offhost_temporary" ||
           ! mv "$offhost_temporary" "/offhost/$(basename "$destination")" ||
           { [ -n "${ca_destination:-}" ] &&
             { ! cp "$ca_destination" "$ca_offhost_temporary" ||
               ! tar -tzf "$ca_offhost_temporary" >/dev/null ||
               ! mv "$ca_offhost_temporary" "/offhost/$(basename "$ca_destination")"; }; }; then
          rm -f "$offhost_temporary" "$ca_offhost_temporary"
          rm -f "/offhost/$(basename "$destination")"
          if [ -n "${ca_destination:-}" ]; then
            rm -f "/offhost/$(basename "$ca_destination")"
          fi
          echo "Off-host backup copy failed; keeping previous success marker" >&2
          sleep 300 & wait $!
          continue
        fi
        offhost_count=0
        for backup in $(ls -1 /offhost/postgres-*.sql.gz 2>/dev/null | sort -r); do
          offhost_count=$((offhost_count + 1))
          if [ "$offhost_count" -gt "$keep" ]; then rm -f "$backup"; fi
        done
        offhost_count=0
        for backup in $(ls -1 /offhost/step-ca-postgres-*.tar.gz 2>/dev/null | sort -r); do
          offhost_count=$((offhost_count + 1))
          if [ "$offhost_count" -gt "$keep" ]; then rm -f "$backup"; fi
        done
      fi
      if restore_verify; then
        now=$(date +%s)
        printf '%s\n' "$now" > "$verification_temporary"
        chmod 0644 "$verification_temporary"
        mv "$verification_temporary" /state/last-backup-restore-verification.epoch
      else
        echo "PostgreSQL restore verification failed; keeping previous success marker" >&2
        sleep 300 & wait $!
        continue
      fi
      now=$(date +%s)
      printf '%s\n' "$now" > "$state_temporary"
      chmod 0644 "$state_temporary"
      mv "$state_temporary" /state/last-successful-backup.epoch
      echo "PostgreSQL and step-ca backup completed and restore-verified: $destination"
      # Filenames are generated above and contain neither spaces nor newlines.
      count=0
      for backup in $(ls -1 /backups/postgres-*.sql.gz | sort -r); do
        count=$((count + 1))
        if [ "$count" -gt "$keep" ]; then rm -f "$backup"; fi
      done
      count=0
      for backup in $(ls -1 /backups/step-ca-postgres-*.tar.gz | sort -r); do
        count=$((count + 1))
        if [ "$count" -gt "$keep" ]; then rm -f "$backup"; fi
      done
      sleep "$interval" & wait $!
    else
      echo "PostgreSQL backup failed; retrying in five minutes" >&2
      rm -f "$temporary" "$temporary.gz"
      sleep 300 & wait $!
    fi
  done
)
backup_loop &
backup_pid=$!
wait "$postgres_pid"
