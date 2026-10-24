from pathlib import Path

import pytest

from tradeline.config import Config, ConfigError, UniverseRule, load_config
from tradeline.risk import Limits


def test_loads_settings_from_the_toml_file(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text('[logging]\nlevel = "DEBUG"\n')

    config = load_config(path)

    assert config == Config(logging=Config.Logging(level="DEBUG"))


def test_missing_sections_take_their_defaults(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("")

    config = load_config(path)

    assert config.logging.level == "INFO"


def test_unknown_keys_are_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[logging]\nlevel = 'INFO'\ncolour = true\n")

    with pytest.raises(ConfigError, match="logging.colour"):
        load_config(path)


def test_unknown_sections_are_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[broker]\nname = 'kite'\n")

    with pytest.raises(ConfigError, match="broker"):
        load_config(path)


def test_a_wrong_type_is_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[logging]\nlevel = 3\n")

    with pytest.raises(ConfigError, match="logging.level"):
        load_config(path)


def test_a_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="nowhere.toml"):
        load_config(tmp_path / "nowhere.toml")


def test_the_committed_config_file_loads() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "tradeline.toml")

    assert config.logging.level == "INFO"


def test_the_committed_config_states_the_universe_rule_of_adr_0005_ranking_instruments() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "tradeline.toml")

    # ADR 0016: the Universe Rule ranks Instruments. ADR 0017: Listing Age counts from
    # the earliest NSE evidence, the version #18 stored over the 120 months.
    assert config.universe == UniverseRule(
        top_n=200,
        lookback_trading_days=126,
        min_listing_trading_days=126,
        min_presence_percent=90,
        series="EQ",
        excluded_isin_prefix="INF",
        rank_by="instrument",
        listing_age_from="earliest_evidence",
    )
    assert config.universe.version == (
        "f4997f66b2733bf8c8fe02e2a0aaf22acfefea8b41a97fe20e91b0ac81160715"
    )


