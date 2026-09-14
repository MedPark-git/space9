# Migrations
Schema changes are applied idempotently under a PostgreSQL advisory transaction lock.
The `schema_versions` table records applied versions and prevents duplicate execution.
Initial schema version: 1.
