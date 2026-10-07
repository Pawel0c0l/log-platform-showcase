# DB migrations (`ops/db_migrate.sh`)

## Uruchomienie

```bash
cd /opt/log-platform
set -a
source .env
set +a
bash ops/db_migrate.sh
```

Skrypt:
- tworzy `public.schema_migrations` jeśli nie istnieje,
- aplikuje `db/migrations/*.sql` alfabetycznie,
- pomija migracje już zapisane w rejestrze.

## Weryfikacja

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT * FROM public.schema_migrations ORDER BY applied_at DESC;"
```