def test_universe_rule_parameters_load_and_any_change_is_a_new_version(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[universe]\ntop_n = 100\n")

    rule = load_config(path).universe

    assert rule.top_n == 100
    assert rule.lookback_trading_days == 126
    assert rule.version != UniverseRule().version
    assert len(rule.version) == 64
    assert UniverseRule(top_n=100).version == rule.version


def test_the_rule_ranking_symbols_keeps_the_version_its_memberships_were_stored_under() -> None:
    # The #13 acceptance run stored ten years of membership under this version.
    assert UniverseRule().version == (
        "53181efb872605eba686125c8fa9127b98abb79a9fe00158117e1f08222d62e5"
    )
    assert UniverseRule(rank_by="symbol").version == UniverseRule().version
    assert "rank_by" not in UniverseRule().parameters


def test_ranking_instruments_is_a_new_rule_version_that_records_it(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text('[universe]\nrank_by = "instrument"\n')

    rule = load_config(path).universe

    assert rule.rank_by == "instrument"
    assert rule.version != UniverseRule().version
    assert rule.parameters == UniverseRule().parameters | {"rank_by": "instrument"}


def test_listing_age_from_the_earliest_evidence_is_a_new_rule_version_that_records_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text('[universe]\nrank_by = "instrument"\nlisting_age_from = "earliest_evidence"\n')

    rule = load_config(path).universe
    by_listing_date = UniverseRule(rank_by="instrument")

    # ADR 0017. The rule ranking Instruments from the listing date keeps its version.
    assert by_listing_date.version == (
        "ded30b1014c4f6aa48ff01d83a7592077ab09fc718301f5ff3b275e9e12ef57e"
    )
    assert UniverseRule(rank_by="instrument", listing_age_from="listing_date") == by_listing_date
    assert rule.version != by_listing_date.version
    assert rule.parameters == by_listing_date.parameters | {"listing_age_from": "earliest_evidence"}


def test_listing_age_from_the_earliest_evidence_is_refused_for_a_rule_ranking_symbols(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text('[universe]\nlisting_age_from = "earliest_evidence"\n')

    with pytest.raises(ConfigError, match="universe.listing_age_from"):
        load_config(path)


def test_a_universe_rule_ranking_anything_else_is_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text('[universe]\nrank_by = "isin"\n')

    with pytest.raises(ConfigError, match="universe.rank_by"):
        load_config(path)


def test_an_impossible_universe_rule_is_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[universe]\nmin_presence_percent = 120\n")

    with pytest.raises(ConfigError, match="universe.min_presence_percent"):
        load_config(path)


def test_news_feeds_are_a_table_of_names_and_urls(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text(
        '[news.feeds]\nfirst = "https://a.example/rss"\nsecond = "https://b.example/rss"\n'
    )

    config = load_config(path)

    assert config.news.feeds == {
        "first": "https://a.example/rss",
        "second": "https://b.example/rss",
    }


def test_a_news_feed_that_is_not_a_string_is_refused_by_name(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[news.feeds]\nfirst = 3\n")

    with pytest.raises(ConfigError, match="news.feeds.first"):
        load_config(path)


def test_the_committed_config_names_at_least_two_news_feeds() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "tradeline.toml")

    assert len(config.news.feeds) >= 2
    assert all(url.startswith("https://") for url in config.news.feeds.values())


def test_the_committed_config_states_kite_paritys_tolerance_and_gate() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "tradeline.toml")

    assert config.parity == Config.Parity(close_tolerance=0.001, max_unexplained_pct=0.1)


def test_the_committed_config_gives_parity_and_integrity_one_trading_calendar() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "tradeline.toml")

    assert config.parity.trading_calendar == config.integrity.trading_calendar


def test_parity_settings_load_from_their_own_section(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[parity]\nclose_tolerance = 0.002\nmax_unexplained_pct = 0.5\n")

    assert load_config(path).parity == Config.Parity(close_tolerance=0.002, max_unexplained_pct=0.5)


def test_the_retired_additive_minimum_is_refused_naming_what_replaced_it(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text("[parity]\nadditive_min_bars = 5\n")

    with pytest.raises(ConfigError) as refused:
        load_config(path)

    assert str(refused.value) == (
        f"{path}: parity.additive_min_bars was removed (ADR 0018, #62): Kite's Adjustment"
        " explains a close only in a Kite Basis of at least 20 compared bars, with at most"
        " 5 percent of a stretch's closes off the replay; delete the key"
    )


def test_a_config_with_the_retired_reconcile_section_is_refused_naming_parity(
    tmp_path: Path,
) -> None:
    # ADR 0018: the word is kept for Phase 6's reconciliation of positions against the Ledger.
    path = tmp_path / "tradeline.toml"
    path.write_text("[reconcile]\nmax_discrepancy_pct = 0.1\n")

    with pytest.raises(ConfigError, match=r"\[reconcile\] is retired.*\[parity\]"):
        load_config(path)


def test_the_integrity_section_names_the_trading_calendar_file(tmp_path: Path) -> None:
    path = tmp_path / "tradeline.toml"
    path.write_text(
        '[parity]\ntrading_calendar = "data/calendar.csv"\n'
        '[integrity]\ntrading_calendar = "data/calendar.csv"\n'
    )

    assert load_config(path).integrity == Config.Integrity(trading_calendar="data/calendar.csv")


def test_a_config_whose_two_trading_calendars_differ_is_refused_naming_both(
    tmp_path: Path,
) -> None:
    # One Trading Calendar serves the Integrity Check and Kite Parity (ADR 0018).
    path = tmp_path / "tradeline.toml"
    path.write_text('[integrity]\ntrading_calendar = "data/calendar.csv"\n')

    with pytest.raises(
        ConfigError,
        match=r"parity.trading_calendar \(trading_calendar.csv\)"
        r" and integrity.trading_calendar \(data/calendar.csv\)",
    ):
        load_config(path)


def test_the_committed_config_states_the_default_limits_and_the_tick_table() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "tradeline.toml")

    assert config.risk.limits() == Limits()
    assert config.risk.tick_sizes == "tick_sizes.csv"
