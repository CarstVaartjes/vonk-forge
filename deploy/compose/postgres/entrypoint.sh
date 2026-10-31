#!/bin/sh
set -eu

source=${LITELLM_DATABASE_PASSWORD_FILE:-/run/secrets/litellm-database-password}
runtime_directory=/run/vonk-postgres-secrets
target=$runtime_directory/litellm-database-password
init_source=/run/vonk-runtime-assets/postgres/init-databases.sh
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
  export VONK_POSTGRES_INIT_SENTINEL="$sentinel"
fi

/usr/local/bin/docker-entrypoint.sh "$@" &
postgres_pid=$!

forward_signal() {
  trap '' TERM INT
  if [ -n "${backup_pid:-}" ]; then kill -TERM "$backup_pid" 2>/dev/null || true; fi
  # Match the official image's fast, clean shutdown, including active clients.
  kill -INT "$postgres_pid" 2>/dev/null || true
  wait "$postgres_pid" || true
  if [ -n "${backup_pid:-}" ]; then wait "$backup_pid" || true; fi
  printf '%s\n' 'PostgreSQL shutdown complete' >&2
  exit 0
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
  if [ "$((attempt % 15))" -eq 0 ]; then
    printf 'PostgreSQL waiting for database readiness (%s/240 seconds)\n' "$attempt" >&2
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
backup_loop() {
  # This loop supervises attempts and must outlive any failed command, so it
  # runs without errexit. Each attempt runs under errexit in backup_once.
  set +e
  umask 077
  # One backup at startup, then daily; keep the newest week of dumps.
  interval=86400
  keep=7
  temporary=/backups/.postgres-backup.tmp
  state_temporary=/state/.last-successful-backup.epoch.tmp
  verification_temporary=/state/.last-backup-restore-verification.epoch.tmp
  offhost_temporary=/offhost/.postgres-backup.tmp.gz
  margin=268435456
  # The old uncompressed "$temporary" is removed too: a container recreated
  # during an older version's dump leaves exactly that file behind.
  cleanup() {
    rm -f "$temporary" "$temporary.gz" "$temporary.status" "$state_temporary" "$verification_temporary"
    # An unreachable off-host mount must not hang the local cleanup.
    timeout 10 rm -f "$offhost_temporary"
  }
  # A shell runs a trap only after the foreground command it waits on ends. So
  # an attempt runs in the background and is awaited with the interruptible
  # `wait`; the trap then stops the attempt and everything below it (cp,
  # pg_dumpall, restore; `timeout` moves itself to its own process group, so
  # parentage is followed, not the group) instead of waiting for them.
  attempt_pid=
  descendants() {
    for stat in /proc/[0-9]*/stat; do
      line=
      { read -r line <"$stat"; } 2>/dev/null || continue
      rest=${line##*) }
      set -- $rest
      pid=${stat#/proc/}
      pid=${pid%/stat}
      [ "${2:-}" = "$parent" ] && echo "$pid"
    done
  }
  stop_attempt() {
    [ -n "$attempt_pid" ] || return 0
    doomed=$attempt_pid
    frontier=$attempt_pid
    while [ -n "$frontier" ]; do
      next=
      for parent in $frontier; do next="$next $(descendants)"; done
      doomed="$doomed $next"
      frontier=$(echo $next)
    done
    kill -TERM $doomed 2>/dev/null || true
    wait "$attempt_pid" 2>/dev/null || true
    attempt_pid=
  }
  trap 'stop_attempt; cleanup; exit 0' TERM INT
  # A SIGKILLed container never ran the trap; drop its leftovers before any attempt.
  cleanup
  rm -rf /tmp/vonk-backup-restore.*
  # Free bytes of the filesystem of $1 and its device name, or "unknown" when
  # df cannot answer within ten seconds (an unreachable mount must not hang us).
  free_bytes() {
    timeout 10 df -Pk "$1" 2>/dev/null | { read -r _ || :; read -r _ _ _ available _ || :; case "${available:-}" in ''|*[!0-9]*) echo unknown ;; *) echo $((available * 1024)) ;; esac; }
  }
  filesystem_of() {
    timeout 10 df -Pk "$1" 2>/dev/null | { read -r _ || :; read -r name _ || :; echo "${name:-}"; }
  }
  # Succeeds when the filesystem of $1 has $2 bytes free. Short space is
  # reported. Unknown space is reported; $3 decides: "short" for volumes shared
  # with the database (never risk ENOSPC there), "continue" for the off-host
  # mount, whose own copy step fails and heals when it comes back.
  has_space() {
    free=$(free_bytes "$1")
    if [ "$free" = unknown ]; then
      if [ "$3" = short ]; then
        echo "PostgreSQL backup cannot read the free space of $1; treating it as short" >&2
        return 1
      fi
      echo "PostgreSQL backup cannot read the free space of $1; continuing" >&2
    elif [ "$free" -lt "$2" ]; then
      echo "PostgreSQL backup needs $2 bytes free on $1 but only $free are free" >&2
      return 1
    fi
  }
  # Keep the newest $keep dumps of directory $1 plus the newest dump that a
  # passed restore verification covers. Runs at every loop pass, so attempts
  # that publish but fail later cannot pile dumps up and make the space check
  # refuse every attempt for good.
  prune_dumps() {
    protected=
    verified_epoch=$(cat /state/last-backup-restore-verification.epoch 2>/dev/null || true)
    case "$verified_epoch" in
      ''|*[!0-9]*) ;;
      *)
        for backup in $(ls -1 /backups/postgres-*.sql.gz 2>/dev/null | sort -r); do
          if [ "$(stat -c %Y "$backup")" -le "$verified_epoch" ]; then
            protected=$(basename "$backup")
            break
          fi
        done
        ;;
    esac
    count=0
    # Filenames are generated above and contain neither spaces nor newlines.
    for backup in $(timeout 10 ls -1 "$1"/postgres-*.sql.gz 2>/dev/null | sort -r); do
      count=$((count + 1))
      if [ "$count" -gt "$keep" ] && [ "$(basename "$backup")" != "$protected" ]; then
        timeout 10 rm -f "$backup"
      fi
    done
  }
  # Copy $1 to $2, killing the copy only when $2 has not grown for a minute.
  copy_offhost() {
    cp "$1" "$2" &
    copy_pid=$!
    last_size=-1
    stalled=0
    while kill -0 "$copy_pid" 2>/dev/null; do
      size=$(timeout 10 stat -c %s "$2" 2>/dev/null) || size=unknown
      if [ "$size" != unknown ] && [ "$size" != "$last_size" ]; then
        last_size=$size
        stalled=0
      else
        stalled=$((stalled + 5))
      fi
      if [ "$stalled" -ge 60 ]; then
        kill -KILL "$copy_pid" 2>/dev/null
        echo "PostgreSQL off-host copy made no progress for $stalled seconds" >&2
        return 1
      fi
      sleep 5 & wait $!
    done
    wait "$copy_pid"
  }
  restore_verify() (
    set -e
    trap 'exit 143' TERM INT
    restore_directory=$(mktemp -d /tmp/vonk-backup-restore.XXXXXX)
    trap 'gosu postgres pg_ctl -D "$restore_directory/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf -- "${restore_directory:?}"' EXIT
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
    rm -rf -- "${restore_directory:?}"
  )
  # One attempt. Any failed step ends it. The backup marker means a dump was
  # published and gzip-tested, and copied off-host when configured; the
  # restore-verification marker is written only when the isolated restore also
  # succeeded (it is skipped, not failed, while scratch space is short).
  # Call it as a plain command: shells ignore errexit inside a condition.
  backup_once() (
    set -e
    trap 'exit 143' TERM INT
    report_failure() {
      if [ "$1" -ne 0 ]; then
        echo "PostgreSQL backup failed at step $step (status $1); keeping previous success marker; retrying in five minutes" >&2
        cleanup
      fi
    }
    trap 'report_failure "$?"' EXIT
    step=space-check
    database_bytes=$(gosu postgres psql --username "$POSTGRES_USER" --dbname postgres -XtAc \
      'SELECT coalesce(sum(pg_database_size(datname)), 0)::bigint FROM pg_database')
    # A new dump is about as large as the newest published one; before the
    # first dump, the database size is the upper bound. The off-host copy may
    # share the backup volume, in which case both copies must fit.
    newest=$(ls -1 /backups/postgres-*.sql.gz 2>/dev/null | sort -r | head -n 1)
    dump_bytes=$database_bytes
    [ -z "$newest" ] || dump_bytes=$(stat -c %s "$newest")
    needed=$((dump_bytes + margin))
    offhost_needed=0
    if [ -n "${VONK_BACKUP_OFFHOST_PATH:-}" ]; then
      offhost_needed=$needed
      backups_device=$(filesystem_of /backups)
      offhost_device=$(filesystem_of /offhost)
      if [ -n "$backups_device" ] && [ "$backups_device" = "$offhost_device" ]; then
        needed=$((needed + offhost_needed))
      fi
    fi
    has_space /backups "$needed" short
    [ "$offhost_needed" -eq 0 ] || has_space /offhost "$offhost_needed" continue
    step=dump
    # dash has no pipefail: record the dump status so a failed dump cannot hide
    # behind a gzip that happily compressed its partial output.
    { gosu postgres pg_dumpall --username "$POSTGRES_USER" --database postgres \
      && echo 0 > "$temporary.status" || echo 1 > "$temporary.status"; } | gzip -c > "$temporary.gz"
    [ "$(cat "$temporary.status")" = 0 ]
    rm -f "$temporary.status"
    gzip -t "$temporary.gz"
    step=publish
    destination=/backups/postgres-$(date -u +%Y%m%dT%H%M%SZ).sql.gz
    mv "$temporary.gz" "$destination"
    # The entrypoint runs as root so the postgres process can write through a
    # private host bind mount. Return completed dumps to the bundle owner's
    # UID/GID so the invoking user can inspect and prune them safely.
    chown "$backup_owner" "$destination"
    chmod 0600 "$destination"
    if [ -n "${VONK_BACKUP_OFFHOST_PATH:-}" ]; then
      step=off-host-copy
      # An unreachable mount fails the step instead of hanging the loop. The
      # copy may take as long as it keeps growing; our own copy is trusted, so
      # only its size is compared.
      timeout 10 test -d /offhost
      copy_offhost "$destination" "$offhost_temporary"
      [ "$(timeout 10 stat -c %s "$offhost_temporary")" = "$(stat -c %s "$destination")" ]
      timeout 60 mv "$offhost_temporary" "/offhost/$(basename "$destination")"
    fi
    step=restore-verification
    verified=1
    if ! has_space /tmp $((database_bytes * 2 + margin)) short; then
      echo "PostgreSQL restore verification skipped this round: not enough scratch space" >&2
      verified=0
    else
      restore_verify
    fi
    step=success-marker
    now=$(date +%s)
    if [ "$verified" -eq 1 ]; then
      printf '%s\n' "$now" > "$verification_temporary"
      chmod 0644 "$verification_temporary"
      mv "$verification_temporary" /state/last-backup-restore-verification.epoch
    fi
    printf '%s\n' "$now" > "$state_temporary"
    chmod 0644 "$state_temporary"
    mv "$state_temporary" /state/last-successful-backup.epoch
    echo "PostgreSQL backup completed (restore-verified: $verified): $destination"
  )
  while :; do
    if [ ! -d /backups ]; then
      echo "PostgreSQL backup directory /backups is unavailable; retrying shortly" >&2
      sleep 5 & wait $!
      continue
    fi
    backup_owner=$(stat -c '%u:%g' /backups 2>/dev/null || true)
    case "$backup_owner" in
      ''|*[!0-9:]*|*:*:*)
        echo "PostgreSQL backup directory owner cannot be determined; retrying in five minutes" >&2
        sleep 300 & wait $!
        continue
        ;;
    esac
    prune_dumps /backups
    if [ -n "${VONK_BACKUP_OFFHOST_PATH:-}" ]; then prune_dumps /offhost; fi
    backup_once & attempt_pid=$!
    wait "$attempt_pid"
    attempt_status=$?
    attempt_pid=
    if [ "$attempt_status" -ne 0 ]; then
      sleep 300 & wait $!
      continue
    fi
    prune_dumps /backups
    if [ -n "${VONK_BACKUP_OFFHOST_PATH:-}" ]; then prune_dumps /offhost; fi
    sleep "$interval" & wait $!
  done
}
backup_loop &
backup_pid=$!
status=0
wait "$postgres_pid" || status=$?
kill -TERM "$backup_pid" 2>/dev/null || true
wait "$backup_pid" || true
exit "$status"
