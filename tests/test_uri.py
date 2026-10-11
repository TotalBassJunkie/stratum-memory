import pytest

from stratum.uri import UriError, build_uri, parse_uri


def test_parse_roundtrip():
    u = parse_uri("stratum://docs/default/projects/stratum/architecture")
    assert u.bereich == "docs"
    assert u.tenant == "default"
    assert u.path == "projects/stratum/architecture"
    assert u.uri == "stratum://docs/default/projects/stratum/architecture"


def test_build_uri():
    assert build_uri("facts", "acme", "customer/42/plan-pro") == (
        "stratum://facts/acme/customer/42/plan-pro"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "docs/default/foo",  # no scheme
        "stratum://docs/default",  # missing path
        "stratum://nope/default/foo",  # bad bereich
        "stratum://docs/DEFAULT/foo",  # uppercase
        "stratum://docs/default/foo=bar",  # forbidden char
        "stratum://docs/default/..",
        "stratum://docs/default/a/b/../../../etc",
    ],
)
def test_invalid_uris(bad):
    with pytest.raises(UriError):
        parse_uri(bad)


def test_lowercase_w_only():
    # '2026-w41' is valid (lowercase), '2026-W41' is not.
    assert parse_uri("stratum://syntheses/default/2026-w41/report").path == "2026-w41/report"
    with pytest.raises(UriError):
        parse_uri("stratum://syntheses/default/2026-W41/report")
