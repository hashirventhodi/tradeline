"""Walk-Forward's choice of a Window's parameters from its grid cells' in-sample scores (no
database).

The grid is k in {1.5, 2, 2.5, 3}, r in {2}, n in {5, 10}: eight cells, r having
no neighbours. A cell's objective is the mean of its own Sharpe and its
neighbours' along each axis. With these Sharpes (k across, n down)

    n = 5:   0.2   2.4   0.1   1.2
    n = 10:  0.3   0.2   1.3   1.4

the spike at k = 2, n = 5 averages (2.4 + 0.2 + 0.1 + 0.2) / 4 = 0.725, while
the plateau corner k = 3, n = 10 averages (1.4 + 1.3 + 1.2) / 3 = 1.3, the
highest; next is the spike's neighbour k = 2.5, n = 5, at
(0.1 + 2.4 + 1.2 + 1.3) / 4 = 1.25.
"""

from collections.abc import Mapping
from decimal import Decimal

from tradeline.walk_forward import CellScore, choose

D = Decimal
GRID: Mapping[str, tuple[Decimal, ...]] = {
    "k": (D("1.5"), D("2"), D("2.5"), D("3")),
    "n": (D(5), D(10)),
    "r": (D("2"),),
}
SHARPES = {
    (D("1.5"), D(5)): D("0.2"),
    (D("2"), D(5)): D("2.4"),
    (D("2.5"), D(5)): D("0.1"),
    (D("3"), D(5)): D("1.2"),
    (D("1.5"), D(10)): D("0.3"),
    (D("2"), D(10)): D("0.2"),
    (D("2.5"), D(10)): D("1.3"),
    (D("3"), D(10)): D("1.4"),
}


def cells(
    sharpes: Mapping[tuple[Decimal, Decimal], Decimal | None] = SHARPES,
    trades: Mapping[tuple[Decimal, Decimal], int] | None = None,
) -> list[CellScore]:
    return [
        CellScore({"k": k, "r": D("2"), "n": n}, (trades or {}).get((k, n), 30), sharpe)
        for (k, n), sharpe in sharpes.items()
    ]


def test_a_plateau_of_good_cells_beats_a_lone_spike() -> None:
    chosen = choose(cells(), GRID, min_trades=30)

    assert chosen.parameters == {"k": D("3"), "r": D("2"), "n": D(10)}
    assert chosen.objective == (D("1.4") + D("1.3") + D("1.2")) / 3
    assert (chosen.sharpe, chosen.trades, chosen.eligible) == (D("1.4"), 30, 8)


def test_a_cell_under_the_minimum_trades_is_never_chosen_but_still_counts_as_a_neighbour() -> None:
    chosen = choose(cells(trades={(D("3"), D(10)): 29}), GRID, min_trades=30)

    # k = 2.5, n = 5 is next, its average reading the ineligible corner's neighbour as before.
    assert chosen.parameters == {"k": D("2.5"), "r": D("2"), "n": D(5)}
    assert chosen.objective == (D("0.1") + D("2.4") + D("1.2") + D("1.3")) / 4
    assert chosen.eligible == 7


def test_equal_objectives_go_to_the_higher_sharpe_then_more_trades_then_smaller_k_r_n() -> None:
    flat = dict.fromkeys(SHARPES, D(1))

    assert choose(cells(flat), GRID, min_trades=30).parameters == {
        "k": D("1.5"),
        "r": D("2"),
        "n": D(5),
    }
    more = choose(cells(flat, trades={(D("2.5"), D(10)): 31}), GRID, min_trades=30)
    assert more.parameters == {"k": D("2.5"), "r": D("2"), "n": D(10)}
    higher = dict(flat)
    higher[(D("3"), D(5))] = D("1.0001")
    # k = 3, n = 5 and its corner neighbour k = 3, n = 10 both average (1.0001 + 1 + 1) / 3;
    # the higher raw Sharpe decides.
    assert choose(cells(higher), GRID, min_trades=30).parameters == {
        "k": D("3"),
        "r": D("2"),
        "n": D(5),
    }


def test_a_neighbour_without_a_sharpe_is_left_out_of_the_average() -> None:
    sharpes: dict[tuple[Decimal, Decimal], Decimal | None] = dict(SHARPES)
    sharpes[(D("3"), D(5))] = None

    chosen = choose(cells(sharpes), GRID, min_trades=30)

    # k = 3, n = 10 now averages only itself and k = 2.5, n = 10: (1.4 + 1.3) / 2 = 1.35.
    assert chosen.parameters == {"k": D("3"), "r": D("2"), "n": D(10)}
    assert chosen.objective == (D("1.4") + D("1.3")) / 2


def test_a_window_where_no_cell_reaches_the_minimum_trades_makes_no_selection() -> None:
    chosen = choose(cells(trades=dict.fromkeys(SHARPES, 29)), GRID, min_trades=30)

    assert (chosen.parameters, chosen.objective, chosen.eligible) == (None, None, 0)
