import pytest

from pulsar.errors import INVALID_TEXT, SECRET_DETECTED, PulsarError
from pulsar.guard import scan_for_secrets, validate_text, weighted_length
from pulsar.settings import DEFAULT_PLAIN_POST_USD, DEFAULT_URL_POST_USD, Prices


def test_weighted_length_counts_urls_as_23_and_emoji_as_2():
    assert weighted_length("hello") == 5
    assert weighted_length("see https://example.com/some/very/long/path/that/keeps/going") == 4 + 23
    assert weighted_length("🚀") == 2
    assert weighted_length("日本語") == 6


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_text_is_invalid(text):
    with pytest.raises(PulsarError) as exc:
        validate_text(text)
    assert exc.value.code == INVALID_TEXT


def test_over_limit_is_invalid_with_detail():
    with pytest.raises(PulsarError) as exc:
        validate_text("x" * 281)
    assert exc.value.code == INVALID_TEXT
    assert exc.value.detail["weighted_length"] == 281


def test_exactly_280_is_fine():
    assert validate_text("x" * 280).weighted_length == 280


def test_control_characters_are_invalid():
    with pytest.raises(PulsarError) as exc:
        validate_text("hello\x00world")
    assert exc.value.code == INVALID_TEXT


@pytest.mark.parametrize(
    "text",
    [
        "my key is sk-abcdefghijklmnopqrstuvwxyz123456",
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "api_key=abcdefghijklmnop1234567890",
    ],
)
def test_secret_like_text_is_rejected(text):
    with pytest.raises(PulsarError) as exc:
        validate_text(text)
    assert exc.value.code == SECRET_DETECTED
    assert exc.value.detail["matched"]


def test_ordinary_text_has_no_secret_hits():
    assert scan_for_secrets("Shipping pulsar today. Posts now flow through an MCP bridge.") == []


def test_cost_estimate_depends_on_url():
    plain = validate_text("Hello from the constellation.")
    linked = validate_text("Read more at https://constellation-works.com/orbit")
    assert plain.estimated_cost_usd == DEFAULT_PLAIN_POST_USD
    assert linked.estimated_cost_usd == DEFAULT_URL_POST_USD
    assert linked.has_url and not plain.has_url


def test_cost_comes_from_the_configured_price_table():
    prices = Prices(plain_post_usd=0.01, url_post_usd=0.5)
    assert validate_text("plain", prices).estimated_cost_usd == 0.01
    assert validate_text("see https://example.com", prices).estimated_cost_usd == 0.5
