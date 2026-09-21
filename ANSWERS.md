# ANSWERS

Parts B, C and D of the Artikate backend take-home. Part A is the code in this repository; see `README.md`.

---

## Part B — Diagnose three broken snippets

### Snippet 1 — overdue report view

#### 1. What is wrong

**(a) N+1 queries.** `c.asset.name`, `c.asset.asset_tag` and `c.employee.full_name` are accessed inside the loop with no `select_related`. Every open check-out costs two extra `SELECT`s. With 5,000 open items that is 10,001 queries for one page load.

**(b) Filtering in Python instead of SQL.** The queryset only filters `returned_at__isnull=True`; the `due_at < now` test happens in the loop. Every open check-out, overdue or not, is pulled over the wire and instantiated as a model object just to be discarded.

**(c) Sorting in Python, no pagination.** `rows.sort(...)` sorts the full list in memory and the whole thing is serialised into one response. Nothing bounds the size of the payload.

**(d) `timezone.now()` is called repeatedly.** Once per row in the `if`, and again per row for `days_overdue`. Rows are compared against slightly different instants, and a row that becomes overdue mid-loop is measured against a different clock than its neighbours. Small, but it means the report is not a consistent snapshot.

**(e) No authentication and no method restriction.** This is a plain Django view, not a DRF view: no `IsAuthenticated`, no `@api_view(["GET"])`. It answers `POST` and it answers anonymous callers. The spec says every endpoint except health requires auth.

**(f) Output is missing the employee code** (spec asks for asset name, tag, employee code, employee name, days overdue). Minor, but it is a contract violation.

#### 2. Why it looks correct locally

- A dev database has ten check-outs. Ten rows means twenty extra queries that finish in a few milliseconds; nobody notices 21 queries versus 1 unless they are counting.
- Loading "all open rows" and filtering in Python gives the *right answer*; it only becomes a problem when the open set is large, which it never is on a laptop.
- No pagination is invisible when the result fits on one screen.
- Local settings typically run with `DEBUG=True` and a logged-in browser session, or the developer tests with `curl` and never tries an anonymous request, so the missing auth is never exercised.
- The repeated `now()` calls only differ by microseconds; the inconsistency cannot be observed without thousands of rows.

#### 3. Fix

```python
from django.db.models import DurationField, ExpressionWrapper, F, Value
from django.utils import timezone
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated


class OverdueRowSerializer(serializers.Serializer):
    asset_name = serializers.CharField(source="asset.name")
    asset_tag = serializers.CharField(source="asset.asset_tag")
    employee_code = serializers.CharField(source="employee.employee_code")
    employee_name = serializers.CharField(source="employee.full_name")
    days_overdue = serializers.SerializerMethodField()

    def get_days_overdue(self, obj):
        return obj.overdue_for.days


class OverdueReportView(ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OverdueRowSerializer
    # pagination comes from REST_FRAMEWORK["DEFAULT_PAGINATION_CLASS"], PAGE_SIZE=20

    def get_queryset(self):
        now = timezone.now()                       # one instant for the whole report
        return (
            CheckOut.objects
            .filter(returned_at__isnull=True, due_at__lt=now)   # filter in SQL
            .select_related("asset", "employee")                # no N+1
            .annotate(overdue_for=ExpressionWrapper(
                Value(now) - F("due_at"), output_field=DurationField()))
            .order_by("due_at")                                 # most overdue first, in SQL
        )
```

Plus a partial index so the filter is cheap at scale:

```python
models.Index(fields=["due_at"], condition=Q(returned_at__isnull=True), name="idx_open_checkout_due_at")
```

This is what `assets/queries.py` and `OverdueReportView` in this repo do.

#### 4. What would have caught it

- `assertNumQueries` / `CaptureQueriesContext` in a test that seeds 15+ rows and asserts a constant number of queries (see `test_overdue_report_does_not_query_per_row`). This is the single cheapest guard against N+1 and it fails the moment someone removes `select_related`.
- `django-debug-toolbar` or `nplusone` in development, which flag repeated identical queries.
- A test that hits the endpoint without credentials and expects 401.
- A seed of ~50k rows and a p95 check in a load test (`locust`, `k6`) before it reaches production; or after: an APM trace / slow query log showing thousands of tiny queries per request.

