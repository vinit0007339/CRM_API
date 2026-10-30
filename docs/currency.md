# Currency in the CRM

Three currencies are supported: **INR, USD, CAD**
(`app/services/defaults.SUPPORTED_CURRENCIES`).

## The rule

**A pipeline has a currency. Every lead in it has the pipeline's currency.**

That single rule is what makes a board total, a column total and a stage
forecast mean anything: they are `$sum: "$amount"` with no currency in the
`$group` key, so they are only valid because every row being added shares one.

It is enforced in three places, and all three are needed:

| Door | What it does |
|---|---|
| `POST /crm/deals` | `currency=pipeline.currency` — the payload gets no vote |
| `POST /crm/deals/{id}/stage` | the currency moves with the pipeline |
| `serializers.DEAL_UPDATABLE` | `currency` is not updatable, so no PATCH, import or attribute write can set it |

`attribute_routing.SYSTEM_FIELDS["deal"]` omits `currency` for the same
reason — listed there, a `custom_fields: {"currency": "EUR"}` would route onto
the column and reintroduce a mixed pipeline through the back door.

⚠️ **A move re-labels; it never converts.** 5,000 stays 5,000 when a lead moves
from a USD pipeline to an INR one. There are no exchange rates in this service,
and inventing one would turn a visible inconsistency into an invisible wrong
number. The move is logged.

## Sums that span pipelines

The deals **list** has no pipeline filter by default, so its footer is the one
place a total can span currencies. It does not add them:

* `queries.list_totals_pipeline` groups by **stage AND currency**;
* `forecast.list_summary` returns `by_currency` beside the flat totals;
* `GET /crm/deals` carries it as `summary.by_currency`;
* the footer renders subtotals — "₹4.2L · $5,000" — whenever that list holds
  more than one entry, and the ordinary single figure when it does not.

The flat `total_value` is **kept and still correct for a single-currency
account**, which is most of them. It is additive, so an older client is exactly
as right as it was before. A bucket with a `null` currency is its own row and
renders without a symbol: a lead under a pipeline that never claimed a currency
is not evidence for any particular one.

⚠️ **NONE OF THIS CONVERTS.** Two subtotals side by side are two facts; one
number would be a claim no stored rate supports.

**Reports are always single-pipeline** — the screen falls back to the default
pipeline and refuses to load without one — so `pipeline.currency` labels them
correctly and they need no subtotals.

## Dormant fields

`company.open_deal_value` / `won_deal_value` are stored fields nothing writes.
They were removed from the company Summary rather than computed, because
computing them means summing a company's leads across pipelines — the same
cross-currency sum. They remain available as list COLUMNS, where they render
empty; if they are ever computed, they need `by_currency` treatment too.

## Retired codes

The picker once offered seven (EUR, GBP, AED, SGD, AUD as well) and did not
offer CAD. Validation applies to **writes only**:

* an existing pipeline keeps whatever it holds;
* the picker shows that value as a disabled "EUR — no longer offered" option,
  so the Select's value always matches an option and a later save cannot
  silently rewrite a money setting;
* `scripts/check_currency_usage.py` reports where they are.

Nothing rewrites an account's currency on its behalf.

## Scripts

    python3 scripts/check_currency_usage.py            # read-only report
    python3 scripts/backfill_deal_currency.py          # dry run
    python3 scripts/backfill_deal_currency.py --apply  # re-label leads

The backfill re-labels leads that disagree with their pipeline (a pre-phase-1
state). It changes the currency and never the amount, and calls out separately
the leads that carry an amount — those are the ones a person should look at.

## If conversion is ever added

The platform already has rates: `fx_rates` (USD-base, refreshed on a loop,
jump-guarded) maintained by `api/services/currency_service.py` in
`emailMarketingAPI`. Reaching them from here means a seventh read through
`app/foreign.py`, which that file requires be argued for first.

Two things to settle before writing any of it:

1. **Stamp the rate when a lead closes.** Otherwise won/lost history moves
   every time the rate does, and last quarter's number changes overnight.
2. **Keep the original amount and currency.** A converted figure is a display,
   never the record.
