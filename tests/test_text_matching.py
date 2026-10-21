"""Finding exact symbol and company-name mentions in free text (news items).

The listings are a small hand-made instrument master; real feed text is
exercised in ``test_ingest_news``.
"""

from tradeline.text_matching import TextMatcher

MATCHER = TextMatcher(
    [
        ("TCS", "Tata Consultancy Services Limited"),
        ("BAJAJ-AUTO", "Bajaj Auto Limited"),
        ("M&M", "Mahindra & Mahindra Limited"),
        ("RELIANCE", "Reliance Industries Limited"),
        ("RELIANCE", "Reliance Industries Limited"),  # a second series row
        ("TMPV", "Tata Motors Passenger Vehicles Limited"),
        ("TATAMOTORS", "Tata Motors Ltd."),
        ("ABC", "Twin Holdings Limited"),
        ("XYZ", "Twin Holdings Ltd"),
        ("NONAME", None),
    ]
)


def test_a_symbol_matches_as_a_whole_token_in_nse_case_only() -> None:
    assert MATCHER.match("TCS, BAJAJ-AUTO and M&M rally") == ("BAJAJ-AUTO", "M&M", "TCS")
    assert MATCHER.match("Tcs and tcs are not TCS's ticker") == ("TCS",)
    assert MATCHER.match("TCSL and BAJAJ-AUTOS are other tokens") == ()


def test_a_company_name_matches_without_its_legal_suffix_in_any_case() -> None:
    assert MATCHER.match("RELIANCE INDUSTRIES LTD. posts results") == ("RELIANCE",)
    assert MATCHER.match("Reliance Industries’ Jio") == ("RELIANCE",)
    assert MATCHER.match("mahindra & mahindra sales") == ("M&M",)


def test_a_vague_or_partial_name_does_not_match() -> None:
    assert MATCHER.match("Reliance Jio IPO") == ()
    assert MATCHER.match("Bajaj Finance shares fall") == ()
    assert MATCHER.match("Tata Consultancy hires") == ()


def test_a_name_shared_by_two_symbols_is_ambiguous_and_matches_neither() -> None:
    assert MATCHER.match("Twin Holdings announces a dividend") == ()


def test_a_possessive_or_an_apostrophe_in_the_name_still_matches() -> None:
    matcher = TextMatcher([("INFY", "Infosys Limited"), ("DRREDDY", "Dr. Reddy's Laboratories")])

    assert matcher.match("Infosys's results beat estimates") == ("INFY",)
    assert matcher.match("Infosys’ guidance") == ("INFY",)
    assert matcher.match("DR REDDYS LABORATORIES gains") == ("DRREDDY",)


def test_and_and_ampersand_are_one_spelling() -> None:
    assert MATCHER.match("Mahindra and Mahindra sales") == ("M&M",)


def test_a_name_inside_a_longer_matched_name_does_not_count() -> None:
    assert MATCHER.match("Tata Motors Passenger Vehicles sales") == ("TMPV",)
    assert MATCHER.match("Tata Motors sales") == ("TATAMOTORS",)


def test_an_exchange_name_alone_is_ambiguous_and_matches_no_listed_exchange() -> None:
    matcher = TextMatcher(
        [("BSE", "BSE Limited"), ("MCX", "Multi Commodity Exchange of India Limited")]
    )

    assert matcher.match("BSE Sensex climbs 400 points as IT stocks rally") == ()
    # An Economic Times feed's own description, as recorded in fixtures/ingest_news.
    assert (
        matcher.match("Find Stock Market Live Updates, BSE, NSE Top Gainers, Losers and more.")
        == ()
    )
    assert matcher.match("MCX gold futures rise") == ()
    assert matcher.match("BSE Ltd shares jump 8 percent on record volumes") == ("BSE",)
    assert matcher.match("Multi Commodity Exchange of India shares slip") == ("MCX",)