---

### Snippet 2 — check-out endpoint

#### 1. What is wrong

**(a) No transaction.** `CheckOut.objects.create(...)` and `asset.save()` are two separate autocommitted statements. If the process dies, the connection drops, or `asset.save()` raises between them, a `CheckOut` row exists next to an `AVAILABLE` asset. This is exactly the state rule 5 forbids.

**(b) Check-then-act race on the asset.** Two requests read `status == "AVAILABLE"` at the same moment, both pass the `if`, both insert a `CheckOut`. Nothing is locked, so rule 7 is violated. The same race exists on `open_count` for rule 3: two concurrent requests for the same employee each see `open_count == 2` and both proceed, leaving the employee at four.

**(c) `asset.save()` writes every field.** The `asset` instance was loaded at the top of the request. If anyone updated another column (say `name`) in between, this `save()` silently overwrites it with the stale value. It should be `save(update_fields=[...])`, or `Asset.objects.filter(pk=...).update(status=...)`.

**(d) `DoesNotExist` becomes a 500.** `Asset.objects.get(...)` and `Employee.objects.get(...)` raise `DoesNotExist` for an unknown tag or code. That surfaces as a 500, not the 404 rule 8 requires. Same for a missing key: `request.data["asset_tag"]` raises `KeyError` → 500 instead of 400.

**(e) `due_at` is not validated.** Whatever string arrives is passed straight to the model. Past dates and dates a year out are accepted (rule 4 ignored); a malformed string raises a `ValidationError` from the field at `create()` time, which is again a 500. A naive datetime is accepted and silently given the server's timezone.

**(f) Inactive employees are not checked** (rule 2).

**(g) Nothing states auth or permissions.** `@api_view` uses the project defaults; if those are DRF's own defaults (`AllowAny`), this endpoint is open.

#### 2. Why it looks correct locally

- **Django's `TestCase` wraps every test in a transaction.** That is the big one for (a): inside a test the two writes *are* atomic because the test's own transaction surrounds them, and it is rolled back at the end regardless. The missing `atomic()` is invisible to the standard test runner. You only see it with `TransactionTestCase` and a failure injected between the two statements.
- (b) needs two requests inside the same few milliseconds. A developer clicking in a browser, or a sequential test suite, never produces that. A dev server with a single worker thread cannot even reproduce it.
- (c) needs a concurrent writer on the same row; same story.
- (d)/(e): tests send well-formed bodies with valid tags, so the failure paths never run. With `DEBUG=True` a 500 shows a yellow traceback page which looks like "an error was handled".
- (f): dev fixtures have every employee active.

#### 3. Fix

```python
from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound, ValidationError

class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError("due_at must be in the future.")
        if value > now + timedelta(days=30):
            raise serializers.ValidationError("due_at must be within 30 days.")
        return value


@transaction.atomic
def check_out_asset(*, asset_tag, employee_code, due_at):
    # lock order is fixed (employee, then asset) so two requests cannot deadlock
    try:
        employee = Employee.objects.select_for_update().get(employee_code=employee_code)
    except Employee.DoesNotExist:
        raise NotFound(f"Employee {employee_code!r} not found.")
    try:
        asset = Asset.objects.select_for_update().get(asset_tag=asset_tag)
    except Asset.DoesNotExist:
        raise NotFound(f"Asset {asset_tag!r} not found.")

    if not employee.is_active:
        raise ValidationError({"employee_code": "Employee is inactive."})
    if asset.status != Asset.Status.AVAILABLE:
        raise Conflict(f"Asset {asset_tag} is {asset.status}.")
    if CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3:
        raise Conflict("Employee already holds three open check-outs.")

    try:
        checkout = CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at)
    except IntegrityError:   # partial unique index on (asset) WHERE returned_at IS NULL
        raise Conflict(f"Asset {asset_tag} already has an open check-out.")

    asset.status = Asset.Status.CHECKED_OUT
    asset.save(update_fields=["status", "updated_at"])
    return checkout


class CheckOutViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]

    def create(self, request):
        payload = CheckOutCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        checkout = check_out_asset(**payload.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=201)
```

