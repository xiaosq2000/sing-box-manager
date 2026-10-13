"""Create a dedicated database without altering or replacing existing data."""

from psycopg import sql

from sing_box_manager.settings import ControlPlaneSettings


def provision_sql(database_name: str) -> str:
    name = ControlPlaneSettings(database_name=database_name).database_name
    identifier = sql.Identifier(name)
    create = sql.SQL("CREATE DATABASE {} OWNER sbm").format(identifier).as_string()
    return (
        sql.SQL(r"""SELECT pg_advisory_lock(1396854084);
DO $$
BEGIN
    IF current_setting('server_version_num')::integer < 140000 THEN
        RAISE EXCEPTION 'PostgreSQL 14 or later is required';
    END IF;
    IF EXISTS (
        SELECT FROM regexp_split_to_table(current_setting('listen_addresses'), ',') AS address
        WHERE trim(address) NOT IN ('', 'localhost', '127.0.0.1', '::1')
    ) THEN
        RAISE EXCEPTION 'PostgreSQL must listen only on loopback or Unix sockets';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sbm') THEN
        CREATE ROLE sbm LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS (
        SELECT FROM pg_roles WHERE rolname = 'sbm'
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR NOT rolcanlogin)
    ) OR EXISTS (
        SELECT FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = 'sbm')
    ) THEN
        RAISE EXCEPTION 'Existing sbm database role has incompatible privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_database WHERE datname = {literal}
        AND datdba <> (SELECT oid FROM pg_roles WHERE rolname = 'sbm')
    ) THEN
        RAISE EXCEPTION 'Refusing a database owned by another role';
    END IF;
END $$;
SELECT {create}
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = {literal})
\gexec
REVOKE ALL ON DATABASE {identifier} FROM PUBLIC;
\connect {identifier}
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO sbm;
""")
        .format(
            literal=sql.Literal(name), identifier=identifier, create=sql.Literal(create)
        )
        .as_string()
    )
