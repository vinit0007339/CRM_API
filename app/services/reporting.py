"""Pipeline reporting. Reads only — nothing here writes.

WHAT THIS ANSWERS, AND WHY IT IS NOT `forecast.py`. `forecast.py` answers
"what is the pipeline worth", weighting open deals by stage probability. These
functions answer the other three questions a pipeline owner asks and nothing
could: **where do deals die**, **how long do they sit**, and **who converts**.

⚠️ EVERY INPUT IS ALREADY BEING RECORDED. There is no new write anywhere in
this feature, and that is the reason it is cheap:

* `DealModel.stage_entered_at` is maintained on every stage change, close and
  reopen (`routes/deals.py` sets it in four places), so the CURRENT dwell time
  needs no history at all.
* `activity_recorder.stage_changed` writes `from_stage_id`, `from_stage_name`,
  `to_stage_id` and `to_stage_name` into `CrmActivityModel.metadata`, with an
  `occurred_at` — which is the full transition series for every deal.
* `crm_settings.lost_reasons` is populated per account and `closed` records
  `lost_reason_id` on the deal. Collected since the beginning and never read
  back until now.

PURE ON PURPOSE. These take plain lists of dicts and return plain dicts, so
they are testable without a database and the route layer owns all the querying.
`forecast.py` is written the same way for the same reason.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Activity type written by `activity_recorder.stage_changed`.
STAGE_CHANGED = "stage_changed"
#: Activity type written by `activity_recorder.reopened`.
REOPENED = "reopened"

#: ⚠️ BOTH, AND THE SECOND ONE IS NOT OPTIONAL. `reopen` moves a deal back
#: into an open stage but records `activity_type="reopened"`, NOT
#: `stage_changed` — so reading only `stage_changed` loses the re-entry
#: entirely: the stage a deal was reopened into never shows the entry, and
#: every spell measured from it starts at the wrong moment. The route must
#: query this whole set; `_gather` does.
MOVEMENT_TYPES = (STAGE_CHANGED, REOPENED)


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Read from a Beanie model or a dict, indifferently — `serializers._val`."""
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _meta(activity: Any) -> Dict[str, Any]:
    return _get(activity, "metadata", None) or {}


def _status(deal: Any, default: str = "") -> str:
    """The deal's status as a plain lowercase string.

    ⚠️ NOT `str(deal.status)`. `DealModel.status` is a `DealStatus` enum, and
    `str()` on it yields `"DealStatus.WON"`, which matches none of the three
    literals every comparison below uses — so win rate, conversion and lost
    reasons all silently reported zero. `routes/deals.py` and the `/pipeline`
    route already read it through `.value`; this is the same read, in the one
    place the aggregations share. Dicts (tests, aggregation output) pass
    through untouched.
    """
    value = _get(deal, "status", None)
    value = getattr(value, "value", value)
    return str(value).lower() if value is not None else default


# ── transitions ──────────────────────────────────────────────────────────────

