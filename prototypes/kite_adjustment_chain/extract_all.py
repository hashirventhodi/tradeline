"""PROTOTYPE (#62), read-only: dump every compared stretch of every Instrument, with each
close's current Kite Parity outcome, for the Kite's Adjustment replay. Reuses parity's own
pipeline. Never writes; the connection is read-only. Run:
    uv run python prototypes/kite_adjustment_chain/extract_all.py <out.pkl>
"""
import bisect, pickle, sys
from collections import defaultdict
from datetime import date
from decimal import Decimal

from tradeline import parity as P, queries
from tradeline.config import load_config, default_config_path
from tradeline.db import app_connection
from tradeline.environment import Environment
from tradeline.secrets import DATABASE_URL, EnvSecrets

OUT = sys.argv[1]
start, end = date(2016, 10, 21), date(2026, 10, 20)
config = load_config(default_config_path())
secrets = EnvSecrets.for_environment(Environment.current())
with app_connection(secrets.require(DATABASE_URL)) as conn:
    conn.execute("SET default_transaction_read_only = on"); conn.commit()
    rows = [r for r in P._rows(conn, start, end, None) if not r.pre_listing]
    tol = Decimal(str(config.parity.close_tolerance))
    rules = P._rules(conn, config.parity, rows)
    outcome = {}
    for u in P._close_mismatches(conn, rows, tol):
        d = P._classify(u, rules)
        outcome[(u.row.instrument, u.row.series, u.row.trade_date)] = d.reason.value if d.reason else "unexplained"
    missing_unexpl = sum(1 for r in rows if not r.has_candle
                         and P._classify(P._Unclassified("missing", r), rules).reason is None)
    extra_unexpl = sum(1 for r in rows if not r.has_eod
                       and P._classify(P._Unclassified("extra", r), rules).reason is None)
    insts = sorted({r.instrument for r in rows})
    actions = {i: [(a.ex_date, a.kind, a.subject, a.factor) for a in queries.corporate_actions(conn, i)] for i in insts}
    ex = {i: sorted({a[0] for a in actions[i]}) for i in insts}
    stretches = defaultdict(list)
    for r in rows:
        if r.candle_close is not None and r.eod_close:
            k = (r.instrument, r.series, bisect.bisect_right(ex[r.instrument], r.trade_date))
            stretches[k].append((r.trade_date, r.symbol, r.eod_close, r.candle_close,
                                 outcome.get((r.instrument, r.series, r.trade_date), "match")))
    meta = dict(rows=len(rows), missing_unexplained=missing_unexpl, extra_unexplained=extra_unexpl)
    pickle.dump(dict(meta=meta, stretches=dict(stretches), actions=actions, ex=ex), open(OUT, "wb"))
    from collections import Counter
    print(meta, Counter(outcome.values()), len(stretches), len(insts))
