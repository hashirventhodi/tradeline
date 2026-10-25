"""The two Limit sets of ``docs/risk-controls.md``, as tests state them.

``PULLBACK`` is the table of Limits (``pullback_v1``'s, the configured defaults);
``LOWVOL`` is "Limits by family"'s ``lowvol`` column (ADR 0020).
"""

from decimal import Decimal

from tradeline.config import Config
from tradeline.risk import Limits

D = Decimal

PULLBACK = Config.Risk().limits()

LOWVOL = Limits(
    stop_required=False,
    risk_per_trade=None,
    min_reward_to_risk=None,
    max_new_entries=None,
    max_sector=None,
    max_open_positions=30,
    max_position=D("0.15"),
    max_gross=D("1"),
    participation_cap=D("0.01"),
    min_order_value=D("5000"),
)
