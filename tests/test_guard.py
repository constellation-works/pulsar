import pytest

from pulsar.core import guard
from pulsar.core.errors import INVALID_TEXT, SECRET_DETECTED, PulsarError
from pulsar.core.guard import SECRET_PATTERNS, looks_generated, redact, scan_for_secrets
from pulsar.core.settings import DEFAULT_PLAIN_POST_USD, DEFAULT_URL_POST_USD, Prices
from pulsar.core.store import FernetFileStore, TokenBundle
from pulsar.providers.x.text import validate_text, weighted_length


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
        'api_key = "a8f3K2p9Qx7LmN4vB6tR1sZ0"',
    ],
)
def test_secret_like_text_is_rejected(text):
    with pytest.raises(PulsarError) as exc:
        validate_text(text)
    assert exc.value.code == SECRET_DETECTED
    assert exc.value.detail["matched"]


def test_ordinary_text_has_no_secret_hits():
    assert scan_for_secrets("Shipping pulsar today. Posts now flow through an MCP bridge.") == []


# One positive example per pattern, keyed by label: each is caught and redacted.
POSITIVE = {
    "anthropic key": "sk-ant-api03-Zx8Kq2Lm9Pw4Rt7Yv1Bn6Hc3",
    "openai-style key": "sk-proj-4fT9xQ2mL8vB1nR6kW3zY7",
    "github token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234",
    "github fine-grained pat": "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz",
    "slack token": "xoxb-1234567890-abcdefghij",
    "aws access key": "AKIAIOSFODNN7EXAMPLE",
    "google api key": "AIza" + "Sy8x3Kq9Lm2Pw7Rt4Yv6Bn1Hc5Jd0Fg-Ab_",
    "stripe key": "sk_live_4eC39HqLyjWDarjtT1zdp7dc",
    "x/twitter bearer": "AAAAAAAAAAAAAAAAAAAAAMLheAAAAAAA0%2BuSeid%2BULvsea4JtiGRiSDSJSI",
    "pem block": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "bearer header": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "generic secret assignment": 'client_secret = "a8f3K2p9Qx7LmN4vB6tR1sZ0"',
}


def test_every_pattern_has_a_positive_example():
    assert set(POSITIVE) == {p.label for p in SECRET_PATTERNS}


@pytest.mark.parametrize(("label", "secret"), sorted(POSITIVE.items()))
def test_each_pattern_is_caught_and_redacted(label, secret):
    text = f"before {secret} after"
    assert label in scan_for_secrets(text)
    masked = redact(text)
    assert masked.startswith("before ") and masked.endswith(" after")
    assert f"[redacted:{label}]" in masked
    assert scan_for_secrets(masked) == [], masked


def test_redaction_keeps_the_words_around_the_value():
    assert redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789") == (
        "Authorization: Bearer [redacted:bearer header]"
    )
    assert redact('api_key = "a8f3K2p9Qx7LmN4vB6tR1sZ0" ok') == (
        'api_key = "[redacted:generic secret assignment]" ok'
    )
    assert redact("key sk-ant-abcdefghijklmnopqrstu") == "key [redacted:anthropic key]"


@pytest.mark.parametrize(
    "text",
    [
        "Our new secret: remembering_to_hydrate_daily is key",
        "password: correct-horse-battery-staple",
        "token: v2-release-candidate-2026-09 ships today",
        "the bearer of good news",
        "Tracking task-sk-learning-pipeline-v2 and my-ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZab in orbit",
        "build-AKIAIOSFODNN7EXAMPLE-cache and x-sk_live_4eC39HqLyjWDarjtT1zdp7dc",
        "Read https://constellation-works.com/orbit/blog/2026-09-26-launch-notes?ref=x-sk-abc",
        "#buildinpublic #task-sk-learning-pipeline-v2 #secretsauce",
        "api_key and secret and token are words; password: hunter2",
    ],
)
def test_ordinary_words_identifiers_urls_and_hashtags_survive(text):
    assert scan_for_secrets(text) == []
    assert redact(text) == text


@pytest.mark.parametrize(
    ("value", "generated"),
    [
        ("a8f3K2p9Qx7LmN4vB6tR1sZ0", True),
        ("3f9a1c0e7b2d4f6a8c0e1b3d5f7a9c2e", True),
        ("remembering_to_hydrate_daily", False),
        ("correct-horse-battery-staple", False),
        ("v2-release-candidate-2026-09", False),
        ("hunter2hunter2hunter2hunter2", False),
        ("short1a", False),
        ("abcdefghijklmnopqrstu", True),
    ],
)
def test_looks_generated(value, generated):
    assert looks_generated(value) is generated


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


# -- live values ----------------------------------------------------------

# An X OAuth 2 token has no prefix and no header around it: only its value gives it away.
SHAPELESS = "dGhpcy1pcy1ub3QtYS1zaGFwZWQtdG9rZW4tMTIz"


def test_a_loaded_token_is_masked_whatever_its_shape(paths):
    text = f"X answered 401 for {SHAPELESS}"
    assert guard.redact(text) == text and guard.scan_for_secrets(text) == []
    store = FernetFileStore.for_account(paths, "x:constworks")
    store.save(
        TokenBundle(
            access_token=SHAPELESS, refresh_token=None, expires_at=0.0, scope="s", client_id="c"
        )
    )
    assert guard.redact(text) == "X answered 401 for [redacted:live credential]"
    assert guard.scan_for_secrets(text) == ["live credential"], "a post quoting it is refused"
    key = paths.key_file.read_text().strip()
    guard._live.clear()
    store.load()
    assert SHAPELESS not in guard.redact(text), "loading registers it too"
    assert key not in guard.redact(f"key={key}"), "and the store key"


def test_a_short_value_is_never_registered():
    guard.register_live_secret("hunter2")
    guard.register_live_secret(None)
    assert guard.redact("my password is hunter2") == "my password is hunter2"
