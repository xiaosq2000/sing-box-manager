-- Schema shipped before atomic connection batches. No runtime data.

            CREATE TABLE IF NOT EXISTS service_state (
                service_name TEXT PRIMARY KEY,
                uptime_seconds INTEGER NOT NULL,
                collected_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_user_counters (
                service_name TEXT NOT NULL,
                username TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                PRIMARY KEY (service_name, username)
            );

            CREATE TABLE IF NOT EXISTS monthly_usage (
                cycle_month TEXT NOT NULL,
                username TEXT NOT NULL,
                protocol TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (cycle_month, username, protocol)
            );

            CREATE TABLE IF NOT EXISTS daily_usage (
                usage_date TEXT NOT NULL,
                username TEXT NOT NULL,
                protocol TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (usage_date, username, protocol)
            );

            CREATE INDEX IF NOT EXISTS daily_usage_username_date
                ON daily_usage (username, usage_date);

            CREATE TABLE IF NOT EXISTS stats_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Written by the connection-stream daemon, never by the poller.
            -- `domain` carries two reserved keys alongside real hostnames:
            -- OTHER_DOMAIN_KEY for the tail beyond domain_top_n, and
            -- UNATTRIBUTED_DOMAIN_KEY for the reconciliation shortfall. Both
            -- exist so this table still sums to daily_usage for the same key.
            CREATE TABLE IF NOT EXISTS daily_domain_usage (
                usage_date TEXT NOT NULL,
                username TEXT NOT NULL,
                protocol TEXT NOT NULL,
                domain TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                connection_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (usage_date, username, protocol, domain)
            );

            CREATE INDEX IF NOT EXISTS daily_domain_usage_username_date
                ON daily_domain_usage (username, usage_date);

            CREATE TABLE IF NOT EXISTS daily_destination_usage (
                usage_date TEXT NOT NULL,
                username TEXT NOT NULL,
                destination_ip TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                connection_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (usage_date, username, destination_ip)
            );

            CREATE INDEX IF NOT EXISTS daily_destination_usage_username_date
                ON daily_destination_usage (username, usage_date);

            -- A snapshot, not a log: the daemon replaces each service's rows on
            -- every tick and clears them on shutdown, so the ops view reads
            -- SQLite instead of opening its own gRPC stream.
            CREATE TABLE IF NOT EXISTS live_connections (
                connection_id TEXT PRIMARY KEY,
                service_name TEXT NOT NULL,
                username TEXT NOT NULL,
                network TEXT NOT NULL,
                protocol TEXT NOT NULL,
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                domain TEXT NOT NULL,
                outbound TEXT NOT NULL,
                created_at TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS live_connections_service
                ON live_connections (service_name);

            -- How many bytes of each connection are already in
            -- daily_domain_usage. Every subscription starts with a frame
            -- replaying live connections and up to 1000 already-closed ones, so
            -- without this a daemon restart would bill them a second time.
            -- The counters are cumulative rather than a bare id set because a
            -- connection can be partly billed through UPDATE deltas and then
            -- replayed with its final total; only the remainder is owed.
            CREATE TABLE IF NOT EXISTS stream_attributed_connections (
                service_name TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                upload_bytes INTEGER NOT NULL,
                download_bytes INTEGER NOT NULL,
                attributed_at TEXT NOT NULL,
                PRIMARY KEY (service_name, connection_id)
            );

            CREATE INDEX IF NOT EXISTS stream_attributed_connections_service_time
                ON stream_attributed_connections (service_name, attributed_at);

PRAGMA user_version = 2;