With the model constraint that makes the database the last line of defence:

```python
models.UniqueConstraint(fields=["asset"], condition=Q(returned_at__isnull=True),
                        name="uniq_open_checkout_per_asset")
```

`SELECT ... FOR UPDATE` makes the second concurrent request wait on the row, then re-read `status` as `CHECKED_OUT` and return 409. The partial unique index means that even a code path that forgets to lock cannot insert a second open row.

#### 4. What would have caught it

- A **`TransactionTestCase`/`transaction=True` test with two threads** released by a `threading.Barrier` against one asset, asserting `[201, 409]` (`test_concurrency.py` here). I also ran that test with the `select_for_update` calls removed: the same-asset case still passed on the unique index alone, but the same-employee case returned three 201s. That experiment is what justified locking the employee row.
- A test that mocks `asset.save` to raise after `CheckOut.objects.create` and asserts no `CheckOut` row survives (needs `TransactionTestCase`, for the reason above).
- Tests for unknown tag → 404, missing field → 400, past `due_at` → 400.
- A DB-level unique constraint: the schema itself refuses the bad state, and a migration review would ask "what stops two open rows?".
- Load testing the endpoint with a few hundred concurrent identical requests (`hey -c 50 -n 500`) and counting `CheckOut` rows afterwards.

---

### Snippet 3 — nightly notice task

#### 1. What is wrong

**(a) Not idempotent, so retries duplicate.** There is no "already notified today" check. If the task fails halfway (broker hiccup, DB timeout on row 3,000) and Celery retries it, or Beat fires it again, every check-out processed before the failure gets a second notice and a second email. If the `(checkout, notice_date)` unique constraint from Part A exists, the retry instead crashes on the first duplicate with `IntegrityError` and never reaches the unprocessed rows. Either way the retry does the wrong thing.

**(b) Model instances passed to `.delay()`.** `deliver_email.delay(c.employee, c)` hands ORM objects to Celery. With the default JSON serializer this raises `kombu.exceptions.EncodeError` at enqueue time. If someone "fixed" that by enabling the pickle serializer, it is a remote code execution vector and it also ships a stale snapshot: by the time the email task runs the check-out may already be returned. Tasks should receive primary keys and re-fetch.

**(c) Email is enqueued outside any transaction boundary and before it is safe.** The notice `INSERT` and `deliver_email.delay(...)` are interleaved with no `transaction.on_commit`. If the surrounding transaction (or a future `atomic()` wrapper) rolls back, emails still go out for notices that do not exist. And because of (a), a retry re-sends emails already delivered.

**(d) The whole result set is loaded into memory and processed one row at a time.** `for c in overdue` caches every row on the queryset. At tens of thousands of rows: tens of thousands of model instances in RAM, one `INSERT` round-trip each, one broker publish each, and `c.employee` is an N+1 on top (no `select_related`). A run that takes long enough crosses the Redis broker's visibility timeout (default one hour), at which point the *same* task is redelivered to another worker while the first is still running, doubling everything.

**(e) `overdue.count()` re-runs the query at the end** with a fresh `timezone.now()`. It is an extra `COUNT(*)` over the table and the number can differ from what was iterated (rows returned or became overdue during the run), so the return value is not what the task actually did.

**(f) `timezone.now().date()` is evaluated per row.** A run that starts at 23:59:58 will stamp some notices with today and some with tomorrow.

**(g) No retry policy, no overlap guard.** No `bind=True`/`autoretry_for`/`max_retries`, and nothing stops Beat from starting a second run while a slow one is in progress.

#### 2. Why it looks correct locally

