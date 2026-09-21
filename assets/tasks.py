import logging

from celery import shared_task
from django.utils import timezone

from .models import OverdueNotice
from .queries import open_checkouts

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


@shared_task(name="assets.tasks.flag_overdue_checkouts")
def flag_overdue_checkouts() -> dict:
    """
    Create today's OverdueNotice for every open, overdue check-out.

    Idempotent by construction:
      * check-outs that already have a notice dated today are excluded up
        front, so a re-run inserts nothing new;
      * bulk_create(ignore_conflicts=True) leans on the
        (checkout, notice_date) unique constraint, so two workers running the
        task at the same moment cannot double-insert either.

    Works in fixed-size batches over a server-side cursor so a large overdue
    set does not get loaded into memory at once.
    """
    now = timezone.now()
    today = now.date()

    candidate_ids = (
        open_checkouts()
        .filter(due_at__lt=now)
        .exclude(notices__notice_date=today)
        .order_by("id")
        .values_list("id", flat=True)
    )

    created = 0
    scanned = 0
    batch: list[OverdueNotice] = []
    for checkout_id in candidate_ids.iterator(chunk_size=BATCH_SIZE):
        scanned += 1
        batch.append(OverdueNotice(checkout_id=checkout_id, notice_date=today))
        if len(batch) >= BATCH_SIZE:
            created += _flush(batch)
            batch = []
    if batch:
        created += _flush(batch)

    logger.info("flag_overdue_checkouts: date=%s scanned=%d created=%d", today, scanned, created)
    return {"notice_date": today.isoformat(), "scanned": scanned, "created": created}


def _flush(batch: list[OverdueNotice]) -> int:
    # With ignore_conflicts Postgres does not report how many rows it skipped,
    # so the count is of attempted inserts; conflicts are already filtered
    # out by the .exclude() above except in the concurrent-run case.
    OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)
    return len(batch)
