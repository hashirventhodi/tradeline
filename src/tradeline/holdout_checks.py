"""The pre-Holdout checks: the machinery checked from records, not results, before the
one-shot Holdout runs (ADR 0007, 2026-10-25 amendment; #102).

``check_holdout`` runs four checks for a frozen Protocol and records each in
``holdout_checks`` with the git commit the process runs at:

- **the contract-note test** (``contract_notes``): every real contract note the
  Operator supplied is charged what the rates in force on its trade date say.
  With none supplied it is unmet, unless the Operator explicitly accepts it as
  unmet, with a reason, which is recorded. A note off the rates fails whatever
  is accepted.
- **the dividend subjects** (``dividends``): every dividend subject of every
  eligible member over the Protocol's range parsed (``tradeline.dividends``), or
  is in the reviewed list of unparsed subjects with the Operator's reason.
- **the demerger table** (``demergers``): every demerger in NSE's Corporate
  Action lists for an eligible member over the range has a row in the demerger
  table in force with its parent and Ex-Date.
- **the tax-ledger tests** (``tax_tests``): the tax ledger's synthetic tests
  (``tests/test_tax_ledger.py``) pass when run at this checkout.

The eligible members are a superset read from records alone, with no Strategy
run: every member of every Universe month the Protocol froze, from its first
In-Sample month to its Holdout's, so every decision date of every run under it.
A member's actions are read over the same range.

``require_passed`` is the Holdout's gate: ``holdout.run_holdout`` refuses,
naming each item, unless the latest check of every item recorded for the
Protocol at the commit it runs at passed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from tradeline import queries, revision
from tradeline.costs import ContractNote, CostModel, CostSchedule, Slippage
from tradeline.dividends import ListedDividend, dividend_check
from tradeline.freeze import run_members

log = logging.getLogger(__name__)

# Each check, by its Ledger name, as a refusal names it.
ITEMS = {
    "contract_notes": "the contract-note test",
    "dividends": "the dividend subjects",
    "demergers": "the demerger table",
    "tax_tests": "the tax-ledger tests",
}
# The tax ledger's synthetic tests, relative to the checkout's root.
TAX_TESTS = Path("tests") / "test_tax_ledger.py"
# NSE's "Demerger", "De-Merger", "Scheme Of Arrangement In The Nature Of Demerger"
# (``tradeline.adjust``'s reading), whatever kind a subject naming more was stored as.
_DEMERGER = re.compile(r"\bde-?\s*merger", re.IGNORECASE)
_NO_SLIPPAGE = Slippage(margin=Decimal(0), impact=Decimal(0))


class PreHoldoutChecksUnmet(ValueError):
    """The Holdout refused: a pre-Holdout check has not passed at the commit it runs at."""


@dataclass(frozen=True)
class TaxTestOutcome:
    """Whether a test module passed, and pytest's last line."""

    passed: bool
    summary: str


@dataclass(frozen=True)
class Check:
    """One check's outcome: ``summary`` says what failed or what was counted, ``detail`` what
    was read; ``accepted`` is the reason a contract-note test with no note was accepted."""

    item: str
    passed: bool
    summary: str
    detail: dict[str, Any]
    accepted: str | None = None


@dataclass(frozen=True)
class HoldoutChecks:
    protocol: str
    git_commit: str
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def summary(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "git_commit": self.git_commit,
            "passed": self.passed,
            "checks": {
                c.item: {"passed": c.passed, "summary": c.summary, "accepted": c.accepted}
                for c in self.checks
            },
        }


def run_tax_tests() -> TaxTestOutcome:
    """The tax ledger's synthetic tests run by pytest at this checkout."""
    root = revision.PACKAGE_DIR.parents[2]
    if not (root / TAX_TESTS).exists():
        return TaxTestOutcome(False, f"{TAX_TESTS} is not in the checkout at {root}")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(TAX_TESTS)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    last = lines[-1].strip("= ") if lines else done.stderr.strip()[-200:]
    return TaxTestOutcome(done.returncode == 0 and "passed" in last, last)


def eligible_members(conn: psycopg.Connection, frozen: queries.FrozenProtocol) -> set[str]:
    """Every member of every Universe month ``frozen`` froze, as its Runs read them: a
    superset of the names eligible at any of its decision dates, read with no run."""
    terms = frozen.protocol
    return {
        member
        for month in terms.snapshot.months
        for member in run_members(conn, month.month, terms.rule_version)
    }


def check_holdout(
    conn: psycopg.Connection,
    *,
    protocol: str,
    contract_notes: Sequence[ContractNote],
    schedule: CostSchedule,
    listed: Sequence[ListedDividend],
    accept_no_contract_notes: str | None = None,
    tax_tests: Callable[[], TaxTestOutcome] | None = None,
) -> HoldoutChecks:
    """Run and record the pre-Holdout checks of the frozen Protocol ``protocol`` (its hash,
    or 12 or more characters of it) at the commit this process runs."""
    frozen = queries.protocol(conn, protocol)
    if frozen is None:
        raise ValueError(
            f"no Protocol {protocol} is frozen; `tradeline protocol freeze` freezes one"
        )
    commit = revision.running_commit()
    terms = frozen.protocol
    members = sorted(eligible_members(conn, frozen))
    start, end = terms.first, terms.last
    checks = (
        _contract_notes(contract_notes, schedule, accept_no_contract_notes),
        _dividends(conn, members, listed, start=start, end=end),
        _demergers(conn, members, start=start, end=end),
        _tax_tests((tax_tests or run_tax_tests)()),
    )
    with conn.transaction():
        for check in checks:
            conn.execute(
                "INSERT INTO holdout_checks (id, protocol, git_commit, item, passed, accepted,"
                " summary, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    frozen.hash,
                    commit,
                    check.item,
                    check.passed,
                    check.accepted,
                    check.summary,
                    json.dumps(check.detail, sort_keys=True),
                ),
            )
    conn.commit()
    result = HoldoutChecks(frozen.hash, commit, checks)
    log.info("pre-holdout checks finished", extra=result.summary())
    return result