- Tests and dev settings almost always use `CELERY_TASK_ALWAYS_EAGER=True`. With eager mode `.delay(...)` calls the function in-process, **no serialisation ever happens**, so passing model instances "works". It breaks the first time a real worker consumes from a real broker.
- The overdue set locally is ten rows. Memory, N+1, per-row inserts and broker round-trips are all sub-second.
- Nothing fails mid-run on a laptop, so retries never happen and the missing idempotency is never exercised.
- Nobody runs the dev task at 23:59:59.
- The unique constraint may not even exist in the local schema if migrations are behind.

#### 3. Fix

Split it into a batched, idempotent "flag" step and a per-notice delivery step that receives ids and is itself idempotent:

```python
from celery import shared_task
from django.db import transaction
from django.utils import timezone

BATCH = 500

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def send_overdue_notices(self):
    now = timezone.now()
    today = now.date()                       # one date for the whole run

    candidate_ids = (
        CheckOut.objects
        .filter(returned_at__isnull=True, due_at__lt=now)
        .exclude(notices__notice_date=today)  # already done today -> skip, so retries are no-ops
        .order_by("id")
        .values_list("id", flat=True)
    )

    created = 0
    batch = []
    for checkout_id in candidate_ids.iterator(chunk_size=BATCH):   # server-side cursor
        batch.append(OverdueNotice(checkout_id=checkout_id, notice_date=today))
        if len(batch) >= BATCH:
            created += _flush(batch); batch = []
    if batch:
        created += _flush(batch)
    return {"date": today.isoformat(), "created": created}


def _flush(batch):
    with transaction.atomic():
        # unique (checkout, notice_date) + ignore_conflicts: safe even if two workers overlap
        notices = OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)
        ids = list(OverdueNotice.objects.filter(
            checkout_id__in=[n.checkout_id for n in batch], notice_date=batch[0].notice_date
        ).values_list("id", flat=True))
        # fan out only after the notices are durable, and pass ids, not instances
        transaction.on_commit(lambda: [deliver_email.delay(nid) for nid in ids])
    return len(batch)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def deliver_email(self, notice_id):
    notice = (OverdueNotice.objects
              .select_related("checkout__employee", "checkout__asset")
              .get(pk=notice_id))
    if notice.sent_at:                      # delivery is idempotent too
        return
    send_mail(...)
    OverdueNotice.objects.filter(pk=notice_id, sent_at__isnull=True).update(sent_at=timezone.now())
```

(`sent_at` is an extra field on `OverdueNotice` this design would need; the Part A model does not have it because Part A does not send anything.) For a Beat-driven task I would also add a Redis lock (`cache.add("lock:send_overdue_notices", ..., timeout=...)`) at the top so overlapping runs exit early, and set `task_acks_late=True` with a visibility timeout longer than the worst-case run.

`assets/tasks.py` in this repo implements the flag step this way; the tests run it five times and assert one notice per check-out.

#### 4. What would have caught it

- **A test with eager mode off and `task_serializer="json"`**: `deliver_email.delay(instance)` raises `EncodeError` immediately. Even simpler: `deliver_email.s(c.employee, c).freeze()`/`apply_async` in a unit test with the JSON serializer.
- **An idempotency test**: run the task twice, assert `OverdueNotice.objects.count()` did not change (`test_task_is_idempotent_within_a_day`).
- **A partial-failure test**: patch `deliver_email.delay` to raise on the third call, run, catch, run again, assert no duplicate notices and no duplicate email calls.
- A test seeding 50,000 overdue rows and asserting a bounded query count and runtime.
- In production: Flower / task runtime metrics showing runtime growing linearly with table size, and an alert on duplicate emails from the mail provider's logs.

---

## Part C — Optimise a slow PostgreSQL query

### 1. Rewritten query

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at
FROM checkouts c
JOIN employees e
  ON e.id = c.employee_id
 AND e.is_active
WHERE c.returned_at IS NULL
  AND c.checked_out_at >= TIMESTAMPTZ '2026-01-01 00:00:00+05:30'
  AND c.checked_out_at <  TIMESTAMPTZ '2026-07-01 00:00:00+05:30'
