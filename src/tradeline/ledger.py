"""The engine's writes to the Ledger (ADR 0019): every row a day of an account adds.

Each row is tagged with its account (``queries.Account``: the Engine Mode, the
backtest id in backtest mode, and whether a paper Run is a dry run). A Run
writes its Strategy version, the Run, its Candidates, Plans, Passes and Blocks
and a Verdict on each Candidate; the account's session and Orders write
Orders, Fills, Positions and dividend credits as event rows, each with the day
and a sequence number that orders it within the account. Nothing here decides anything: the
engine (``tradeline.engine``) decides, and these functions write what it
decided. Reads are ``tradeline.queries``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

import psycopg

from tradeline import queries
from tradeline.costs import Charges
from tradeline.dates import month_of
from tradeline.demergers import Demerger
from tradeline.dividends import Dividend
from tradeline.paper_broker import OrderPurpose
from tradeline.queries import EngineMode
from tradeline.risk import Decision, Pass, PortfolioPlan, Proposal
from tradeline.strategy import Candidate, Plan
from tradeline.strategy.portfolio import AnyStrategy
from tradeline.tax import Liquidation, Losses, TaxYear


@dataclass(frozen=True)
class Order:
    """An Order as the engine holds it between its events."""

    id: uuid.UUID
    run_id: uuid.UUID
    rank: int | None
    position_id: uuid.UUID
    instrument: str
    symbol: str
    purpose: OrderPurpose
    limit_price: Decimal
    shares: int
    session: date

    @property
    def side(self) -> str:
        return "buy" if self.purpose == "entry" else "sell"


def insert_order(
    conn: psycopg.Connection,
    account: queries.Account,
    order: Order,
    *,
    event: str,
    day: date,
    seq: int,
    reason: str | None,
) -> None:
    """The Order ``placed`` or ``cancelled`` (with its reason) on ``day``."""
    conn.execute(
        "INSERT INTO orders (mode, backtest_id, dry_run, id, event, day, seq, reason, run_id,"
        " rank, position_id, instrument, symbol, side, purpose, limit_price, shares, session)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            account["mode"],
            account["backtest_id"],
            account["dry_run"],
            order.id,
            event,
            day,
            seq,
            reason,
            order.run_id,
            order.rank,
            order.position_id,
            order.instrument,
            order.symbol,
            order.side,
            order.purpose,
            order.limit_price,
            order.shares,
            order.session,
        ),
    )


def insert_fill(
    conn: psycopg.Connection,
    account: queries.Account,
    order: Order,
    *,
    price: Decimal,
    charges: Charges,
    day: date,
    seq: int,
) -> None:
    """The Order's Fill at ``price``, with its charges and Slippage itemised."""
    conn.execute(
        "INSERT INTO fills (mode, backtest_id, dry_run, order_id, day, seq, position_id,"
        " instrument, symbol, side, price, shares, "
        + ", ".join(charges.items())
        + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
        + ", %s" * len(charges.items())
        + ")",
        (
            account["mode"],
            account["backtest_id"],
            account["dry_run"],
            order.id,
            day,
            seq,
            order.position_id,
            order.instrument,
            order.symbol,
            order.side,
            price,
            order.shares,
            *charges.items().values(),
        ),
    )


def insert_position(
    conn: psycopg.Connection,
    account: queries.Account,
    position: queries.LedgerPosition,
    *,
    day: date,
    seq: int,
    event: str | None = None,
    factor: Fraction | None = None,
    cash_in_lieu: Decimal | None = None,
) -> None:
    """The Position's state after an event: ``opened`` or ``closed`` by its exit unless
    ``event`` says otherwise (``restated``, with the factor and any cash in lieu)."""
    conn.execute(
        "INSERT INTO positions (mode, backtest_id, dry_run, position_id, event, day, seq,"
        " run_id, rank, instrument, symbol, opened_on, shares, entry, stop, target,"
        " time_stop_days, exit_price, exit_reason, factor, cash_in_lieu)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        " %s, %s, %s)",
        (
            account["mode"],
            account["backtest_id"],
            account["dry_run"],
            position.id,
            event or ("opened" if position.exit_reason is None else "closed"),
            day,
            seq,
            position.run_id,
            position.rank,
            position.instrument,
            position.symbol,
            position.opened_on,
            position.shares,
            position.entry,
            position.stop,
            position.target,
            position.time_stop_days,
            position.exit_price,
            position.exit_reason,
            None if factor is None else f"{factor.numerator}/{factor.denominator}",
            cash_in_lieu,
        ),
    )


