-- Bootstraps a stock PostgreSQL server so its managing role looks like the RDS
-- master user: a member of rds_superuser with CREATEDB and CREATEROLE, but not a
-- superuser, and holding neither REPLICATION nor BYPASSRLS. Run once, as the
-- container superuser, by `make test-setup` and by CI.
--
-- hack/local/00-servers.yaml embeds this file verbatim and adds GRANT SET ON
-- PARAMETER in a second script. That grant is deliberately absent here: the
-- integration suite needs a server that still refuses a superuser-only GUC.

CREATE ROLE rds_superuser NOLOGIN;
CREATE ROLE pgadmin LOGIN PASSWORD 'adminpw' CREATEDB CREATEROLE;
GRANT rds_superuser TO pgadmin;

-- RDS makes rds_superuser a member of pg_monitor, which is what lets the
-- master user read other roles' rows in pg_stat_replication.
GRANT pg_monitor TO rds_superuser;

-- On RDS, membership of rds_replication lets a login stream from a slot, and
-- rds_superuser holds it WITH ADMIN OPTION so the master user can grant it. A
-- stock server only honours the REPLICATION attribute, so here the role exists
-- for grants to be issued and read back, and hack/rds-replication-shim.sql
-- confers the attribute on its direct LOGIN members.
CREATE ROLE rds_replication NOLOGIN;
GRANT rds_replication TO rds_superuser WITH ADMIN OPTION;

-- RDS lets its master user create subscriptions; PostgreSQL 16 added
-- pg_create_subscription as the stock equivalent. There is none before 16.
DO $$
BEGIN
  IF current_setting('server_version_num')::int >= 160000 THEN
    EXECUTE 'GRANT pg_create_subscription TO rds_superuser';
  END IF;
END $$;