def require_passed(conn: psycopg.Connection, frozen: queries.FrozenProtocol) -> None:
    """Refuse, naming each, unless every check's latest record for ``frozen`` at the commit
    this process runs passed."""
    commit = revision.running_commit()
    latest = {
        c.item: c for c in queries.holdout_checks(conn, protocol=frozen.hash, git_commit=commit)
    }
    unmet = []
    for item, name in ITEMS.items():
        check = latest.get(item)
        if check is None:
            unmet.append(f"{name} (not run at commit {commit[:12]})")
        elif not check.passed:
            unmet.append(f"{name} ({check.summary})")
    if unmet:
        short = frozen.hash[:12]
        raise PreHoldoutChecksUnmet(
            f"the Holdout of Protocol {short} runs only once every pre-Holdout check has passed"
            " at this commit; unmet: " + "; ".join(unmet) + ". Fix each and rerun"
            f" `tradeline backtest checks --protocol {short}`"
        )


def _contract_notes(
    notes: Sequence[ContractNote], schedule: CostSchedule, accepted: str | None
) -> Check:
    model = CostModel(schedule, _NO_SLIPPAGE)
    off: list[ContractNote] = []
    mismatches: list[dict[str, Any]] = []
    for note in notes:
        found = model.charges(
            note.side,
            day=note.trade_date,
            price=note.price,
            shares=note.shares,
            average_traded_value=Decimal(1),
        )
        charged = (note.stt, note.stamp_duty, note.exchange_charge, note.sebi_fee, note.gst)
        model_says = (found.stt, found.stamp_duty, found.exchange_charge, found.sebi_fee, found.gst)
        if charged != model_says:
            off.append(note)
            mismatches.append(
                {
                    "source": note.source,
                    "charged": [str(v) for v in charged],
                    "rates_say": [str(v) for v in model_says],
                }
            )
    detail: dict[str, Any] = {"notes": len(notes), "off_the_rates": mismatches}
    if off:
        sources = ", ".join(n.source for n in off)
        return Check(
            "contract_notes",
            False,
            f"{len(off)} of {len(notes)} contract notes charged other than the rates in force"
            f" on their trade dates: {sources}",
            detail,
        )
    if notes:
        return Check(
            "contract_notes",
            True,
            f"{len(notes)} contract notes match the rates in force on their trade dates",
            detail,
        )
    if accepted:
        return Check(
            "contract_notes",
            True,
            f"no contract note supplied; accepted as unmet: {accepted}",
            detail,
            accepted=accepted,
        )
    return Check(
        "contract_notes",
        False,
        "no contract note supplied, and not accepted as unmet",
        detail,
    )


def _dividends(
    conn: psycopg.Connection,
    members: Sequence[str],
    listed: Sequence[ListedDividend],
    *,
    start: date,
    end: date,
) -> Check:
    found = dividend_check(conn, members, start=start, end=end)
    keys = {row.key() for row in listed}
    unlisted = [d for d in found.unparsed if (d.symbol, d.ex_date, d.subject) not in keys]
    detail: dict[str, Any] = {
        "members": len(members),
        "parsed": len(found.parsed),
        "unparsed": len(found.unparsed),
        "unlisted": [
            {
                "symbol": d.symbol,
                "ex_date": d.ex_date.isoformat(),
                "subject": d.subject,
                "reason": d.reason,
            }
            for d in unlisted
        ],
    }
    if unlisted:
        named = ", ".join(f"{d.symbol} {d.ex_date} {d.subject!r} ({d.reason})" for d in unlisted)
        return Check(
            "dividends",
            False,
            f"{len(unlisted)} unparsed dividend subjects are not in the reviewed list: {named}",
            detail,
        )
    return Check(
        "dividends",
        True,
        f"{len(found.parsed)} dividend subjects parsed and {len(found.unparsed)} listed with a"
        f" reason, over {len(members)} eligible members",
        detail,
    )


def _demergers(
    conn: psycopg.Connection, members: Sequence[str], *, start: date, end: date
) -> Check:
    table = {(d.parent, d.ex_date) for d in queries.demergers(conn)}
    listed: dict[tuple[str, date], str] = {}
    for member in members:
        for action in queries.corporate_actions(conn, member, start=start, end=end, as_of=end):
            if action.nse_list is not None and _DEMERGER.search(action.subject):
                listed.setdefault((action.symbol, action.ex_date), action.subject)
    missing = [
        {"symbol": symbol, "ex_date": ex_date.isoformat(), "subject": subject}
        for (symbol, ex_date), subject in sorted(listed.items())
        if (symbol, ex_date) not in table
    ]
    detail: dict[str, Any] = {"listed": len(listed), "missing": missing}
    if missing:
        named = ", ".join(f"{m['symbol']} {m['ex_date']} {m['subject']!r}" for m in missing)
        return Check(
            "demergers",
            False,
            f"{len(missing)} demergers in NSE's list have no demerger table row: {named}",
            detail,
        )
    return Check(
        "demergers",
        True,
        f"each of {len(listed)} demergers in NSE's list for an eligible member has its row",
        detail,
    )


def _tax_tests(outcome: TaxTestOutcome) -> Check:
    detail = {"module": str(TAX_TESTS), "pytest": outcome.summary}
    if outcome.passed:
        return Check("tax_tests", True, f"{TAX_TESTS}: {outcome.summary}", detail)
    return Check("tax_tests", False, f"{TAX_TESTS} did not pass: {outcome.summary}", detail)
