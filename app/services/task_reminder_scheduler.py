"""TaskReminderScheduler — sends task reminders. CRM-227.

⚠️ **R8, and the answer to it.**

Seven schedulers in this codebase assume they are the only process running.
Each one loops, reads a batch of work, does it, and sleeps. Run two copies and
every piece of work happens twice, because nothing in the loop records that it
was taken. That is a known, accepted risk for the existing seven — this one
does not add an eighth.

The mechanism is one conditional update per reminder::

    update_one({"_id": task, "reminder.sent_at": None},
               {"$set": {"reminder.sent_at": now}})

Mongo applies a single-document update atomically. Two workers that both see
the same due reminder both issue this; exactly one gets ``modified_count == 1``
and sends, the other gets ``0`` and moves on. No lock, no leader election, no
queue.

**Is this safe under multiple workers? Yes — for double-sending, which is the
failure that matters here.** Two things it deliberately does not solve:

* If the send fails *after* the claim, the reminder is not retried. It is
  marked sent and nobody is told. Losing a reminder is a smaller harm than
  sending it twice — and a retry would need the claim to be released, which
  reintroduces the race it exists to close.
* Sends are not transactional with the claim. There is no way to make them so
  against a mail provider.

THE SEND ITSELF IS A NO-OP TODAY. ``_deliver`` logs and returns; in-app
notification and email delivery are wired in when the notification service
lands (Q-open). The claim, the sweep and the grace window are all live, so the
day delivery is added there is nothing left to get wrong about *when*.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from app.config import logger as _logger
from app.crud import CrmTaskCrud
from app.models.task import CrmTaskModel

from app.services import task_rules as tr


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskReminderScheduler:
    """Periodic sweep: claim each due reminder, then deliver it."""

    def __init__(
        self,
        *,
        task_crud: CrmTaskCrud,
        interval_seconds: int = 60,
        batch_size: int = 200,
        grace_minutes: int = 60,
    ):
        self.task_crud = task_crud
        # A minute is the resolution a reminder needs; anything finer is a
        # scan per second for a feature measured in "minutes before".
        self.interval_seconds = max(30, int(interval_seconds))
        self.batch_size = batch_size
        # How late a reminder may be and still be worth sending. Past this, the
        # scheduler was down long enough that the meeting has happened, and a
        # burst of reminders for things already missed is worse than silence.
        self.grace_minutes = grace_minutes
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self):
        self._running = True
        _logger.info(
            f"TaskReminderScheduler: started (interval={self.interval_seconds}s, "
            f"grace={self.grace_minutes}m)")
        try:
            while self._running:
                try:
                    await self.tick()
                except Exception as ex:  # pylint: disable=broad-except
                    # One bad tick must not end the loop; the next sweep picks
                    # up whatever this one dropped, because nothing was claimed.
                    _logger.exception(
                        f"TaskReminderScheduler: tick error: {ex}")
                await asyncio.sleep(self.interval_seconds)
        except asyncio.CancelledError:
            _logger.info("TaskReminderScheduler: cancelled")
        finally:
            self._running = False

    async def stop(self):
        self._running = False

    # ── one sweep ────────────────────────────────────────────────────────────
    async def tick(self, now: Optional[datetime] = None) -> int:
        """Claim and deliver every due reminder. Returns how many were SENT.

        Returns the sent count, not the found count: under two workers those
        differ, and the number worth logging is the one that reached a person.
        """
        now = now or _utcnow()
        due = await self.task_crud.due_reminders(
            now=now, grace_minutes=self.grace_minutes, limit=self.batch_size)
        if not due:
            return 0

        sent = 0
        for task in due:
            # CLAIM FIRST, THEN SEND. The other order — send, then mark — sends
            # twice whenever two workers overlap, which is the entire failure
            # this class is written to avoid.
            if not await self.task_crud.claim_reminder(task.id, now):
                continue
            try:
                await self._deliver(task)
                sent += 1
            except Exception as ex:  # pylint: disable=broad-except
                # The claim stands. See the module docstring: an unsent
                # reminder is a smaller harm than a doubled one, and releasing
                # the claim to retry would reopen the race.
                _logger.exception(
                    f"TaskReminderScheduler: delivery failed for {task.id}: {ex}")

        if sent:
            _logger.info(f"TaskReminderScheduler: sent {sent} reminder(s)")
        return sent

    async def _deliver(self, task: CrmTaskModel) -> None:
        """Placeholder delivery. See the module docstring.

        Kept as its own method so the day a notification service exists this is
        the only thing that changes — the timing, the claim and the grace
        window are already settled and tested.
        """
        channels = list(getattr(task.reminder, "channels", None) or ["in_app"])
        _logger.info(
            f"TaskReminderScheduler: reminder for task {task.id} "
            f"('{task.title}') due {tr.as_utc(task.due_at)} "
            f"→ {task.assigned_to_id} via {','.join(channels)}")