def insert_dividend_credit(
    conn: psycopg.Connection,
    account: queries.Account,
    position: queries.LedgerPosition,
    dividend: Dividend,
    *,
    day: date,
    seq: int,
) -> None:
    """The Position's credit of ``dividend`` on ``day``: its shares times the amount per
    share, or nothing, with the reason, when the subject is unparsed."""
    per_share = dividend.per_share
    conn.execute(
        "INSERT INTO dividend_credits (mode, backtest_id, dry_run, day, seq, position_id,"
        " instrument, symbol, ex_date, subject, shares, per_share, cash, reason)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            account["mode"],
            account["backtest_id"],
            account["dry_run"],
            day,
            seq,
            position.id,
            position.instrument,
            position.symbol,
            dividend.ex_date,
            dividend.subject,
            position.shares,
            per_share,
            None if per_share is None else per_share * position.shares,
            dividend.reason,
        ),
    )


def insert_demerger_credit(
    conn: psycopg.Connection,
    account: queries.Account,
    held: queries.HeldAtClose,
    demerger: Demerger,
    *,
    close: Decimal,
    cash: Decimal,
    basis: Decimal,
    day: date,
    seq: int,
) -> None:
    """The Demerger Credit on ``day`` of a Position ``held`` at the close before the Ex-Date:
    ``cash`` for its entitlement at the child's ``close``, and the child's ``basis``."""
    conn.execute(
        "INSERT INTO demerger_credits (mode, backtest_id, dry_run, day, seq, position_id,"
        " instrument, parent, child, ex_date, bought, shares, ratio_numerator,"
        " ratio_denominator, close, cash, basis)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            account["mode"],
            account["backtest_id"],
            account["dry_run"],
            day,
            seq,
            held.position_id,
            held.instrument,
            demerger.parent,
            demerger.child,
            demerger.ex_date,
            held.opened_on,
            held.shares,
            demerger.ratio.numerator,
            demerger.ratio.denominator,
            close,
            cash,
            basis,
        ),
    )


def insert_tax_debit(
    conn: psycopg.Connection,
    account: queries.Account,
    year: TaxYear,
    *,
    day: date,
    dropped: Losses | None = None,
) -> None:
    """The financial year ``year``'s tax debited from the account's cash on ``day``; with
    ``dropped``, a liquidation's settlement of its final year and the losses it left."""
    conn.execute(
        "INSERT INTO tax_debits (mode, backtest_id, dry_run, id, day, financial_year,"
        " short_term_gain, long_term_gain, dividends, tax, settled, dropped_short_term_loss,"
        " dropped_long_term_loss) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            account["mode"],
            account["backtest_id"],
            account["dry_run"],
            uuid.uuid4(),
            day,
            year.year,
            year.short_term_gain,
            year.long_term_gain,
            year.dividends,
            year.tax,
            dropped is not None,
            None if dropped is None else dropped.short_term,
            None if dropped is None else dropped.long_term,
        ),
    )


def insert_liquidation_value(
    conn: psycopg.Connection,
    account: queries.Account,
    liquidation: Liquidation,
    benchmark: queries.BenchmarkDay | None,
    *,
    day: date,
) -> None:
    """The account's Liquidation Value on ``day``, and the Benchmark's when it is held."""
    conn.execute(
        "INSERT INTO liquidation_values (mode, backtest_id, dry_run, id, day, value, tax_due,"
        " dropped_short_term_loss, dropped_long_term_loss, benchmark_scheme,"
        " benchmark_bought_on, benchmark_bought_nav, benchmark_nav, benchmark_value,"
        " benchmark_tax_due) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
        " %s)",
        (
            account["mode"],
            account["backtest_id"],
            account["dry_run"],
            uuid.uuid4(),
            day,
            liquidation.value,
            liquidation.tax_due,
            liquidation.losses_dropped.short_term,
            liquidation.losses_dropped.long_term,
            *(
                (None,) * 6
                if benchmark is None
                else (
                    benchmark.scheme,
                    benchmark.bought_on,
                    benchmark.bought_nav,
                    benchmark.nav,
                    benchmark.value,
                    benchmark.tax_due,
                )
            ),
        ),
    )


def strategy_row(
    conn: psycopg.Connection,
    strategy: AnyStrategy,
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    protocol: str | None,
    code_revision: str,
) -> uuid.UUID:
    """The Strategy version's row in the account, written once; its id."""
    key = {
        "mode": mode,
        "backtest_id": backtest_id,
        "family": strategy.family,
        "name": strategy.name,
        "code_revision": code_revision,
        "protocol": protocol,
        "ranking": strategy.ranking,
    }
    conn.execute(
        "INSERT INTO strategies (mode, backtest_id, family, name, code_revision, protocol,"
        " ranking) VALUES (%(mode)s, %(backtest_id)s, %(family)s, %(name)s,"
        " %(code_revision)s, %(protocol)s, %(ranking)s)"
        " ON CONFLICT DO NOTHING",
        key,
    )
    row = conn.execute(
        "SELECT id FROM strategies WHERE mode = %(mode)s"
        " AND backtest_id IS NOT DISTINCT FROM %(backtest_id)s AND name = %(name)s"
        " AND code_revision = %(code_revision)s"
        " AND protocol IS NOT DISTINCT FROM %(protocol)s",
        key,
    ).fetchone()
    assert row is not None
    found: uuid.UUID = row[0]
    return found


