#!/usr/bin/env bash
# The integration suite's PostgreSQL servers: a source and a target, each
# bootstrapped like RDS (hack/rds-like.sql), started with wal_level=logical, on
# one Docker network so the target's server can subscribe to the source.
#
#   hack/test-servers.sh up           start both servers (replacing old ones)
#   hack/test-servers.sh env          print `export` lines for the test suite
#   hack/test-servers.sh github-env   the same, in $GITHUB_ENV's format
#   hack/test-servers.sh down         remove both servers and the network
#
# Images and ports come from the environment: SOURCE_IMAGE (postgres:16-alpine),
# TARGET_IMAGE (same as source), SOURCE_PORT (15432), TARGET_PORT (15433).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)

network=pgop-test
source_name=pgop-test
target_name=pgop-test-target
source_image=${SOURCE_IMAGE:-postgres:16-alpine}
target_image=${TARGET_IMAGE:-$source_image}
source_port=${SOURCE_PORT:-15432}
target_port=${TARGET_PORT:-15433}

dsn() { # port user password
  echo "host=localhost port=$1 user=$2 password=$3 dbname=postgres sslmode=disable"
}

variables() {
  echo "PGOP_TEST_DSN=$(dsn "$source_port" pgadmin adminpw)"
  echo "PGOP_TEST_TARGET_DSN=$(dsn "$target_port" pgadmin adminpw)"
  # The source as the *target server* reaches it: by container name, on the
  # container port. Subscriptions connect server to server, not via the host.
  echo "PGOP_TEST_SOURCE_FROM_TARGET=host=$source_name port=5432"
  # Used only to emulate rds_replication (hack/rds-replication-shim.sql).
  echo "PGOP_TEST_SUPERUSER_DSN=$(dsn "$source_port" postgres testpw)"
}

start() { # name image port
  docker run -d --name "$1" --network "$network" \
    -e POSTGRES_PASSWORD=testpw -p "$3:5432" "$2" \
    -c wal_level=logical -c max_connections=200 >/dev/null
}

wait_ready() { # name
  # Over TCP: during initialisation the entrypoint's temporary server listens
  # on the socket only, so a socket probe reports ready too early.
  for _ in $(seq 1 60); do
    if docker exec "$1" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  echo "$1 did not become ready" >&2
  docker logs "$1" >&2
  exit 1
}

bootstrap() { # name
  docker exec -i "$1" psql -q -v ON_ERROR_STOP=1 -U postgres -d postgres \
    < "$here/rds-like.sql"
}

down() {
  docker rm -f "$source_name" "$target_name" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}

case ${1:-} in
  up)
    down
    docker network create "$network" >/dev/null
    start "$source_name" "$source_image" "$source_port"
    start "$target_name" "$target_image" "$target_port"
    for name in "$source_name" "$target_name"; do
      wait_ready "$name"
      bootstrap "$name"
    done
    echo "source $source_image on :$source_port, target $target_image on :$target_port"
    ;;
  env)
    variables | sed -E "s/^([A-Z_]+)=(.*)$/export \1='\2'/"
    ;;
  github-env)
    variables
    ;;
  down)
    down
    ;;
  *)
    echo "usage: $0 up|env|github-env|down" >&2
    exit 2
    ;;
esac
