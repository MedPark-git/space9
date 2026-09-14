# Medpark-Cash

MedPark 사내용 자금일보 웹 애플리케이션입니다.

## Stack
- Python 3.11 / Flask / Gunicorn
- PostgreSQL / SQLAlchemy / psycopg
- Asia/Seoul display timezone, UTC database timestamps

## Production rules
- SQLite fallback is intentionally disabled.
- Database credentials are read only from AI SPACE injected environment variables.
- Persistent file assets belong only under `/app/user_data`; business data belongs in PostgreSQL.
- Bootstrap admin is created once only when the user table is empty. The bootstrap password is never stored in source.

## Start
`gunicorn --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-8000} app:app`
