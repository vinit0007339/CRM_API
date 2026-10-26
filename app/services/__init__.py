"""CRM pure-logic services.

Everything in this package is DB-free and unit-testable. Stage rules, card
positioning, forecasting, dedupe matching, record-level permissions and
association normalisation live here rather than in route handlers, because
this repo has no database test harness — pure modules are the only layer that
can be meaningfully covered by the existing pytest suite.
"""
