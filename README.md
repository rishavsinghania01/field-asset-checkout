# Field Asset Check-Out Service

A small Django REST API that tracks physical equipment (cameras, laptops, sensors, vehicles) being checked out to and returned by employees. Built for the Artikate backend take-home.

**Screen recording:** [Watch on Google Drive](https://drive.google.com/file/d/1VjGXn0bwWbqqK60xx7L-teLJhWzfgFUp/view?usp=sharing)

- Parts B, C and D are in [`ANSWERS.md`](ANSWERS.md).
- Stack: Python 3.12, Django 5.2, DRF 3.18, PostgreSQL 16, Celery 5 + Redis 7, pytest-django.
- Auth: **DRF token auth** (`Authorization: Token <key>`). The seed command prints a token.

---

## Quick start (Docker)

```bash
git clone https://github.com/rishavsinghania01/field-asset-checkout.git
cd field-asset-checkout

docker compose up --build -d          # app, db, redis, worker, beat
docker compose exec app python manage.py migrate
docker compose exec app python manage.py seed_demo_data   # prints the API token
```

Then:

```bash
export TOKEN=<token printed above>

curl -s localhost:8000/api/v1/health/
curl -s -H "Authorization: Token $TOKEN" localhost:8000/api/v1/assets/
curl -s -H "Authorization: Token $TOKEN" localhost:8000/api/v1/reports/overdue/
curl -s -H "Authorization: Token $TOKEN" localhost:8000/api/v1/employees/EMP-002/summary/

# check out SEN-002 to Priya (EMP-003)
curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d '{"asset_tag":"SEN-002","employee_code":"EMP-003","due_at":"2026-10-05T12:00:00Z"}' \
  localhost:8000/api/v1/checkouts/

# return it (id from the response above)
curl -s -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d '{"condition_note":"all good","needs_maintenance":false}' \
  localhost:8000/api/v1/checkouts/11/return/
```

Run the tests inside the container:

```bash
docker compose exec app python -m pytest -q
```

Trigger the background task by hand (the worker picks it up from Redis; Beat also schedules it hourly):

```bash
docker compose exec app python -c "from assets.tasks import flag_overdue_checkouts as t; print(t.delay().get(timeout=30))"
docker compose logs worker --tail 20
```

Stop everything: `docker compose down` (add `-v` to drop the database volume).

If port 8000 is already taken on your machine, pick another host port:
`APP_PORT=8080 docker compose up --build -d` and use `localhost:8080` in the curl commands.
Postgres and Redis are not published to the host at all, so they cannot clash with a local install.

## Running locally without Docker

You need a PostgreSQL 16 and a Redis reachable from your machine.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_DB=assetdb \
       POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres \
       CELERY_BROKER_URL=redis://localhost:6379/0

python manage.py migrate
python manage.py seed_demo_data
python manage.py runserver

# in other terminals
celery -A config worker -l info
celery -A config beat -l info

python -m pytest -q
```

The tests create their own `test_assetdb` database, so the Postgres user needs `CREATEDB`. There is deliberately no SQLite fallback: the concurrency tests and the partial unique index only mean something on Postgres.

---

## API

All routes are under `/api/v1/`. Everything except `/health/` needs a token. Lists are paginated at 20 (`?page=N`).

| Method | Path | Notes |
|---|---|---|
| `POST` | `/assets/` | `{asset_tag, name, category, purchase_date[, status]}` |
| `GET` | `/assets/` | `?status=`, `?category=`, `?search=` (name or tag), `?ordering=` |
| `GET` | `/assets/{id}/` | includes `current_holder` (`null` or `{employee_code, full_name}`) |
| `POST` | `/checkouts/` | `{asset_tag, employee_code, due_at}` → `201` |
| `POST` | `/checkouts/{id}/return/` | `{condition_note, needs_maintenance}` → `200` |
| `GET` | `/checkouts/`, `/checkouts/{id}/` | read-only convenience; `?employee__employee_code=`, `?asset__asset_tag=` |
| `GET` | `/employees/{employee_code}/summary/` | four numbers, one aggregate query |
| `GET` | `/reports/overdue/` | open + past due, most overdue first |
| `GET` | `/health/` | unauthenticated; `200 {"status":"ok","database":true}`, `503` if the DB does not answer |

Error bodies are `{"detail": "...", "code": "..."}`; field validation errors are keyed by field name (DRF default).

Status codes for the business rules:

| Situation | Code |
|---|---|
| asset not `AVAILABLE` | 409 |
| employee `is_active=False` | 400 |
| employee already holds 3 open check-outs | 409 |
| `due_at` in the past or more than 30 days out | 400 |
| unknown `asset_tag` / `employee_code` / check-out id | 404 |
| return of an already-returned check-out | 409 |
| two simultaneous check-outs of one asset | one 201, one 409 |

---

## How the important bits work

**Check-out is one transaction with two row locks.** `services.check_out_asset` runs inside `transaction.atomic()`, does `SELECT … FOR UPDATE` on the employee row and then the asset row (always in that order, so two requests can never deadlock), re-checks the rules against the locked rows, inserts the `CheckOut` and flips the asset to `CHECKED_OUT`. Either both persist or neither does. The second of two concurrent requests blocks on the asset lock, wakes up, sees `CHECKED_OUT`, and gets a 409.

**The database enforces it too.** `CheckOut` has a partial unique index `uniq_open_checkout_per_asset` on `(asset_id) WHERE returned_at IS NULL`. Even if the locking were bypassed (a future code path, a raw SQL script), a second open row for the same asset is an `IntegrityError`, which the service turns into a 409. Running the concurrency tests with the `select_for_update` calls removed shows this: the same-asset tests still pass on the index alone, the same-employee test fails — which is why the employee row is locked as well.

**Employee summary is a single query.** `queries.employee_summary` annotates the employee row with `COUNT(...) FILTER (WHERE …)` for lifetime / held / overdue and `AVG(returned_at - checked_out_at) FILTER (WHERE returned_at IS NOT NULL)` for the mean hold, all in one `GROUP BY`. The view only converts the interval to days. A test captures the SQL and asserts exactly one query against the app tables.

**Overdue report never queries per row.** `queries.overdue_checkouts` filters open rows with `due_at < now`, `select_related`s asset and employee, annotates `now - due_at`, and orders by `due_at`. The partial index `idx_open_checkout_due_at` on `(due_at) WHERE returned_at IS NULL` serves it. Test asserts two queries for the page (one `COUNT` for pagination, one `SELECT`).

**Background task is idempotent twice over.** `flag_overdue_checkouts` excludes check-outs that already have a notice dated today, then `bulk_create(..., ignore_conflicts=True)` against the `(checkout, notice_date)` unique constraint. Re-running is a no-op; two workers running it at the same instant cannot double-insert. It streams ids with `.iterator()` in batches of 500 so a large overdue set does not get materialised. Beat schedules it hourly.

---

## Tests

44 tests, all against a real Postgres:

- `test_checkout_rules.py` — rules 1–5 and 8, the due_at window, the partial unique index itself
- `test_concurrency.py` — 2 and 8 simultaneous check-outs of one asset; 3 simultaneous check-outs by one employee already at two
- `test_return.py` — rule 6, maintenance flag, double return, re-checkout
- `test_summary_and_report.py` — the four numbers against controlled data; single-query assertion; overdue boundary with an item due exactly now; ordering; no-query-per-row assertion
- `test_tasks.py` — creates one notice per overdue item, five runs produce no duplicates, picks up newly overdue items, skips returned ones, Beat registration
- `test_seed_command.py` — assignment minimums, re-runnable, prints token
- `test_assets_and_health.py` — auth, create, filter/search, pagination, `current_holder`

Concurrency tests use `transaction=True` so each thread has its own connection and transaction; a `threading.Barrier` releases them together.

---

## Assumptions

1. **"Overdue" means `due_at < now`, strictly.** An item due at exactly this instant is not overdue yet; one tick later it is. Tested both sides of the boundary.
2. **Rule precedence when several apply**: 404 (unknown asset/employee) → 400 (inactive employee) → 409 (asset not available) → 409 (three-open limit). Unknown records are checked first because there is nothing meaningful to say about them; inactive-employee comes before asset status because it is a property of the caller, not the resource.
3. **`due_at` must carry a timezone offset** (`2026-10-05T12:00:00Z` or `+05:30`). A naive value is treated as UTC (DRF default under `USE_TZ`).
4. **`/health/` returns 503, not 200, when the database is unreachable.** The body still reports `database: false`. The spec says "returns 200 and reports connectivity"; I read the 200 as the healthy case — a load balancer should not keep routing to an instance whose DB is gone.
5. **`needs_maintenance: true` on return sets the asset to `MAINTENANCE`**, otherwise `AVAILABLE`. There is no endpoint in the spec for moving a `MAINTENANCE` asset back to `AVAILABLE`; the admin (`/admin/`) is the way to do it.
6. **Returning does not check who returns.** The spec gives no actor for the return call.
7. **`mean_hold_days`** is rounded to two decimals and is `null` when the employee has never returned anything. Only returned items count, as specified.
8. **`days_overdue`** is whole days, floored (`interval.days`). 23 hours overdue reports `0`.
9. **`seed_demo_data` is a reset, not a merge.** Assets and employees are upserted by tag/code; check-outs and notices are wiped and rebuilt so the dataset is identical after every run. Anything created through the API since the last seed is discarded. The API token for user `reviewer` is stable across runs.
10. **No extra model fields.** Two things were added that are not fields: the partial unique index on open check-outs (see above) and a partial index on `(due_at) WHERE returned_at IS NULL`. Both are in `0001_initial`.
11. **`GET /checkouts/` and `GET /checkouts/{id}/`** exist as read-only conveniences. They were not asked for but make the demo easier to follow; they are token-protected and paginated like everything else.
12. **Beat runs in its own container**, so `docker-compose.yml` has five services rather than four. Running Beat inside the worker (`-B`) is fine for one worker but silently double-schedules the moment you scale to two; a separate `beat` service is the shape I would ship.
13. **Token auth over JWT**: there is no login UI and a reviewer with `curl` needs a token that just works. Tokens do not expire; for an internal tool behind a VPN that is acceptable, for anything public I would switch to short-lived JWTs.
14. **Time zone is UTC everywhere**; `notice_date` is the UTC date of the run.

## Known gaps

- **No SQLite path.** Tests need Postgres. Deliberate, but it means `pytest` fails fast without a database rather than silently running a weaker suite.
- **`flag_overdue_checkouts` reports `created` as attempted inserts.** With `ignore_conflicts=True` Postgres does not say how many rows it skipped, so in the rare case of two workers running the task at the same instant the count can over-report. The row count in the table is always correct.
- **Nothing sends the overdue notice anywhere.** The task records the notice; email delivery is out of scope and would be a separate task fanned out per notice (see Part B snippet 3 in `ANSWERS.md` for why it must be separate).
- **No throttling / rate limiting** on the API.
- **No employee CRUD endpoints.** Employees are created by the seed command or the admin; the spec did not ask for more.
