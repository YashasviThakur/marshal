"""Measure: read an experiment's raw events from the warehouse and reduce them to per-variant statistics.

Marshal runs its own SQL, shown with `--sql`, rather than waiting on Flagsmith's results endpoint (which can be
refreshed at most every 5 minutes). Two attribution rules:

  quarantine  Flagsmith's own rule: an identity seen on more than one variant is excluded. Used to prove
              Marshal's numbers match Flagsmith's scorecard exactly.
  first       Each identity counts for the variant it was FIRST exposed to (intention to treat). Moving traffic
              mid-experiment puts some identities on two variants; quarantining them would bias the result
              towards whoever was never moved, so Marshal decides on this one.

Queries follow Flagsmith's Postgres dialect (results_query.py) and work on the "Flagsmith database"
warehouse table or a Postgres warehouse's `events` table.
"""
from dataclasses import dataclass
from datetime import datetime

from .stats import VariantStats

EXPOSURE = "$flag_exposure"
FLAGSMITH_DATABASE_TABLE = "experimentation_sdkexperimentationevent"
# Flagsmith's float_or_zero: numeric strings cast, anything else (including inf/nan) reads as 0.
_NUMERIC = r"^[-+]?([0-9]{1,30}([.][0-9]{0,30})?|[.][0-9]{1,30})([eE][-+]?[0-9]{1,2})?$"


@dataclass(frozen=True)
class Metric:
    id: int
    event: str
    aggregation: str  # occurrence | count | sum | mean
    lower_is_better: bool = False


def exposures_cte(table: str, attribution: str, trait: str | None = None) -> str:
    value = "coalesce(value, '')"
    if attribution == "quarantine":
        variant = f"CASE WHEN count(DISTINCT {value}) > 1 THEN '' ELSE min({value}) END"
        quarantined = f"(count(DISTINCT {value}) > 1)::int"
    elif attribution == "first":
        variant = f"(array_agg({value} ORDER BY timestamp, id))[1]"
        quarantined = "0"
    else:
        raise ValueError(f"unknown attribution {attribution!r}")
    # the trait value the identity had at its first exposure; '' when the SDK sent none
    segment = "coalesce((array_agg(traits::jsonb ->> %(trait)s ORDER BY timestamp, id))[1], '')" if trait else "''"
    return f"""WITH exposures AS (
    SELECT identifier, {variant} AS variant, {quarantined} AS quarantined, min(timestamp) AS first_exposure,
        {segment} AS segment
    FROM {table}
    WHERE environment_key = %(environment_key)s AND event = %(exposure_event)s AND feature_name = %(feature_name)s
        AND timestamp >= %(window_start)s AND timestamp < %(window_end)s
    GROUP BY identifier
)"""


def unit_select(i: int, m: Metric) -> str:
    cond = f"m.event = %(metric_{i}_event)s AND m.timestamp >= e.first_exposure"
    count = f"count(*) FILTER (WHERE {cond})"
    value = f"CASE WHEN m.value ~ '{_NUMERIC}' THEN (m.value)::double precision ELSE 0 END"
    return {"occurrence": f"({count} > 0)::int",
            "count": count,
            "sum": f"coalesce(sum({value}) FILTER (WHERE {cond}), 0)",
            "mean": f"coalesce(avg({value}) FILTER (WHERE {cond}), 0)"}[m.aggregation] + f" AS m{i}"


def results_sql(metrics: list[Metric], table: str = FLAGSMITH_DATABASE_TABLE, attribution: str = "first",
                trait: str | None = None) -> str:
    units = ",\n        ".join(unit_select(i, m) for i, m in enumerate(metrics))
    outer = ",\n    ".join(f"sum(m{i}) AS m{i}_sum, sum(m{i} * m{i}) AS m{i}_sum_squares" for i in range(len(metrics)))
    return exposures_cte(table, attribution, trait) + f""",
unit_values AS (
    SELECT e.variant AS variant, e.segment AS segment,
        {units}
    FROM exposures AS e
    LEFT JOIN {table} AS m
        ON m.identifier = e.identifier AND m.environment_key = %(environment_key)s
        AND m.event = ANY(%(metric_events)s)
        AND m.timestamp >= %(window_start)s AND m.timestamp < %(window_end)s
    WHERE e.quarantined = 0
    GROUP BY e.identifier, e.variant, e.segment
)
SELECT variant, segment, count(*) AS n,
    {outer}
FROM unit_values
GROUP BY variant, segment
ORDER BY variant, segment"""


def params(env_key: str, feature: str, start: datetime, end: datetime, metrics: list[Metric],
           trait: str | None = None) -> dict:
    p = {"trait": trait, "environment_key": env_key, "exposure_event": EXPOSURE, "feature_name": feature,
         "window_start": start, "window_end": end, "metric_events": [m.event for m in metrics]}
    p.update({f"metric_{i}_event": m.event for i, m in enumerate(metrics)})
    return p


def read_stats(conn, env_key: str, feature: str, start: datetime, end: datetime, metrics: list[Metric],
               table: str = FLAGSMITH_DATABASE_TABLE, attribution: str = "first", trait: str | None = None):
    """-> ({variant: n}, {metric id: {variant: VariantStats}}), or with `trait` the same keyed by (variant, segment)
    after an outer {segment: ...} level: ({segment: {variant: n}}, {metric id: {segment: {variant: VariantStats}}})."""
    with conn.cursor() as cur:
        cur.execute(results_sql(metrics, table, attribution, trait), params(env_key, feature, start, end, metrics, trait))
        rows = cur.fetchall()
    if trait:
        counts, stats = {}, {m.id: {} for m in metrics}
        for r in rows:
            counts.setdefault(r[1], {})[r[0]] = int(r[2])
            for i, m in enumerate(metrics):
                stats[m.id].setdefault(r[1], {})[r[0]] = VariantStats(int(r[2]), float(r[3 + 2 * i]), float(r[4 + 2 * i]))
        return counts, stats
    counts = {r[0]: int(r[2]) for r in rows}
    stats = {m.id: {r[0]: VariantStats(int(r[2]), float(r[3 + 2 * i]), float(r[4 + 2 * i])) for r in rows}
             for i, m in enumerate(metrics)}
    return counts, stats
