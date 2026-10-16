"""Physical fixture guard for the opt-in postfix-exclusion browser case."""

from tests.browser.runtime_live_browser_support import CASES, expected_housing, fixture_uploads


def test_postfix_exclusion_browser_case_keeps_unknown_prices_and_same_oracle():
    case_id = "housing_exclude_postfix"
    assert case_id in CASES
    assert "的房源排除" in CASES[case_id]["prompt"]
    assert fixture_uploads(case_id, seed=17) == fixture_uploads("housing_exclude", seed=17)
    expected = expected_housing(case_id, seed=17)
    assert len(expected) == 4
    assert expected["总价万元"].isna().sum() == 1
    assert set(expected["房源ID"]) == set(expected_housing("housing_exclude", seed=17)["房源ID"])