def insert_run(
    conn: psycopg.Connection,
    strategy: AnyStrategy,
    *,
    run_id: uuid.UUID,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool,
    strategy_id: uuid.UUID,
    as_of: date,
    rule_version: str,
    universe_size: int,
    cash: Decimal,
    equity: Decimal,
    impact: Decimal | None,
    portfolio_step: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO runs (id, mode, backtest_id, dry_run, strategy_id, as_of, universe_month,"
        " universe_rule_version, universe_size, parameters, cash, equity, impact,"
        " portfolio_step) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            mode,
            backtest_id,
            dry_run,
            strategy_id,
            as_of,
            month_of(as_of),
            rule_version,
            universe_size,
            json.dumps(dict(strategy.parameters()), sort_keys=True),
            cash,
            equity,
            impact,
            portfolio_step,
        ),
    )


def _facts(values: Any) -> str:
    return json.dumps({name: str(value) for name, value in values.items()}, sort_keys=True)


def insert_candidates(
    conn: psycopg.Connection,
    candidates: Sequence[Candidate],
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool,
    run_id: uuid.UUID,
) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO candidates (mode, backtest_id, dry_run, run_id, rank, instrument,"
            " symbol, score, facts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    mode,
                    backtest_id,
                    dry_run,
                    run_id,
                    c.rank,
                    c.instrument,
                    c.symbol,
                    c.score,
                    _facts(c.facts),
                )
                for c in candidates
            ],
        )


def insert_plans(
    conn: psycopg.Connection,
    plans: Sequence[Plan | PortfolioPlan],
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool,
    run_id: uuid.UUID,
) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO plans (mode, backtest_id, dry_run, run_id, rank, instrument, symbol,"
            " entry, stop, target, time_stop_days, facts)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    mode,
                    backtest_id,
                    dry_run,
                    run_id,
                    p.rank,
                    p.instrument,
                    p.symbol,
                    p.entry,
                    p.stop,
                    p.target,
                    p.time_stop_days,
                    _facts(p.facts if isinstance(p, Plan) else {}),
                )
                for p in plans
            ],
        )


def insert_decisions(
    conn: psycopg.Connection,
    decisions: Sequence[Decision],
    proposals: Sequence[Proposal],
    *,
    equity: Decimal,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool,
) -> None:
    """Each Pass or Block, with what it read, and the Verdict it makes."""
    with conn.cursor() as cursor:
        for decision, proposal in zip(decisions, proposals, strict=True):
            plan = decision.plan
            facts = _facts(
                {
                    "equity": equity,
                    "tick": proposal.tick,
                    "average_traded_value": proposal.average_traded_value,
                    **{f"allowed_{name}": shares for name, shares in decision.allowed.items()},
                }
            )
            priced = (
                plan.entry,
                plan.stop,
                plan.target,
                plan.time_stop_days,
                plan.shares,
                decision.sector,
                facts,
            )
            if isinstance(decision, Pass):
                cursor.execute(
                    "INSERT INTO passes (mode, backtest_id, dry_run, run_id, rank, plan_hash,"
                    " entry, stop, target, time_stop_days, shares, sector, facts)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        mode,
                        backtest_id,
                        dry_run,
                        decision.run_id,
                        plan.rank,
                        decision.plan_hash,
                        *priced,
                    ),
                )
                verdict, reason = "BUY", "pass"
            else:
                cursor.execute(
                    "INSERT INTO blocks (mode, backtest_id, dry_run, run_id, rank, limit_name,"
                    " entry, stop, target, time_stop_days, shares, sector, facts)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        mode,
                        backtest_id,
                        dry_run,
                        decision.run_id,
                        plan.rank,
                        decision.limit,
                        *priced,
                    ),
                )
                verdict, reason = "SKIP", decision.limit
            cursor.execute(
                "INSERT INTO verdicts (mode, backtest_id, dry_run, run_id, rank, verdict,"
                " reason) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (mode, backtest_id, dry_run, decision.run_id, plan.rank, verdict, reason),
            )


def insert_held_verdicts(
    conn: psycopg.Connection,
    plans: Sequence[Plan | PortfolioPlan],
    *,
    mode: EngineMode,
    backtest_id: uuid.UUID | None,
    dry_run: bool,
    run_id: uuid.UUID,
) -> None:
    """SKIP, reason ``held``, for each Candidate whose Instrument the account already holds."""
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO verdicts (mode, backtest_id, dry_run, run_id, rank, verdict, reason)"
            " VALUES (%s, %s, %s, %s, %s, 'SKIP', 'held')",
            [(mode, backtest_id, dry_run, run_id, p.rank) for p in plans],
        )