def transitions_by_deal(activities: Iterable[Any]) -> Dict[str, List[Dict[str, Any]]]:
    """`{deal_id: [{from_id, to_id, at}, ...]}`, each list in time order.

    ⚠️ SORTED PER DEAL, NOT COUNTED FLAT, AND THAT IS NOT TIDINESS. A reopened
    deal re-enters a stage it has already been in — `reopen` writes its own
    activity and sets `stage_entered_at` again — so a deal can legitimately
    appear twice in "entered stage X". Counting transitions as a flat bag gets
    the right total and the wrong sequence, and every dwell calculation below
    depends on the sequence: the time a deal spent in a stage is the gap to its
    NEXT transition, which only exists if the list is ordered.

    ⚠️ A `reopened` ROW CARRIES NO `from_stage_id`, BY DESIGN —
    `activity_recorder.reopened` records only `to_stage_id`, because the stage
    it left was the Won or Lost stage and naming it as an origin would score a
    reopen as movement through the pipeline. So its `from_id` is None here,
    which is exactly what `conversion` needs: it counts as an ENTRY into the
    new stage and never as an advance out of anything.

    ⚠️ CLOSING IS NOT IN THIS SERIES AT ALL. `close` overwrites `stage_id`
    with the Won/Lost stage and records `activity_type="won"/"lost"` — no
    stage row. That is why `closing_stage()` below exists: after a close, the
    deal's own `stage_id` no longer says where it actually died.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for activity in activities:
        if _get(activity, "activity_type") not in MOVEMENT_TYPES:
            continue
        meta = _meta(activity)
        deal_id = str(_get(activity, "entity_id") or "")
        if not deal_id:
            continue
        out.setdefault(deal_id, []).append({
            "from_id": str(meta.get("from_stage_id") or "") or None,
            "to_id": str(meta.get("to_stage_id") or "") or None,
            "at": _get(activity, "occurred_at"),
        })
    for moves in out.values():
        moves.sort(key=lambda m: (m["at"] is None, m["at"]))
    return out



def _open_ids(stages: List[Dict[str, Any]]) -> set:
    """Stage ids whose `type` is `open` — i.e. not the Won or Lost column."""
    return {str(s["id"]) for s in stages if str(s.get("type") or "open") == "open"}


def closing_stage(deal: Any, path: List[Dict[str, Any]],
                  open_ids: set) -> Optional[str]:
    """The last OPEN stage the deal occupied. `None` if it never had one.

    ⚠️ NOT `deal.stage_id`, AND THIS IS THE WHOLE POINT OF THE REPORT.
    `routes/deals.py::close_deal` sets `deal.stage_id = <the Won or Lost
    stage>`. So after a close the deal's own stage says "Lost", and attributing
    losses to the stage a deal is resting in puts EVERY loss on the Lost
    column — a report that says deals die in the Lost stage, which is true and
    worthless. "Where do deals die" means the stage they were in when someone
    gave up on them.

    That stage is recoverable, because close does not touch the transition
    series: it is the last `to_id` in `path` that is an open stage, or the
    deal's creation stage when it never moved. For an OPEN deal the resting
    stage is already correct and is returned unchanged.
    """
    resting = str(_get(deal, "stage_id") or "")
    if resting in open_ids:
        return resting
    for move in reversed(path):
        if move["to_id"] and move["to_id"] in open_ids:
            return move["to_id"]
    for move in path:
        if move["from_id"] and move["from_id"] in open_ids:
            return move["from_id"]
    return None


# ── conversion ───────────────────────────────────────────────────────────────

def conversion(stages: List[Dict[str, Any]], deals: List[Any],
               activities: Iterable[Any]
               ) -> Tuple[List[Dict[str, Any]], int]:
    """`(rows, unattributed)` — per stage: entries, forward exits, and the rate.

    ⚠️ A DEAL THAT NEVER MOVED HAS NO `stage_changed` ROW, and counting only
    transitions silently omits every one of them. That is not an edge case: it
    is the stage deals are created in, which is usually where the most of them
    sit and exactly what this report exists to find. Each deal's CREATION stage
    is therefore counted as an entry in its own right, taken from the first
    transition's `from_id` when there is one and from the deal's current
    `stage_id` when there is not.

    `advanced` counts departures to a LATER stage by `order`, so a deal pushed
    backwards is not scored as progress.

    `lost_here` and `won_here` are attributed through `closing_stage()`, NOT
    through the deal's own `stage_id` — read that function before changing
    either, because `stage_id` has been overwritten by the time a deal is
    closed and the obvious reading puts every loss on the Lost column.

    `unattributed` is the count of closed deals whose death stage is not in the
    data: created and closed without ever moving, so there is no transition to
    read and `created` records no stage. Returned rather than folded into the
    first stage, because the first stage is the most plausible guess and a
    plausible guess in a drop-off report is the one error nobody checks.
    """
    order = {str(s["id"]): int(s.get("order") or 0) for s in stages}
    open_ids = _open_ids(stages)
    moves = transitions_by_deal(activities)

    entered: Dict[str, int] = {str(s["id"]): 0 for s in stages}
    advanced: Dict[str, int] = {str(s["id"]): 0 for s in stages}
    lost_here: Dict[str, int] = {str(s["id"]): 0 for s in stages}
    won_from: Dict[str, int] = {str(s["id"]): 0 for s in stages}
    unattributed = 0

    for deal in deals:
        deal_id = str(_get(deal, "id") or _get(deal, "_id") or "")
        path = moves.get(deal_id, [])
        # The CREATION stage: the origin of the first transition when there is
        # one, and otherwise wherever the deal still is.
        #
        # ⚠️ `closing_stage`, NOT `stage_id`, FOR THE FALLBACK. For an open
        # deal the two are the same. For a deal created and closed without
        # moving, `stage_id` is the Won or Lost column — so the naive read
        # recorded an ENTRY into the Lost stage that no transition supports,
        # and the first stage lost the very deals that never left it. When the
        # creation stage is unrecoverable the deal is counted in
        # `unattributed` below instead of being credited to a guess.
        origin = (path[0]["from_id"] if path and path[0]["from_id"]
                  else closing_stage(deal, path, open_ids))
        if origin in entered:
            entered[origin] += 1
        for move in path:
            src, dst = move["from_id"], move["to_id"]
            if dst in entered:
                entered[dst] += 1
            if src in advanced and dst in order and src in order \
                    and order[dst] > order[src]:
                advanced[src] += 1
        status = _status(deal)
        died = closing_stage(deal, path, open_ids)
        if status in ("won", "lost") and died is None:
            # A deal created and closed without ever moving. `close` overwrote
            # `stage_id` and `created` records no stage, so the stage it died
            # in is not in the data at all. Counted here rather than guessed —
            # see the `unattributed` note on the return value.
            unattributed += 1
        elif died is not None:
            if status == "lost" and died in lost_here:
                lost_here[died] += 1
            if status == "won" and died in won_from:
                won_from[died] += 1

    rows = []
    for stage in sorted(stages, key=lambda s: int(s.get("order") or 0)):
        sid = str(stage["id"])
        n = entered[sid]
        rows.append({
            "stage_id": sid,
            "stage_name": stage.get("name"),
            "order": int(stage.get("order") or 0),
            # So a `conversion_rate: null` is readable: `open` means no deal
            # reached this stage, `won`/`lost` means the rate does not apply.
            "stage_type": str(stage.get("type") or "open"),
            "entered": n,
            "advanced": advanced[sid],
            "lost_here": lost_here[sid],
            "won_here": won_from[sid],
            # ⚠️ None, NOT 0.0, IN TWO CASES, AND FOR THE SAME REASON: 0% reads
            # as "every deal died here", so it must never stand in for "this
            # question does not apply".
            #   · nothing entered — a stage no deal reached has no rate;
            #   · the stage is the Won or Lost column — nothing can advance OUT
            #     of a terminal stage, so its rate would be a guaranteed 0%
            #     printed next to the deals that actually closed there.
            "conversion_rate": (round(advanced[sid] / n * 100, 1)
                                if n and sid in open_ids else None),
        })
    return rows, unattributed


# ── time in stage ────────────────────────────────────────────────────────────

def time_in_stage(stages: List[Dict[str, Any]], deals: List[Any],
                  activities: Iterable[Any], now: datetime
                  ) -> List[Dict[str, Any]]:
    """Per stage: average and median days spent, over completed spells + current.

    Two sources, because neither alone is the answer:

    * a COMPLETED spell is the gap between entering a stage and leaving it,
      read from consecutive transitions;
    * the CURRENT spell is `now - deal.stage_entered_at`, which
      `routes/deals.py` maintains on every change, close and reopen.

    ⚠️ THE CURRENT SPELL IS WHY A NEVER-MOVED DEAL IS NOT INVISIBLE HERE.
    `stage_entered_at` defaults to creation, so a deal that has sat in the
    first stage since January contributes its full dwell without a single
    activity row. Reporting only completed spells would make the slowest stage
    in the pipeline look like the fastest, because the deals stuck in it have
    not left to be measured.

    ⚠️ A CLOSE ENDS A SPELL; IT DOES NOT START ONE IN THE WON COLUMN. See the
    comment on that branch below — it is the one place in this module where
    `stage_entered_at` and `stage_id` mean something other than what they say.

    ⚠️ FRACTIONAL DAYS HERE, WHERE THE BOARD USES WHOLE ONES, AND THE SPLIT IS
    DELIBERATE. `stage_rules.days_in_stage` floors to `int` — correct for "is
    this card rotting", where a part-day is not a day. For an AVERAGE it is
    not: every spell under 24 hours collapses to 0, so a stage deals pass
    through in an afternoon reports a median of 0.0 days and reads as broken
    rather than fast.
    What IS shared is `stage_rules._as_utc`, and that is the part worth sharing:
    the Motor client is built without `tz_aware=True`, so PyMongo hands back
    NAIVE datetimes while every timestamp from a route is aware. Subtracting
    one from the other raises. That rule has one home and this is not a second
    copy of it.
    """
    from app.services.stage_rules import _as_utc

    def _days(start, end) -> Optional[float]:
        if start is None or end is None:
            return None
        return max(0.0, (_as_utc(end) - _as_utc(start)).total_seconds() / 86400)

    spells: Dict[str, List[float]] = {str(s["id"]): [] for s in stages}
    moves = transitions_by_deal(activities)
    open_ids = _open_ids(stages)

    for deal in deals:
        deal_id = str(_get(deal, "id") or _get(deal, "_id") or "")
        path = moves.get(deal_id, [])
        for earlier, later in zip(path, path[1:]):
            sid = earlier["to_id"]
            if sid in spells and earlier["at"] and later["at"]:
                spells[sid].append(_days(earlier["at"], later["at"]))

        # ── the open spell at the end of the series
        #
        # ⚠️ TWO DIFFERENT SPELLS, AND USING ONE FORMULA FOR BOTH IS WRONG IN
        # BOTH DIRECTIONS.
        #
        # For an OPEN deal it is `now - stage_entered_at` in its current
        # stage — the never-moved deal's whole dwell, and the reason it is not
        # invisible here.
        #
        # For a CLOSED deal `stage_entered_at` is the moment of the CLOSE and
        # `stage_id` is the Won or Lost column, because `close_deal` sets both.
        # Applying the open formula therefore reported "days since this deal
        # was won" as dwell time in the Won column — a number that grows
        # forever and belongs to no stage — while the spell that actually
        # matters, the time the deal sat in its last open stage before someone
        # closed it, was dropped entirely: `zip(path, path[1:])` never closes
        # out the final entry. So the close ENDS the last open spell.
        if _status(deal, "open") == "open":
            resting = str(_get(deal, "stage_id") or "")
            entered_at = _get(deal, "stage_entered_at")
            if resting in spells and entered_at:
                spells[resting].append(_days(entered_at, now))
        else:
            died = closing_stage(deal, path, open_ids)
            # `stage_entered_at` is the close moment; `actual_close_date` is
            # the user-supplied one and may be backdated, so prefer it and
            # fall back rather than reaching for `now`, which would credit the
            # stage with every day since.
            closed_at = (_get(deal, "actual_close_date")
                         or _get(deal, "stage_entered_at"))
            entered_at = next(
                (m["at"] for m in reversed(path) if m["to_id"] == died),
                _get(deal, "created_at"))
            if died in spells and entered_at and closed_at:
                spells[died].append(_days(entered_at, closed_at))

    rows = []
    for stage in sorted(stages, key=lambda s: int(s.get("order") or 0)):
        sid = str(stage["id"])
        values = [v for v in spells[sid] if v is not None]
        rows.append({
            "stage_id": sid,
            "stage_name": stage.get("name"),
            "order": int(stage.get("order") or 0),
            "samples": len(values),
            "avg_days": round(statistics.fmean(values), 1) if values else None,
            # Median as well as mean, because one deal parked for two years
            # drags an average into uselessness and the median is what a
            # reader actually wants from "how long does this stage take".
            "median_days": round(statistics.median(values), 1) if values else None,
            "max_days": round(max(values), 1) if values else None,
        })
    return rows


# ── win rate ─────────────────────────────────────────────────────────────────

def win_rate_by_owner(deals: List[Any]) -> List[Dict[str, Any]]:
    """Per owner: won, lost, open, and the win rate over CLOSED deals only.

    ⚠️ GROUPED ON `owner_id`, DISPLAYED WITH THE NAME, AND THAT ORDER MATTERS.
    `CrmActivityModel.actor_name` is a deliberate snapshot — the model says it
    "survives user deletion" — so grouping on the name splits one person into
    two rows the moment they are renamed, and merges two people who happen to
    share a name. The id is the identity; the name is a label.
    ⚠️ The rate is won / (won + lost), NOT won / all. Open deals have not been
    decided, and counting them as losses makes a healthy pipeline look like a
    failing one — the number would fall every time somebody added a deal.
    """
    buckets: Dict[Optional[str], Dict[str, Any]] = {}
    for deal in deals:
        owner = _get(deal, "owner_id")
        key = str(owner) if owner else None
        bucket = buckets.setdefault(key, {
            "owner_id": key, "won": 0, "lost": 0, "open": 0,
            "won_value": 0.0,
        })
        status = _status(deal, "open") or "open"
        if status in ("won", "lost", "open"):
            bucket[status] += 1
        if status == "won":
            bucket["won_value"] += float(_get(deal, "amount") or 0)

    rows = []
    for bucket in buckets.values():
        closed = bucket["won"] + bucket["lost"]
        rows.append({**bucket,
                     "closed": closed,
                     "win_rate": (round(bucket["won"] / closed * 100, 1)
                                  if closed else None)})
    rows.sort(key=lambda r: (-r["won"], -r["closed"]))
    return rows


# ── lost reasons ─────────────────────────────────────────────────────────────

def lost_reasons(deals: List[Any],
                 configured: List[Any]) -> Tuple[List[Dict[str, Any]], int]:
    """Per reason: how many lost deals and how much value. Plus the unattributed.

    ⚠️ EVERY CONFIGURED REASON APPEARS, INCLUDING THE ZEROS. A reason nobody
    has used is a finding — it usually means the dialog's list is wrong — and
    omitting empty rows hides it. Inactive reasons appear only when something
    was lost to them, because an account that retires a reason still has the
    history.

    The second return value is lost deals with NO reason, which is the number
    that tells you whether this report can be trusted at all.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for reason in configured:
        rid = str(_get(reason, "id") or "")
        if rid:
            by_id[rid] = {"reason_id": rid, "label": _get(reason, "label"),
                          "is_active": bool(_get(reason, "is_active", True)),
                          "deals": 0, "value": 0.0}

    unattributed = 0
    for deal in deals:
        if _status(deal) != "lost":
            continue
        rid = _get(deal, "lost_reason_id")
        key = str(rid) if rid else ""
        if not key:
            unattributed += 1
            continue
        row = by_id.setdefault(key, {
            "reason_id": key, "label": None, "is_active": False,
            "deals": 0, "value": 0.0,
        })
        row["deals"] += 1
        row["value"] += float(_get(deal, "amount") or 0)

    rows = [r for r in by_id.values() if r["deals"] or r["is_active"]]
    rows.sort(key=lambda r: (-r["deals"], (r["label"] or "").lower()))
    return rows, unattributed
