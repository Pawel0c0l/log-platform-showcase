**English** · [Polski](README.pl.md)

# Demo stack

Brings up the whole stack (Postgres 16, MinIO, the API with the portal) as an isolated Compose project, on ports that do not collide with any other instance on the same machine, and onboards two synthetic clients with the same script that onboards production clients.

```bash
./demo/up.sh
```

The first run builds the API image (a few minutes); later runs take under a minute. When it finishes:

| What | Address |
|---|---|
| API health | http://127.0.0.1:8010/health |
| Operator portal | http://127.0.0.1:8010/login (`admin` / `demo-admin`; override the password with `PORTAL_ADMIN_PASSWORD`) |
| MinIO console | http://127.0.0.1:9011 (credentials in `demo/.env.demo`) |

After logging in, Artifacts, Administration (users, groups, client access, datasets, Eco Driving permissions, report folders, audit) and Data work right away. The portal shows a user only the datasets and report folders an administrator has assigned to them, so the Data tab starts empty: access to clients `ALPHA00001` and `BRAVO00016` is granted under Administration → Client access and Datasets. This is production behaviour, not a gap in the demo.

Clean-up, volumes and image included:

```bash
./demo/down.sh
```

## What `up.sh` does

1. Generates `demo/.env.demo` from `.env.example`: random secrets in place of `CHANGE_ME`, a fresh platform identity UUID, demo ports.
2. Builds the API image and starts the containers (`docker-compose.yml` plus the `demo/docker-compose.demo.yml` overlay), then waits for `/health`, because the API creates the base tables on startup.
3. Applies the platform database migrations with `ops/db_migrate.sh`. On an empty database the pass stops three times, by design of the migrations, and each time the script creates the missing state the way production does:
   - migration 047 requires an environment identity: the script writes a `local_dev` marker;
   - migration 060 rewrites one production client's schedule and has nothing to rewrite on a fresh database: it is recorded as applied;
   - migration 072 changes a constraint (that part runs) and then asserts the state of two production clients: the script onboards two synthetic clients, sets the value the migration demands, and records it as applied.
4. Onboards clients `ALPHA00001` and `BRAVO00016` from `demo/clients/*.yaml` with `scripts/onboard_workflow_a_client.py`: a separate business database per client, DDL from `db/client_business`, grants, control-plane rows. The telematics provider is never contacted (`--skip-provider-auth-check`). The clients end in state `CREATED_DISABLED_STRICT`, the first of nine states of the onboarding state machine; schedules are disabled.
5. Creates the portal administrator account.

Host-side tools (onboarding, admin bootstrap) are not installed on the host. They run in a one-off `tools` container from the overlay, on the Compose network, so the client databases are reachable under the same host name for the scripts and for the API.

## What the demo does not include

- **Trips.** The client databases have the full schema but are empty. Filling them needs access to the provider's API or a synthetic trip generator, which this snapshot does not contain.
- **Schedules.** In production the jobs are run by systemd (`ops/systemd`). Nothing runs periodically in the demo.
- **Dashboard delivery.** The `delivery/` layer is a Cloudflare Worker with D1 and R2; the demo does not deploy it. The driver dashboard itself can be viewed without the stack, see the main README.

## Requirements and verification status

Docker with the Compose plugin version 2.24 or newer (the overlay uses `!override` and `!reset`), and Python 3 on the host only to generate the env file. A full `up.sh` run from an empty state was performed on 2026-10-07 on Linux with Docker 29 and Compose v5: 71 migrations, two clients, portal login.

## A note on the API image

The image built from the `api/` directory does not contain the `ops` package, which `api/platform_prune.py` has required for some time. Production runs the API natively from the repository checkout, where the import resolves. The demo overlay mounts `ops/` into the container read-only.
