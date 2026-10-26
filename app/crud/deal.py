"""Deal CRUD + the Kanban board aggregation. CRM-022.

``board_summary`` is the query behind NFR1 (board p95 < 500 ms for 1,000 deals
across 10 stages). It is two round trips, not N+1:

1. one ``$group`` aggregation for every column's count and sum;
2. one ``find`` per stage for that column's first page of cards.

Step 2 looks like N queries and is — but N is the stage count (3-20, typically
7), each one an indexed range read of ~25 documents on
``(created_by, pipeline_id, stage_id, position)``. The alternative,
``$setWindowFields`` ranking in a single pipeline, is measurably slower on
small N and much harder to page. Revisit if a board ever needs >20 columns.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import logger as _logger
from app.models.deal import DealModel

from app.services import forecast, queries as qb

from .base import CrmCrudBase


class DealCrud(CrmCrudBase):
    model = DealModel

    async def list(self, *, account_id: Any, sort_by: Optional[str] = None,
                   sort_dir: str = "desc", skip: int = 0, limit: int = 50,
                   **filters) -> List[DealModel]:
        return await self.list_by(
            qb.deal_filter(account_id=self.oid(account_id), **filters),
            sort=qb.deal_sort(sort_by, sort_dir), skip=skip, limit=limit,
        )

    async def count(self, *, account_id: Any, **filters) -> int:
        return await self.count_by(
            qb.deal_filter(account_id=self.oid(account_id), **filters)
        )

    async def list_totals(self, *, account_id: Any, **filters) -> list:
        """Per-stage sums over the deals LIST's filter. E24.

        ⚠️ THE SAME ``**filters`` THE LIST AND COUNT TAKE, so the footer cannot
        add up a different set from the rows above it — the one failure mode
        that makes a total worse than no total. One aggregation, not one query
        per stage.

        Returns ``[]`` on failure rather than raising: a missing footer is a
        missing footer, and taking the whole list down because a sum could not
        be computed would be the wrong trade.
        """
        if self.collection is None:
            return []
        pipeline = qb.list_totals_pipeline(
            account_id=self.oid(account_id), **filters)
        try:
            rows = await self.collection.aggregate(pipeline).to_list(length=None)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return []
        return [
            {
                "stage_id": r.get("_id"),
                "deal_count": int(r.get("deal_count") or 0),
                "total_value": float(r.get("total_value") or 0.0),
                "weighted_overridden": float(r.get("weighted_overridden") or 0.0),
                "amount_without_override": float(
                    r.get("amount_without_override") or 0.0),
            }
            for r in rows
        ]

    # ── board ────────────────────────────────────────────────────────────────
    async def stage_totals(self, *, account_id: Any, pipeline_id: Any,
                           **filters) -> Dict[Any, Dict[str, Any]]:
        """``{stage_id: {deal_count, total_value}}`` in one aggregation."""
        if self.collection is None:
            return {}
        pipeline = qb.board_totals_pipeline(
            account_id=self.oid(account_id),
            pipeline_id=self.oid(pipeline_id),
            **filters,
        )
        try:
            rows = await self.collection.aggregate(pipeline).to_list(length=None)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return {}
        return {
            r["_id"]: {
                "deal_count": int(r.get("deal_count") or 0),
                "total_value": float(r.get("total_value") or 0.0),
                # Split sums — see queries.board_totals_pipeline.
                "weighted_overridden": float(r.get("weighted_overridden") or 0.0),
                "amount_without_override": float(r.get("amount_without_override") or 0.0),
            }
            for r in rows
        }

    async def stage_cards(self, *, account_id: Any, pipeline_id: Any, stage_id: Any,
                          skip: int = 0, limit: int = 25, **filters) -> List[DealModel]:
        """One column's cards, in ``position`` order."""
        return await self.list_by(
            qb.stage_cards_filter(
                account_id=self.oid(account_id),
                pipeline_id=self.oid(pipeline_id),
                stage_id=self.oid(stage_id),
                **filters,
            ),
            sort=qb.board_sort(), skip=skip, limit=limit,
        )

    async def board_summary(self, *, account_id: Any, pipeline: Any,
                            cards_per_stage: int = 25, **filters) -> Dict[str, Any]:
        """Everything ``GET /crm/pipelines/{id}/board`` needs, assembled.

        Weighted values are computed by ``forecast.py`` from the aggregated
        sums, so the column headers and the board header can never disagree.
        """
        stages = [s.model_dump() if hasattr(s, "model_dump") else dict(s)
                  for s in (pipeline.stages or [])]
        totals = await self.stage_totals(
            account_id=account_id, pipeline_id=pipeline.id, **filters
        )

        out_stages: List[Dict[str, Any]] = []
        for stage in stages:
            sid = stage.get("id")
            agg = totals.get(sid) or {
                "deal_count": 0, "total_value": 0.0,
                "weighted_overridden": 0.0, "amount_without_override": 0.0,
            }
            cards = await self.stage_cards(
                account_id=account_id, pipeline_id=pipeline.id, stage_id=sid,
                limit=cards_per_stage, **filters,
            )
            weighted = forecast.stage_weighted_from_aggregate(stage, agg)
            out_stages.append({
                "stage": stage,
                "deal_count": agg["deal_count"],
                "total_value": round(agg["total_value"], 2),
                "weighted_value": weighted,
                "has_more": agg["deal_count"] > len(cards),
                "deals": cards,
            })

        return {
            "stages": out_stages,
            "totals": forecast.board_totals(out_stages),
        }

    async def column_positions(self, *, account_id: Any, pipeline_id: Any,
                               stage_id: Any) -> List[float]:
        """Every ``position`` in a column, ascending — input to CRM-037."""
        if self.collection is None:
            return []
        f = qb.neighbours_filter(
            account_id=self.oid(account_id),
            pipeline_id=self.oid(pipeline_id),
            stage_id=self.oid(stage_id),
        )
        try:
            cursor = self.collection.find(f, {"position": 1}).sort("position", 1)
            return [float(d.get("position") or 0.0)
                    for d in await cursor.to_list(length=None)]
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return []

    async def renumber_column(self, *, account_id: Any, pipeline_id: Any,
                              stage_id: Any, ordered_ids: List[Any]) -> int:
        """Re-space a column whose fractional gaps are exhausted (CRM-015)."""
        from app.services import position as pos

        if self.collection is None or not ordered_ids:
            return 0
        acct = self.oid(account_id)
        positions = pos.renormalise(len(ordered_ids))
        updated = 0
        try:
            for did, new_pos in zip(ordered_ids, positions):
                res = await self.collection.update_one(
                    {"_id": self.oid(did), "created_by": acct},
                    {"$set": {"position": new_pos}},
                )
                updated += int(getattr(res, "modified_count", 0) or 0)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
        return updated
