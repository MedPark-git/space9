# 001 Initial schema
Creates users, audit_logs, cash_journals, and schema_versions through SQLAlchemy metadata.
Execution is guarded by PostgreSQL advisory lock and schema_versions version 1.
