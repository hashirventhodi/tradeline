from pathlib import Path

import pytest

from tradeline.config import Config, ConfigError, UniverseRule, load_config


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


def test_the_committed_config_states_the_universe_rule_of_adr_0005() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "tradeline.toml")

    assert config.universe == UniverseRule(
        top_n=200,
        lookback_trading_days=126,
        min_listing_trading_days=126,
        min_presence_percent=90,
        series="EQ",
        excluded_isin_prefix="INF",
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