ORDER BY c.due_at
LIMIT 200;
```

Change by change:

| Change | Why | Cost / gain |
|---|---|---|
| `DATE(c.checked_out_at) BETWEEN ...` → half-open range on the raw column | Wrapping the column in `DATE()` makes the predicate non-sargable: no B-tree on `checked_out_at` can be used, so it is a full scan regardless of indexes. Comparing the column directly lets an index range scan do the work. | Pure gain. Semantics preserved with `>= start AND < next_day`. |
| Explicit `+05:30` offsets | `DATE(timestamptz)` uses the session `TimeZone`. The same query returns different rows from a `psql` session in UTC and from Django (which sets `TimeZone=UTC`) versus a reporting tool set to IST. Making the boundary explicit removes that ambiguity. | No cost; it forces a decision about which calendar the "January" screen means. |
| `IN (SELECT id FROM employees WHERE is_active)` → `JOIN ... AND e.is_active` | Postgres already turns an uncorrelated `IN (subquery)` into a hashed semi-join, so this is mostly readability. It also makes it obvious that a small 12k-row hash is being built once, not per row. | Roughly neutral; I would not expect this alone to move the timing. |
| `SELECT *` → the columns the screen uses | `condition_note` is `text` and can be TOASTed; pulling it for every row means extra heap/TOAST reads and a fatter result. Listing columns also makes an index-only scan possible later if the columns are covered. | Gain proportional to note size; requires the screen to actually not need the note. |
| `LIMIT 200` (with an offset/keyset for paging) | A reporting screen does not render 4.2M rows. Bounding the result bounds the sort. | If the screen needs all rows exported, this moves to a streaming export path instead. |

If the sort has to cover the full result rather than a page, the `ORDER BY` stays and the sort is on the filtered set only, which after the index below is small.

### 2. Indexes

```sql
-- The one that matters.
CREATE INDEX CONCURRENTLY idx_checkouts_open_by_checked_out_at
    ON checkouts (checked_out_at)
    WHERE returned_at IS NULL;
```

Why this one earns its place: `returned_at IS NULL` is the most selective predicate by far. Of 4.2M historical rows, only the equipment currently out on loan is open, probably in the low thousands or tens of thousands, and that number is bounded by how much equipment exists, not by how long the system has been running. A **partial** index over just those rows is a few MB, stays in shared buffers, and the range condition on `checked_out_at` walks a tiny slice of it. `CONCURRENTLY` because a plain `CREATE INDEX` takes a `SHARE` lock that blocks writes on a 4.2M-row table for the duration of the build.

Why not a composite `(returned_at, checked_out_at)`: `IS NULL` is a poor leading column for a B-tree (all the open rows sit in one key value, and the index would still carry 4.2M entries for the returned rows the query never wants). The partial index encodes the same selectivity at a fraction of the size. Why not `(checked_out_at)` alone: it would be ~4.2M entries and the planner would still have to visit the heap to test `returned_at`, discarding almost every row it fetched.

Why not include `due_at` for the sort: the range predicate is on `checked_out_at` and the sort on `due_at`, so no single B-tree can satisfy both; the filtered set is small, so an in-memory sort of a few thousand rows costs under a millisecond. Adding `INCLUDE (due_at, employee_id)` would enable an index-only scan, but only pays off once `SELECT *` is gone and the visibility map is fresh; I would measure before adding it.

For the Django side, the same index is:

```python
models.Index(fields=["checked_out_at"], condition=Q(returned_at__isnull=True),
             name="idx_checkouts_open_by_checked_out_at")
