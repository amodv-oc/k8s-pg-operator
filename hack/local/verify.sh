#!/bin/bash
# Functional check of the OWNER/RW/RO model, run inside each server's pod
# using the credentials the operator generated.
set -uo pipefail
v=$1
pod=$(kubectl get pod -n pg-instances -l app=pg$v -o jsonpath='{.items[0].metadata.name}')
get() { kubectl get secret -n team-a "$1" -o jsonpath="{.data.password}" | base64 -d; }
MIG=$(get orders-migrator-$v-creds)
API=$(get orders-api-$v-creds)
ANA=$(get analytics-$v-creds)

run() { # run <user> <password> <sql>
  kubectl exec -n pg-instances "$pod" -- env PGPASSWORD="$2" \
    psql -h 127.0.0.1 -U "$1" -d orders -tAX -v ON_ERROR_STOP=1 -c "$3" 2>&1 | tr -d '\r'
}
ok()   { printf '    %-46s %s\n' "$1" "PASS"; }
fail() { printf '    %-46s %s\n' "$1" "FAIL: $2"; PGFAIL=1; }
expect_ok()   { out=$(run "$2" "$3" "$4"); [[ $? -eq 0 ]] && ok "$1" || fail "$1" "$out"; }
expect_deny() {
  out=$(run "$2" "$3" "$4")
  if [[ "$out" == *"permission denied"* || "$out" == *"must be owner"* ]]; then ok "$1"
  else fail "$1" "expected a permission error, got: $out"; fi
}

echo "== PostgreSQL $v =="
PGFAIL=0

# OWNER: creates objects, and they must land on the owner GROUP, not the login
# role, or the group's default privileges would not apply to them.
expect_ok "owner creates table in audit"   orders_migrator "$MIG" \
  "CREATE TABLE IF NOT EXISTS audit.events(id serial primary key, note text)"
expect_ok "owner creates table in public"  orders_migrator "$MIG" \
  "CREATE TABLE IF NOT EXISTS public.widgets(id serial primary key, name text)"
owner=$(run orders_migrator "$MIG" "SELECT tableowner FROM pg_tables WHERE tablename='events'")
[[ "$owner" == "orders_owner" ]] && ok "new table owned by orders_owner" \
  || fail "new table owned by orders_owner" "owner is '$owner'"

# RW: full DML on what the owner created, but no DDL.
expect_ok   "rw inserts"        orders_api "$API" "INSERT INTO audit.events(note) VALUES ('x')"
expect_ok   "rw selects"        orders_api "$API" "SELECT count(*) FROM audit.events"
expect_ok   "rw updates"        orders_api "$API" "UPDATE audit.events SET note='y'"
expect_ok   "rw uses sequence"  orders_api "$API" "SELECT nextval('audit.events_id_seq')"
expect_ok   "rw deletes"        orders_api "$API" "DELETE FROM audit.events WHERE note='nope'"
expect_deny "rw cannot create table" orders_api "$API" "CREATE TABLE audit.nope(id int)"
expect_deny "rw cannot drop table"   orders_api "$API" "DROP TABLE audit.events"

# RO: reads only.
expect_ok   "ro selects"             analytics "$ANA" "SELECT count(*) FROM audit.events"
expect_ok   "ro selects public tbl"  analytics "$ANA" "SELECT count(*) FROM public.widgets"
expect_deny "ro cannot insert"       analytics "$ANA" "INSERT INTO audit.events(note) VALUES ('z')"
expect_deny "ro cannot update"       analytics "$ANA" "UPDATE audit.events SET note='z'"
expect_deny "ro cannot delete"       analytics "$ANA" "DELETE FROM audit.events"

# Per-user parameters and connection limits are applied to the login role.
sto=$(run analytics "$ANA" "SHOW statement_timeout")
[[ "$sto" == "30s" ]] && ok "ro statement_timeout applied" || fail "ro statement_timeout applied" "$sto"
lim=$(run orders_migrator "$MIG" "SELECT rolconnlimit FROM pg_roles WHERE rolname='orders_migrator'")
[[ "$lim" == "4" ]] && ok "owner connection limit applied" || fail "owner connection limit applied" "$lim"

# A brand-new table must be reachable by RW and RO with no per-user change:
# that is what ALTER DEFAULT PRIVILEGES FOR ROLE orders_owner buys.
run orders_migrator "$MIG" "CREATE TABLE IF NOT EXISTS audit.fresh(id int)" >/dev/null
expect_ok "rw reaches a table created after grant" orders_api "$API" "SELECT * FROM audit.fresh"
expect_ok "ro reaches a table created after grant" analytics "$ANA" "SELECT * FROM audit.fresh"

exit $PGFAIL
