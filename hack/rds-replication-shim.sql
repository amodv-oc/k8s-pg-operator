-- Emulates RDS's rds_replication rule on a stock server. Test-only; run as the
-- container superuser.
--
-- On RDS a login granted rds_replication may open a walsender connection and
-- stream from a slot. Stock PostgreSQL checks only the REPLICATION attribute,
-- which a non-superuser managing role cannot confer, so this gives it to every
-- LOGIN role that is a *direct* member of rds_replication.
--
-- Direct only: the managing role is an indirect member through rds_superuser,
-- exactly as on RDS, and must keep NOREPLICATION - otherwise the harness would
-- hide the "ALTER ROLE ... NOREPLICATION is refused" class of bug.
--
-- Revocation is not emulated: a login that leaves rds_replication keeps the
-- attribute until it is dropped.
DO $$
DECLARE
  login name;
BEGIN
  FOR login IN
    SELECT m.rolname
      FROM pg_auth_members am
      JOIN pg_roles g ON g.oid = am.roleid AND g.rolname = 'rds_replication'
      JOIN pg_roles m ON m.oid = am.member
     WHERE m.rolcanlogin AND NOT m.rolreplication AND NOT m.rolsuper
  LOOP
    EXECUTE format('ALTER ROLE %I REPLICATION', login);
    RAISE NOTICE 'emulated rds_replication: % now has REPLICATION', login;
  END LOOP;
END $$;
