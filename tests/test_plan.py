import hashlib

import pytest

from pulsar.app.core.account import normalize_alias
from pulsar.app.core.channels.contract import MediaRef
from pulsar.app.core.publishing import Plan
from pulsar.internal.errors import PulsarError

MEDIA = {
    "hero.png": b"png-bytes-1",
    "other.png": b"png-bytes-2",
    "copy-of-hero.png": b"png-bytes-1",
}


def _sha(ref: MediaRef) -> str:
    return hashlib.sha256(MEDIA[ref.path]).hexdigest()


def digest(source: str | dict) -> str:
    plan = Plan.from_yaml(source) if isinstance(source, str) else Plan.from_mapping(source)
    return plan.digest(_sha)


BLOCK = """
account: x:constworks
posts:
  - text: |
      Orbit v0.25 is out.
    media:
      - path: hero.png
        alt: The release banner
  - text: Second post of the thread
reply_to: "1790000000000000000"
not_before: 2026-10-01T16:00:00Z
"""

FLOW = (
    '{"reply_to": "1790000000000000000", "posts": [{"media": [{"alt": "The release banner", '
    '"path": "hero.png"}], "text": "Orbit v0.25 is out."}, {"text": "Second post of the thread"}],'
    ' "account": "X:@ConstWorks"}'
)


def test_formatting_key_order_and_alias_spelling_do_not_change_the_digest():
    assert digest(BLOCK) == digest(FLOW)
    assert digest(BLOCK).startswith("sha256:")


def test_not_before_and_media_path_are_not_content():
    moved = BLOCK.replace("2026-10-01T16:00:00Z", "2026-10-02T09:00:00+02:00")
    renamed = BLOCK.replace("path: hero.png", "path: copy-of-hero.png")
    assert digest(moved) == digest(BLOCK)
    assert digest(renamed) == digest(BLOCK)


@pytest.mark.parametrize(
    "change",
    [
        ("Orbit v0.25 is out.", "Orbit v0.26 is out."),
        ("path: hero.png", "path: other.png"),  # different bytes
        ("The release banner", "The launch banner"),
        ("x:constworks", "x:danieljhk1"),
        ('reply_to: "1790000000000000000"', 'quote: "1790000000000000000"'),
        ("  - text: Second post of the thread\n", ""),
    ],
)
def test_anything_published_changes_the_digest(change):
    before, after = change
    assert before in BLOCK
    assert digest(BLOCK.replace(before, after)) != digest(BLOCK)


def test_single_post_shorthand_is_a_thread_of_one():
    short = {"account": "x:constworks", "text": "hi"}
    long = {"account": "x:constworks", "posts": [{"text": "hi"}]}
    assert digest(short) == digest(long)
    assert len(Plan.from_mapping(short).posts) == 1


def test_accounts_are_a_set():
    a = {"accounts": ["x:b", "x:a", "X:@A"], "text": "hi"}
    b = {"accounts": ["x:a", "x:b"], "text": "hi"}
    assert Plan.from_mapping(a).accounts == ("x:a", "x:b")
    assert digest(a) == digest(b)


def test_text_is_normalised_before_it_is_digested_or_posted():
    plan = Plan.from_mapping({"text": "  Café\r\nline two\n\n"})
    assert plan.posts[0].text == "Café\nline two"
    assert digest({"text": "Café\nline two"}) == digest({"text": "  Café\r\nline two\n"})


def test_variants_replace_posts_per_provider_and_are_digested():
    plan = Plan.from_mapping({"text": "long X copy", "variants": {"BSKY": {"text": "short copy"}}})
    assert plan.posts_for("x")[0].text == "long X copy"
    assert plan.posts_for("bsky")[0].text == "short copy"
    assert digest({"text": "long X copy"}) != plan.digest(_sha)


def test_media_refs_cover_variants():
    plan = Plan.from_mapping(
        {
            "text": "a",
            "media": [{"path": "hero.png", "alt": "x"}],
            "variants": {"bsky": {"text": "b", "media": [{"path": "other.png", "alt": "y"}]}},
        }
    )
    assert [m.path for m in plan.media_refs()] == ["hero.png", "other.png"]


@pytest.mark.parametrize(
    ("plan", "needle"),
    [
        ([], "mapping"),
        ({}, "no posts"),
        ({"text": "a", "posts": [{"text": "b"}]}, "not both"),
        ({"posts": []}, "must not be empty"),
        ({"posts": [{"text": "  "}]}, "empty"),
        ({"text": True}, "must be a string"),
        ({"text": "a", "media": [{"path": "hero.png"}]}, "alt"),
        ({"text": "a", "media": [{"path": "hero.png", "alt": "   "}]}, "alt"),
        ({"text": "a", "reply_to": "1", "quote": "2"}, "both reply and quote"),
        ({"text": "a", "account": "constworks"}, "provider:handle"),
        ({"text": "a", "account": "x:a", "accounts": ["x:b"]}, "not both"),
        ({"text": "a", "not_before": "2026-10-01T16:00:00"}, "timezone"),
        ({"text": "a", "not_before": "tomorrow"}, "ISO 8601"),
        ({"text": "a", "sheduled": "x"}, "unknown keys"),
        ({"text": "a", "variants": {"x": {"txt": "b"}}}, "unknown keys"),
        ({"posts": [{"text": str(i)} for i in range(26)]}, "at most 25"),
    ],
)
def test_malformed_plans_are_invalid_plan(plan, needle):
    with pytest.raises(PulsarError) as exc:
        Plan.from_mapping(plan)
    assert exc.value.code == "invalid_plan"
    assert needle in exc.value.message


def test_bad_yaml_is_invalid_plan():
    with pytest.raises(PulsarError) as exc:
        Plan.from_yaml("posts: [unclosed")
    assert exc.value.code == "invalid_plan"


def test_yaml_booleans_are_not_silently_text():
    with pytest.raises(PulsarError):
        Plan.from_yaml("text: no")


@pytest.mark.parametrize(
    ("raw", "alias"),
    [("x:constworks", "x:constworks"), ("X:@ConstWorks", "x:constworks"), (" x : a ", "x:a")],
)
def test_alias_normalisation(raw, alias):
    assert normalize_alias(raw) == alias