```

added with `django.contrib.postgres.operations.AddIndexConcurrently` in a migration with `atomic = False`.

No index on `employees(is_active)`: 12,000 rows is a single-digit-millisecond sequential scan and it is hashed once. An index there would not change the plan in a measurable way.

### 3. What EXPLAIN (ANALYZE, BUFFERS) should show

**Before:**

```
Sort  (cost=... rows=...) (actual time=7900..7950 rows=3120 loops=1)
  Sort Key: c.due_at
  Sort Method: quicksort  Memory: ...
  Buffers: shared hit=... read=~380000
  ->  Hash Semi Join / Hash Join  (actual time=... rows=3120 loops=1)
        Hash Cond: (c.employee_id = employees.id)
        ->  Seq Scan on checkouts c  (actual time=0.05..7800 rows=3300 loops=1)
              Filter: ((returned_at IS NULL) AND (date(checked_out_at) >= '2026-01-01') AND (date(checked_out_at) <= '2026-06-30'))
              Rows Removed by Filter: 4196700
              Buffers: shared read=~380000
        ->  Hash  (rows=11800)
              ->  Seq Scan on employees  Filter: is_active
```

The tell-tales are `Seq Scan on checkouts`, `Rows Removed by Filter` in the millions, and `Buffers: shared read` in the hundreds of thousands of 8 KB pages (a 4.2M-row table with a `text` column is a few GB).

**After:**

```
Limit / Sort  (actual time=6..7 rows=200 loops=1)
  Buffers: shared hit=~900
  ->  Hash Join  (actual time=... rows=3120)
        ->  Index Scan using idx_checkouts_open_by_checked_out_at on checkouts c
              (actual time=0.03..4.5 rows=3300 loops=1)
              Index Cond: ((checked_out_at >= '2026-01-01 00:00:00+05:30') AND (checked_out_at < '2026-07-01 00:00:00+05:30'))
              Buffers: shared hit=~850
        ->  Hash  ... employees
```

(Or `Bitmap Index Scan` + `Bitmap Heap Scan` on the same index if the planner thinks the matching rows are scattered.)

**The specific line that says the fix worked** is the scan node on `checkouts` reading `Index Scan using idx_checkouts_open_by_checked_out_at` with an `Index Cond` on `checked_out_at`, instead of `Seq Scan ... Filter`. Immediately under it, `Buffers: shared hit` should be in the hundreds or low thousands instead of `shared read` in the hundreds of thousands. If the node still says `Seq Scan`, either the `DATE()` wrapper is still there, or the stats say the range matches too many rows, or the planner does not consider the partial index applicable (the `WHERE` in the query must imply the index predicate exactly).

### 4. What breaks first as the table grows

At 8,000 rows a day the table adds ~2.9M rows a year. With the partial index in place, this query does **not** get slower with growth, because the index only covers open rows and that set is bounded by inventory, not history. What degrades first is elsewhere:

1. **Planner statistics on `checked_out_at` go stale.** Default `autovacuum_analyze_scale_factor` is 10%, so on a 4.2M-row table `ANALYZE` runs every ~420k inserts, roughly every seven weeks. Between analyses the planner believes no rows exist past the last-seen maximum date, so a filter on "this month" is estimated at ~1 row, which can flip joins to nested loops that are catastrophic when the real count is thousands. Fix ahead of time: `ALTER TABLE checkouts SET (autovacuum_analyze_scale_factor = 0.01, autovacuum_vacuum_scale_factor = 0.02)`.
2. **Dead tuples from returns.** Every return is an `UPDATE` that sets `returned_at`, which is in the partial index predicate, so the update is not HOT. Each return leaves a dead tuple; vacuum has to keep up or the heap and the FK indexes bloat. Same fix as above plus monitoring `n_dead_tup` in `pg_stat_user_tables`.
3. **Every other query on `checkouts` that is not restricted to open rows** (history reports, "all check-outs in Q1", per-employee history over years) will seq-scan a growing table. For a time-ordered, append-mostly column a `BRIN` index on `checked_out_at` is nearly free (kilobytes) and prunes by block range: `CREATE INDEX CONCURRENTLY ... USING brin (checked_out_at)`.
4. **Operational weight**: backups, `VACUUM` runtime, and any future `ALTER TABLE` that rewrites the table all scale with size. Before it becomes a problem (I would say somewhere around 20–30M rows, but see 5), declare `checkouts` partitioned by range on `checked_out_at` (monthly or quarterly). Partition pruning makes date-bounded reports touch only the relevant partitions, and old partitions can be detached and archived instead of deleted row by row. Because this is a rewrite, it has to be planned as its own migration project with a dual-write window, so I would start it while the table is still comfortably small.

### 5. What I would measure first, and why I cannot be sure without it

**`null_frac` for `returned_at` in `pg_stats`** (or simply `SELECT count(*) FILTER (WHERE returned_at IS NULL), count(*) FROM checkouts`). Everything above assumes open rows are a small, bounded fraction of the table. If the business rarely records returns, or the table includes years of never-returned rows, the partial index is not small, the index scan visits a large slice of it, and the better choice becomes a plain B-tree on `(checked_out_at)` or a composite with `INCLUDE`. The plan the planner picks depends on that selectivity times cost parameters (`random_page_cost`, `effective_cache_size`) and on whether the table's pages are already in cache, none of which can be read off the schema. The second thing I would capture is the real `EXPLAIN (ANALYZE, BUFFERS)` of the current query as a baseline, so "8 seconds" is attributed to the scan and not to, say, network transfer of `SELECT *` with large notes.

---

## Part D — Production reasoning

### D1. Zero-downtime migration: non-nullable `location_id` FK on `checkouts`

Two deploys with a backfill between them. The rule: every schema step must work with both the code before it and after it, because the four instances roll one at a time.

**Deploy 1 — expand.** Migration with `atomic = False`:
1. `ALTER TABLE checkouts ADD COLUMN location_id bigint NULL;` — no default, no constraint: a catalogue-only change, milliseconds under a brief `ACCESS EXCLUSIVE` lock with no scan.
2. `ADD CONSTRAINT ... FOREIGN KEY (location_id) REFERENCES locations(id) NOT VALID;` — enforced for new writes, existing rows not scanned.
3. `CREATE INDEX CONCURRENTLY` on `location_id`.

Code: `ForeignKey(null=True)`, and every path that writes a `CheckOut` now sets `location_id`. In-flight old instances insert without the column; Django lists columns explicitly, so those inserts succeed with `NULL`, which the schema allows.

**Backfill** as a management command, not a migration: `UPDATE ... WHERE id BETWEEN a AND b AND location_id IS NULL` in batches of ~5,000 by primary key with a short sleep, watching replication lag. Short transactions mean row locks last milliseconds. Repeat until no nulls remain, including rows old instances wrote during deploy 1.

**Deploy 2 — contract**, only when all four instances run deploy-1 code and nulls are zero:
1. `VALIDATE CONSTRAINT` on the FK — scans, but under `SHARE UPDATE EXCLUSIVE`, which allows reads and writes.
2. `ADD CONSTRAINT chk CHECK (location_id IS NOT NULL) NOT VALID;` then `VALIDATE CONSTRAINT chk;` — same lock class.
3. `ALTER COLUMN location_id SET NOT NULL;` — PG 12+ uses the validated CHECK to skip the scan, so `ACCESS EXCLUSIVE` lasts milliseconds. Drop the CHECK.

Code: `null=False`. Deploy-1 code already always writes the column, so in-flight requests cannot violate NOT NULL. All DDL runs with `SET lock_timeout = '3s'` and retries, so a lock request queued behind a long report transaction fails fast instead of stalling every query behind it.

**What locks the table if you get it wrong:** the one-step version Django generates from `AddField(null=False)` with a one-off default: `ADD COLUMN location_id bigint NOT NULL DEFAULT 1 REFERENCES locations(id)`. It holds `ACCESS EXCLUSIVE` while validating the FK across 4.2M rows, so every read and write on `checkouts` queues for minutes. Runners-up: `CREATE INDEX` without `CONCURRENTLY` (`SHARE` lock blocks all writes) and `SET NOT NULL` without the pre-validated CHECK (full scan under `ACCESS EXCLUSIVE`).

### D2. Latency triage: `/reports/overdue/` went from fine to 25 s, no deploy in nine days

No code changed, so it is data, database state, or infrastructure. In order:

1. **Blast radius** — per-route p95 in APM/logs. Everything slow (including `/health/`) rules the query out and points at the DB host, pool or network; only this route points at its query or data.
2. **One slow trace** — is the time in pool wait, in SQL, or in Python? Pool wait means something is hoarding connections; Python time means the result set exploded; SQL time sends me to the database.
3. **`pg_stat_activity`** — long-running or `idle in transaction` sessions, anything with `wait_event_type = 'Lock'`, `pg_blocking_pids()`. A stuck backup, manual `VACUUM FULL`, hand-run migration or batch job explains sudden latency with no deploy.
4. **`EXPLAIN (ANALYZE, BUFFERS)`** of the report query against a saved baseline: plan flip (`Seq Scan` where there was an `Index Scan`) or the same plan with huge `shared read`. Then `pg_stat_user_tables` for `last_autoanalyze`, `n_dead_tup`, and `pg_index.indisvalid` in case an index was dropped or left invalid.
5. **The data** — `count(*)` of open overdue rows. 30 yesterday and 300,000 today (an import, a bad `due_at` update, returns not being recorded since a process died) means the workload changed, not the query. Pagination bounds the page but `COUNT(*)` still runs over the whole set.
6. **Host and non-deploy changes** — CPU, IO wait, disk full, replica lag if reading a replica, parameter changes, minor upgrade, new cron.

**Two most likely causes:**

- **Planner regression: stale statistics or vacuum behind.** Growth crossed a threshold and the planner now prefers a sequential scan, or dead tuples from returns and the notice task bloated the heap. Confirm: `EXPLAIN ANALYZE` shows `Seq Scan` with `Rows Removed by Filter` in the millions and `last_autoanalyze` weeks old. Fix now: `ANALYZE`; fix properly: per-table autovacuum thresholds plus the partial index.
- **Lock or resource contention from another process.** A long transaction (export, backup, manual data fix, or the notice task grinding through a large set) blocking or starving IO. Confirm: `pg_stat_activity` shows the report in `Lock` wait or a session running since early morning whose start matches a cron. Fix: end it, then make that job batched and lock-free.

### D3. CI/CD and safety on GitHub Actions

**Pull request** (this repo's `.github/workflows/ci.yml` is the core of it): Python 3.12, `ruff check` and format check, `makemigrations --check --dry-run` so a model change without a migration fails, `pytest` against a Postgres 16 service container with a coverage floor, `docker build`, `pip-audit`, and a migration linter (`django-migration-linter` or `squawk` on `sqlmigrate` output) that fails on `ADD COLUMN ... NOT NULL`, non-concurrent `CREATE INDEX`, or dropping a column. The job posts the `sqlmigrate` SQL of new migrations as a PR comment so the reviewer reads the actual DDL. Branch protection: green checks plus one review.

**Merge to `main`:** same suite, then build the image tagged with the SHA, push to the registry, deploy to staging automatically: `migrate`, roll the app, run a smoke job (`/health/`, `seed_demo_data`, one check-out and return, the overdue report, the Celery task once).

**Production gate:** a GitHub Environment with required reviewers, deployable only from a SHA that passed staging. The deploy job runs `migrate` **before** the new code goes live, with `lock_timeout` and `statement_timeout` set. That order is only safe because of a hard rule: every migration is compatible with the currently running code (expand/contract, as in D1). Additive changes ship first; a column is dropped in a later release once nothing references it. Then a rolling restart of the four instances behind health checks, then worker and beat. A failed health check halts the rollout.

**Rollback after the schema has migrated:** because migrations are N-1 compatible, rolling the *code* back to the previous image is always safe: old code ignores a new nullable column or index. That is the default rollback and takes one deploy. I do not run `migrate <app> <previous>` in production reflexively; reverse migrations can be lossy or take the same locks backwards. I reverse only an additive migration whose reverse is cheap, and only after the code is back. Contract steps (drop, rename) have no cheap rollback, so they wait for a further release, a fresh backup, and a feature flag that can switch the code path off without a deploy. For data damage rather than schema, the answer is point-in-time recovery, which is why WAL archiving is set up before any of this matters.
