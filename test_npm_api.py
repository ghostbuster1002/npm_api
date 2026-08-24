#!/usr/bin/env python3
"""Offline unit tests for npm_api.py.

Deliberately a separate file. npm-api ships as one self-contained module that
gets deployed by copy-pasting npm_api.py as text, so nothing here may become a
runtime dependency of the tool: these tests import npm_api, never the reverse.

Everything runs without a live NPM and without network. The API client is
exercised by subclassing NPMClient with a no-op __init__ (the real one creates
token and backup directories) and overriding only the few methods the code
under test calls. Anything touching disk goes into a tempfile directory that is
removed afterwards.

Run from the repo root:

    python3 -m pytest test_npm_api.py -q
"""

import io
import json
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

# The tool is a script, not an installed package, and some Python builds strip
# the working directory from sys.path. Anchor the import on this file's own
# directory so the suite runs the same way under pytest and unittest.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import npm_api  # noqa: E402


# =============================================================================
# Test doubles
# =============================================================================

_NO_JSON = object()


class _FakeResponse:
    """The slice of requests.Response that npm_api actually touches.

    A real Response cannot be constructed usefully without a transport, and
    building one through requests' adapters would be more machinery than the
    four attributes below.
    """

    def __init__(self, status_code=200, json_body=_NO_JSON, text=None, content=b""):
        self.status_code = status_code
        self._json_body = json_body
        self.content = content
        if text is not None:
            self.text = text
        elif json_body is not _NO_JSON:
            self.text = json.dumps(json_body)
        else:
            self.text = ""

    def json(self):
        # requests raises a ValueError subclass on an unparseable body, and
        # npm_api catches plain ValueError to cover it.
        if self._json_body is _NO_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class _StubClient(npm_api.NPMClient):
    """NPMClient with authentication, config and directory setup removed.

    Subclasses override only the handful of methods their test needs; anything
    else reaching the network is a test bug and should fail loudly, which it
    does — the unstubbed methods call self.get(), which is absent here.
    """

    def __init__(self):  # noqa: D107 - deliberately does not call super()
        pass


@pytest.fixture
def workdir():
    """A private directory, resolved so symlinked temp roots don't skew
    path-containment assertions, removed when the test ends."""
    with tempfile.TemporaryDirectory(prefix="npm_api_test_") as raw:
        yield Path(raw).resolve()


def _mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def _expires_in(delta):
    """An expires_on value relative to now, in the ISO form NPM emits with a
    trailing Z. Relative so the suite does not rot as real time passes."""
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# domain_prefix
# =============================================================================

class TestDomainPrefix:
    """The part of a name carried over when rebasing onto another domain."""

    def test_single_subdomain_label(self):
        assert npm_api.domain_prefix("ex.example.com") == "ex"

    def test_keeps_every_subdomain_label(self):
        # Regression: this took only the first label, so rebasing a host that
        # held sub.ex.example.com produced sub.example.net, dropping "ex".
        assert npm_api.domain_prefix("sub.ex.example.com") == "sub.ex"

    def test_deep_subdomain_keeps_all_labels(self):
        assert npm_api.domain_prefix("a.b.c.example.com") == "a.b.c"

    def test_apex_has_no_prefix(self):
        # None means "skip this one"; rebasing an apex would invent a label.
        assert npm_api.domain_prefix("example.com") is None

    def test_single_label_has_no_prefix(self):
        assert npm_api.domain_prefix("localhost") is None

    def test_wildcard_label_is_a_prefix_like_any_other(self):
        assert npm_api.domain_prefix("*.example.com") == "*"

    def test_trailing_dot_and_surrounding_space_ignored(self):
        assert npm_api.domain_prefix("  ex.example.com.  ") == "ex"

    def test_multipart_suffix_keeps_one_label_too_many(self):
        # Documented limitation, not a bug: the registrable base is assumed to
        # be two labels. Getting .co.uk right needs public-suffix data that
        # neither this tool nor NPM carries. Asserted so the behaviour is a
        # decision on record rather than a surprise.
        assert npm_api.domain_prefix("ex.example.co.uk") == "ex.example"


# =============================================================================
# dedupe_domains
# =============================================================================

class TestDedupeDomains:
    """Rewriting one base onto another can collide with a name the host
    already carries; NPM would otherwise store the same name twice."""

    def test_preserves_order_of_first_occurrence(self):
        assert npm_api.dedupe_domains(
            ["b.example.com", "a.example.com", "c.example.com"]
        ) == ["b.example.com", "a.example.com", "c.example.com"]

    def test_case_insensitive(self):
        assert npm_api.dedupe_domains(
            ["App.Example.com", "app.example.com", "APP.EXAMPLE.COM"]
        ) == ["App.Example.com"]

    def test_keeps_the_first_spelling_not_the_last(self):
        assert npm_api.dedupe_domains(
            ["APP.example.com", "app.example.com"]
        ) == ["APP.example.com"]

    def test_surrounding_whitespace_does_not_defeat_the_match(self):
        assert npm_api.dedupe_domains(
            [" app.example.com ", "app.example.com"]
        ) == [" app.example.com "]

    def test_drops_empty_and_whitespace_only_entries(self):
        assert npm_api.dedupe_domains(
            ["a.example.com", "", "   ", "b.example.com"]
        ) == ["a.example.com", "b.example.com"]

    def test_empty_input(self):
        assert npm_api.dedupe_domains([]) == []


# =============================================================================
# coerce_field_value
# =============================================================================

class TestCoerceFieldValue:
    """CLI "field=value" strings into the JSON types NPM expects."""

    @pytest.mark.parametrize("given", ["true", "True", "  TRUE  "])
    def test_true_in_any_case(self, given):
        assert npm_api.coerce_field_value("block_exploits", given) is True

    @pytest.mark.parametrize("given", ["false", "False", " FALSE "])
    def test_false_in_any_case(self, given):
        assert npm_api.coerce_field_value("block_exploits", given) is False

    @pytest.mark.parametrize("given", ["null", "none", "NULL", "None"])
    def test_null_spellings(self, given):
        assert npm_api.coerce_field_value("advanced_config", given) is None

    def test_json_array_literal(self):
        assert npm_api.coerce_field_value(
            "locations", '[{"path": "/api"}]'
        ) == [{"path": "/api"}]

    def test_json_object_literal(self):
        assert npm_api.coerce_field_value(
            "meta", '{"letsencrypt_agree": true}'
        ) == {"letsencrypt_agree": True}

    def test_leading_whitespace_still_reads_as_json(self):
        assert npm_api.coerce_field_value("locations", '  ["/api"]') == ["/api"]

    def test_json_literal_wins_over_comma_splitting_for_list_fields(self):
        assert npm_api.coerce_field_value(
            "domain_names", '["a.example.com", "b.example.com"]'
        ) == ["a.example.com", "b.example.com"]

    def test_malformed_json_names_the_field(self):
        with pytest.raises(ValueError, match="locations"):
            npm_api.coerce_field_value("locations", "[not json")

    def test_list_field_splits_on_commas_and_strips(self):
        assert npm_api.coerce_field_value(
            "domain_names", "a.example.com, b.example.com ,c.example.com"
        ) == ["a.example.com", "b.example.com", "c.example.com"]

    def test_list_field_drops_empty_segments(self):
        assert npm_api.coerce_field_value(
            "domain_names", "a.example.com,,b.example.com,"
        ) == ["a.example.com", "b.example.com"]

    def test_free_text_field_keeps_its_commas(self):
        # advanced_config is nginx config, not a list; splitting it would
        # corrupt every directive containing a comma.
        assert npm_api.coerce_field_value(
            "advanced_config", "add_header X-A a, b;"
        ) == "add_header X-A a, b;"

    def test_plain_integer(self):
        assert npm_api.coerce_field_value("forward_port", "8080") == 8080

    def test_negative_integer(self):
        assert npm_api.coerce_field_value("forward_port", "-1") == -1

    def test_malformed_numeric_stays_a_string(self):
        # Regression: an lstrip("-").isdigit() test accepted "--5" and then
        # int() raised an uncaught ValueError. Strict matching sends it on as
        # a string and lets NPM reject it.
        assert npm_api.coerce_field_value("forward_port", "--5") == "--5"

    @pytest.mark.parametrize("given", ["5.5", "1e3", "12abc", "0x10", "+5"])
    def test_other_non_integers_stay_strings(self, given):
        assert npm_api.coerce_field_value("forward_port", given) == given

    @pytest.mark.parametrize("field", sorted(npm_api.HOST_UNSET_ON_ZERO_FIELDS))
    def test_zero_clears_a_link_field(self, field):
        # 0 is NPM's "nothing linked" for these two, so `bulk-update
        # certificate_id 0` has to clear rather than point at host 0.
        assert npm_api.coerce_field_value(field, "0") is None

    def test_zero_is_a_real_value_everywhere_else(self):
        result = npm_api.coerce_field_value("forward_port", "0")
        assert result == 0 and isinstance(result, int)

    def test_nonzero_link_field_stays_an_integer(self):
        assert npm_api.coerce_field_value("certificate_id", "7") == 7

    def test_unrecognised_value_passes_through_unchanged(self):
        assert npm_api.coerce_field_value(
            "forward_host", "backend.internal.lan"
        ) == "backend.internal.lan"


# =============================================================================
# cert_covers_domain
# =============================================================================

class TestCertCoversDomain:
    """Three-valued on purpose: True, False, or None for "cannot tell"."""

    def test_exact_match(self):
        cert = {"domain_names": ["app.example.com"]}
        assert npm_api.cert_covers_domain(cert, "app.example.com") is True

    def test_match_is_case_and_trailing_dot_insensitive(self):
        cert = {"domain_names": ["App.Example.COM."]}
        assert npm_api.cert_covers_domain(cert, "  APP.example.com.  ") is True

    def test_non_matching_name(self):
        cert = {"domain_names": ["other.example.com"]}
        assert npm_api.cert_covers_domain(cert, "app.example.com") is False

    def test_wildcard_covers_one_label(self):
        cert = {"domain_names": ["*.example.com"]}
        assert npm_api.cert_covers_domain(cert, "app.example.com") is True

    def test_wildcard_does_not_cover_two_labels(self):
        # RFC 6125: *.example.com is not valid for app.eu.example.com. Getting
        # this wrong would attach a cert that browsers then reject.
        cert = {"domain_names": ["*.example.com"]}
        assert npm_api.cert_covers_domain(cert, "app.eu.example.com") is False

    def test_wildcard_does_not_cover_the_apex(self):
        cert = {"domain_names": ["*.example.com"]}
        assert npm_api.cert_covers_domain(cert, "example.com") is False

    def test_matches_any_entry_in_the_list(self):
        cert = {"domain_names": ["a.example.com", "*.internal.lan", "b.example.com"]}
        assert npm_api.cert_covers_domain(cert, "app.internal.lan") is True

    def test_unusable_metadata_is_unknown_not_absent(self):
        # NPM keeps domain_names as metadata only and never consults it when
        # serving TLS, so for uploaded certs it drifts. A recorded "*.internal,"
        # can belong to a cert that really does serve *.internal.lan; answering
        # False here would refuse a valid assignment.
        cert = {"domain_names": ["*.internal,"]}
        assert npm_api.cert_covers_domain(cert, "app.internal.lan") is None

    @pytest.mark.parametrize("entry", ["*.internal,", "a.example.com b.example.com",
                                       "a.example.com;b.example.com", "localhost", ""])
    def test_entries_without_a_usable_name_are_skipped(self, entry):
        cert = {"domain_names": [entry]}
        assert npm_api.cert_covers_domain(cert, "app.example.com") is None

    def test_empty_domain_list_is_unknown(self):
        assert npm_api.cert_covers_domain({"domain_names": []}, "app.example.com") is None

    def test_missing_domain_list_is_unknown(self):
        assert npm_api.cert_covers_domain({}, "app.example.com") is None

    def test_null_domain_list_is_unknown(self):
        assert npm_api.cert_covers_domain({"domain_names": None}, "app.example.com") is None

    def test_one_usable_entry_makes_the_answer_definite(self):
        # Junk alongside a real name is still a real answer: the usable entry
        # was checked and did not match.
        cert = {"domain_names": ["*.internal,", "other.example.com"]}
        assert npm_api.cert_covers_domain(cert, "app.example.com") is False


# =============================================================================
# cert_days_remaining / cert_status_label
# =============================================================================

class TestCertExpiry:
    """NPM sends no "expired" flag, so validity is derived from expires_on."""

    def test_future_expiry(self):
        cert = {"expires_on": _expires_in(timedelta(days=45, minutes=5))}
        assert npm_api.cert_days_remaining(cert) == 45

    def test_naive_timestamp_in_npms_own_format(self):
        # NPM's usual shape is "YYYY-MM-DD HH:MM:SS" with no zone; it must not
        # collide with an aware `now` and raise on the subtraction.
        naive = (datetime.now() + timedelta(days=10, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        assert npm_api.cert_days_remaining({"expires_on": naive}) == 10

    def test_expired_certificate_is_negative(self):
        cert = {"expires_on": _expires_in(timedelta(days=-3, minutes=-5))}
        assert npm_api.cert_days_remaining(cert) < 0

    def test_days_floor_toward_the_past(self):
        # timedelta.days floors, so a cert three days and a bit past its date
        # reports -4 and the label reads "EXPIRED 4d AGO" -- one day pessimistic.
        #
        # Deliberate, not a rounding bug to tidy up. Truncating toward zero
        # instead would read prettier but turn a certificate that expired an
        # hour ago into 0, and `days < 0` would then call it valid. Erring
        # toward "expired" is the safe direction for a TLS check; erring the
        # other way hands someone a dead certificate labelled fine.
        cert = {"expires_on": _expires_in(timedelta(days=-3, minutes=-5))}
        assert npm_api.cert_days_remaining(cert) == -4

    def test_a_certificate_expired_within_the_last_day_still_reads_expired(self):
        # The case that makes flooring the right choice above.
        cert = {"expires_on": _expires_in(timedelta(hours=-1))}
        assert npm_api.cert_days_remaining(cert) < 0
        assert "EXPIRED" in npm_api.cert_status_label(cert)

    def test_missing_expires_on(self):
        assert npm_api.cert_days_remaining({}) is None

    def test_empty_expires_on(self):
        assert npm_api.cert_days_remaining({"expires_on": ""}) is None

    def test_unparseable_expires_on(self):
        assert npm_api.cert_days_remaining({"expires_on": "not-a-date"}) is None

    def test_valid_label(self):
        cert = {"expires_on": _expires_in(timedelta(days=45, minutes=5))}
        assert "VALID" in npm_api.cert_status_label(cert)

    def test_warning_label_inside_the_window(self):
        cert = {"expires_on": _expires_in(timedelta(days=7, minutes=5))}
        assert "7d LEFT" in npm_api.cert_status_label(cert)

    def test_warning_window_boundary_is_inclusive(self):
        cert = {"expires_on": _expires_in(
            timedelta(days=npm_api.CERT_EXPIRY_WARN_DAYS, minutes=5))}
        label = npm_api.cert_status_label(cert)
        assert "LEFT" in label and "VALID" not in label

    def test_one_day_past_the_window_is_valid(self):
        cert = {"expires_on": _expires_in(
            timedelta(days=npm_api.CERT_EXPIRY_WARN_DAYS + 1, minutes=5))}
        assert "VALID" in npm_api.cert_status_label(cert)

    def test_expired_label_reports_age(self):
        cert = {"expires_on": _expires_in(timedelta(days=-10, minutes=-5))}
        label = npm_api.cert_status_label(cert)
        assert "EXPIRED" in label and "11d AGO" in label

    def test_unknown_label_is_not_a_failure_claim(self):
        # An unreadable date must not render as expired; that would push
        # someone into regenerating a working certificate.
        for cert in ({}, {"expires_on": "not-a-date"}):
            label = npm_api.cert_status_label(cert)
            assert "UNKNOWN" in label and "EXPIRED" not in label


# =============================================================================
# host_config_payload
# =============================================================================

def _host_fixture():
    """A host as NPM returns it with expansions requested."""
    return {
        "id": 12,
        "created_on": "2026-01-01 00:00:00",
        "modified_on": "2026-02-01 00:00:00",
        "owner_user_id": 1,
        "domain_names": ["app.example.com"],
        "forward_host": "10.0.0.5",
        "forward_port": 8080,
        "certificate_id": 4,
        "access_list_id": 2,
        "enabled": True,
        "trust_forwarded_proto": True,
        "meta": {
            "nginx_online": True,
            "nginx_err": None,
            "letsencrypt_agree": True,
        },
        # Objects NPM expands alongside their *_id counterparts
        "certificate": {"id": 4, "nice_name": "example.com"},
        "owner": {"id": 1, "email": "admin@example.com"},
        "access_list": {"id": 2, "name": "internal"},
    }


class TestHostConfigPayload:
    """Copy-by-exclusion: whatever NPM sends is written back untouched unless
    it is explicitly known to be unwritable."""

    def test_strips_server_assigned_fields(self):
        payload = npm_api.host_config_payload(_host_fixture())
        for key in ("id", "created_on", "modified_on", "owner_user_id"):
            assert key not in payload

    def test_strips_expanded_objects_but_keeps_their_ids(self):
        # Echoing the expanded object back sends a nested object where the API
        # wants an integer, and the write fails.
        payload = npm_api.host_config_payload(_host_fixture())
        for key in ("certificate", "owner", "access_list"):
            assert key not in payload
        assert payload["certificate_id"] == 4
        assert payload["access_list_id"] == 2

    def test_readonly_set_is_fully_covered(self):
        payload = npm_api.host_config_payload(_host_fixture())
        assert not (npm_api.HOST_READONLY_FIELDS & payload.keys())

    def test_strips_runtime_meta_but_keeps_configuration_meta(self):
        payload = npm_api.host_config_payload(_host_fixture())
        assert payload["meta"] == {"letsencrypt_agree": True}

    def test_meta_absent_becomes_empty_dict(self):
        payload = npm_api.host_config_payload({"domain_names": ["app.example.com"]})
        assert payload["meta"] == {}

    def test_meta_null_becomes_empty_dict(self):
        payload = npm_api.host_config_payload({"meta": None})
        assert payload["meta"] == {}

    def test_preserves_fields_this_script_has_never_heard_of(self):
        # The whole point of copying by exclusion: an allowlist written against
        # an older NPM silently reset trust_forwarded_proto when 2.15 added it.
        host = _host_fixture()
        host["some_future_npm_field"] = {"nested": ["value"]}
        payload = npm_api.host_config_payload(host)
        assert payload["some_future_npm_field"] == {"nested": ["value"]}
        assert payload["trust_forwarded_proto"] is True

    def test_overrides_replace_existing_values(self):
        payload = npm_api.host_config_payload(
            _host_fixture(), {"forward_port": 9090, "enabled": False}
        )
        assert payload["forward_port"] == 9090
        assert payload["enabled"] is False

    def test_overrides_can_add_and_null_fields(self):
        payload = npm_api.host_config_payload(
            _host_fixture(), {"certificate_id": None, "brand_new": "x"}
        )
        assert payload["certificate_id"] is None
        assert payload["brand_new"] == "x"

    def test_source_host_is_not_mutated(self):
        # Callers reuse the fetched host afterwards, e.g. to print a summary.
        host = _host_fixture()
        npm_api.host_config_payload(host, {"forward_port": 9090})
        assert host["id"] == 12
        assert host["forward_port"] == 8080
        assert host["meta"]["nginx_online"] is True


# =============================================================================
# format_http_error
# =============================================================================

class TestFormatHttpError:
    """requests stringifies an HTTPError as "400 Client Error: Bad Request for
    url: ...", which buries the reason NPM actually gave."""

    def test_npm_error_object(self):
        response = _FakeResponse(400, json_body={"error": {"message": "Domain already in use"}})
        exc = requests.HTTPError("400 Client Error", response=response)
        assert npm_api.format_http_error(exc) == "HTTP 400: Domain already in use"

    def test_error_given_as_a_bare_string(self):
        response = _FakeResponse(403, json_body={"error": "Forbidden"})
        exc = requests.HTTPError("403", response=response)
        assert npm_api.format_http_error(exc) == "HTTP 403: Forbidden"

    def test_json_body_in_some_other_shape(self):
        response = _FakeResponse(422, json_body={"detail": "bad input"})
        exc = requests.HTTPError("422", response=response)
        result = npm_api.format_http_error(exc)
        assert result.startswith("HTTP 422: ") and "bad input" in result

    def test_non_json_body_is_reported_verbatim(self):
        # A reverse proxy in front of NPM answers with HTML, not NPM's JSON.
        response = _FakeResponse(502, text="<html><body>Bad Gateway</body></html>")
        exc = requests.HTTPError("502", response=response)
        assert npm_api.format_http_error(exc) == \
            "HTTP 502: <html><body>Bad Gateway</body></html>"

    def test_long_non_json_body_is_truncated(self):
        response = _FakeResponse(502, text="x" * 5000)
        exc = requests.HTTPError("502", response=response)
        assert npm_api.format_http_error(exc) == "HTTP 502: " + "x" * 200

    def test_empty_non_json_body_gives_the_status_alone(self):
        response = _FakeResponse(500, text="   ")
        exc = requests.HTTPError("500", response=response)
        assert npm_api.format_http_error(exc) == "HTTP 500"

    def test_exception_with_no_response_falls_back_to_its_own_message(self):
        # Connection errors never reach a status code.
        exc = requests.ConnectionError("Connection refused")
        assert npm_api.format_http_error(exc) == "Connection refused"

    def test_plain_exception(self):
        assert npm_api.format_http_error(ValueError("boom")) == "boom"


# =============================================================================
# write_secret
# =============================================================================

class TestWriteSecret:
    """Private keys and API tokens must never exist world-readable, not even
    for the instant between write_text() and chmod()."""

    def test_creates_owner_only_file_with_the_content(self, workdir):
        path = npm_api.write_secret(workdir / "token.txt", "s3cret-token")
        assert path.read_text() == "s3cret-token"
        assert _mode(path) == 0o600

    def test_overwriting_a_world_readable_file_tightens_the_mode(self, workdir):
        # O_CREAT leaves an existing file's mode alone, so write_secret has to
        # unlink first. Without that, a token file created by an older version
        # would stay 0644 forever.
        path = workdir / "token.txt"
        path.write_text("old")
        path.chmod(0o644)

        npm_api.write_secret(path, "new")

        assert path.read_text() == "new"
        assert _mode(path) == 0o600

    def test_replaces_a_symlink_instead_of_writing_through_it(self, workdir):
        # Writing through the link would spray the secret into whatever the
        # link points at, and leave that file's permissive mode in place.
        target = workdir / "innocent.txt"
        target.write_text("untouched")
        link = workdir / "token.txt"
        link.symlink_to(target)

        npm_api.write_secret(link, "s3cret-token")

        assert target.read_text() == "untouched"
        assert not link.is_symlink()
        assert link.read_text() == "s3cret-token"
        assert _mode(link) == 0o600


# =============================================================================
# download_certificate
# =============================================================================

def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buf.getvalue()


class _JsonRouteClient(_StubClient):
    """NPM's preferred route: PEM bodies returned as JSON."""

    def __init__(self, body):
        self._body = body

    def get(self, endpoint, **kwargs):
        assert endpoint.endswith("/certificates")
        return _FakeResponse(200, json_body=self._body)

    def get_certificate(self, cert_id):
        return {"id": cert_id, "nice_name": "example.com"}


class _ZipRouteClient(_StubClient):
    """JSON route unavailable, so the legacy ZIP endpoint is used."""

    def __init__(self, zip_bytes):
        self._zip = zip_bytes

    def get(self, endpoint, **kwargs):
        if endpoint.endswith("/download"):
            return _FakeResponse(200, content=self._zip)
        return _FakeResponse(404)  # force the fallback


class TestDownloadCertificate:

    def test_json_route_writes_key_at_owner_only_mode(self, workdir):
        client = _JsonRouteClient({
            "certificate": "-----BEGIN CERTIFICATE-----\nleaf\n",
            "private": "-----BEGIN PRIVATE KEY-----\nkey\n",
            "intermediate": "-----BEGIN CERTIFICATE-----\nchain\n",
        })

        written = client.download_certificate(1, str(workdir), "example.com")

        names = {p.name for p in written}
        assert names == {"example.com.key", "example.com.crt",
                         "example.com.chain.crt", "example.com_metadata.json"}
        assert _mode(workdir / "example.com.key") == 0o600

    def test_json_route_with_empty_key_material_is_a_failure(self, workdir):
        # NPM answers 200 with empty bodies for certs whose key it does not
        # hold. Writing an empty .key reads as a successful backup of an
        # unusable key, so it has to raise instead.
        client = _ZipRouteClient(_zip_bytes({}))
        client.get = lambda endpoint, **kw: (
            _FakeResponse(200, json_body={"certificate": "", "private": ""})
            if endpoint.endswith("/certificates") else _FakeResponse(404)
        )

        with pytest.raises(npm_api.CertificateDownloadError, match="no key material"):
            client.download_certificate(1, str(workdir), "example.com")

        assert list(workdir.iterdir()) == []

    def test_zip_member_escaping_into_a_prefix_sibling_is_rejected(self, workdir):
        # Regression, and the exact case the old guard got wrong: it compared
        # resolved paths with str.startswith, so ".../out-evil/pwned.txt"
        # passed the ".../out" prefix test and extracted outside the target.
        out = workdir / "out"
        out.mkdir()
        sibling = workdir / "out-evil"
        sibling.mkdir()

        client = _ZipRouteClient(_zip_bytes({"../out-evil/pwned.txt": "owned"}))

        with pytest.raises(npm_api.CertificateDownloadError) as caught:
            client.download_certificate(1, str(out), "example.com")

        assert "skipped unsafe path" in str(caught.value)
        assert list(sibling.iterdir()) == []
        assert not (out / "example.com.download.zip").exists()  # temp zip cleaned up

    def test_safe_members_survive_alongside_a_rejected_one(self, workdir):
        out = workdir / "out"
        out.mkdir()
        sibling = workdir / "out-evil"
        sibling.mkdir()

        client = _ZipRouteClient(_zip_bytes({
            "../out-evil/pwned.txt": "owned",
            "fullchain.pem": "-----BEGIN CERTIFICATE-----\n",
        }))

        written = client.download_certificate(1, str(out), "example.com")

        assert [p.name for p in written] == ["fullchain.pem"]
        assert (out / "fullchain.pem").exists()
        assert list(sibling.iterdir()) == []

    def test_extracted_key_material_is_chmodded(self, workdir):
        # The archive's stored mode is whatever NPM chose; keys taken from it
        # used never to be tightened at all.
        client = _ZipRouteClient(_zip_bytes({"privkey.pem": "-----BEGIN PRIVATE KEY-----\n"}))

        client.download_certificate(1, str(workdir), "example.com")

        assert _mode(workdir / "privkey.pem") == 0o600

    def test_both_routes_failing_names_each_attempt(self, workdir):
        client = _ZipRouteClient(b"")
        client.get = lambda endpoint, **kw: _FakeResponse(404)

        with pytest.raises(npm_api.CertificateDownloadError) as caught:
            client.download_certificate(9, str(workdir), "example.com")

        message = str(caught.value)
        assert "certificate 9" in message
        assert "JSON route" in message and "ZIP route" in message

    def test_certificate_name_is_sanitised_into_the_output_directory(self, workdir):
        # Defense in depth: the name comes from NPM, not from the user, but it
        # still lands in a filename.
        client = _JsonRouteClient({"certificate": "leaf", "private": "key"})

        written = client.download_certificate(1, str(workdir), "../../etc/passwd")

        for path in written:
            assert path.parent == workdir


# =============================================================================
# get_dashboard_stats
# =============================================================================

class _DashboardClient(_StubClient):
    """Half the dashboard's sections are readable, half are not."""

    def list_hosts(self):
        return [{"enabled": True}, {"enabled": True}, {"enabled": False}]

    def list_certificates(self):
        raise npm_api.NPMError("Cannot reach NPM at http://10.0.0.5:81/api")

    def get(self, endpoint, **kwargs):
        if endpoint == "/nginx/redirection-hosts":
            return _FakeResponse(200, json_body=[{"id": 1}, {"id": 2}])
        if endpoint == "/nginx/streams":
            raise requests.ConnectionError("stream endpoint unavailable")
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def list_users(self):
        return [{"id": 1}]

    def list_access_lists(self):
        raise requests.HTTPError(
            "500", response=_FakeResponse(500, json_body={"error": {"message": "db locked"}})
        )


class _TotallyBrokenClient(_StubClient):
    """Every section fails.

    _DashboardClient leaves proxy_hosts and users succeeding, which hides a
    whole class of regression: those two are only ever set inside their try
    block, so on failure they fall through to whatever the initial dict holds.
    Seeding that dict with 0 instead of None would restore the original bug
    -- a sick NPM reporting "0 proxy hosts" -- and every other test would
    still pass.
    """

    def list_hosts(self):
        raise requests.ConnectionError("Connection refused")

    def list_certificates(self):
        raise requests.ConnectionError("Connection refused")

    def get(self, endpoint, **kwargs):
        raise requests.ConnectionError("Connection refused")

    def list_users(self):
        raise requests.ConnectionError("Connection refused")

    def list_access_lists(self):
        raise requests.ConnectionError("Connection refused")


class _HealthyEmptyClient(_StubClient):
    """A working NPM that genuinely has nothing configured."""

    def list_hosts(self):
        return []

    def list_certificates(self):
        return []

    def get(self, endpoint, **kwargs):
        return _FakeResponse(200, json_body=[])

    def list_users(self):
        return []

    def list_access_lists(self):
        return []


class TestDashboardStats:
    """A failed section reports None, never 0 — "0 proxy hosts" used to read
    as a fact when it actually meant the request had failed."""

    def test_working_sections_report_real_counts(self):
        stats = _DashboardClient().get_dashboard_stats()
        assert stats["proxy_hosts"] == {"total": 3, "enabled": 2, "disabled": 1}
        assert stats["redirections"] == 2
        assert stats["users"] == 1

    def test_failed_sections_report_none(self):
        stats = _DashboardClient().get_dashboard_stats()
        assert stats["certificates"] == {"total": None, "valid": None, "expired": None}
        assert stats["streams"] is None
        assert stats["access_lists"] is None

    def test_every_section_failing_reports_none_never_zero(self):
        stats = _TotallyBrokenClient().get_dashboard_stats()

        assert stats["proxy_hosts"] == {"total": None, "enabled": None, "disabled": None}
        assert stats["certificates"] == {"total": None, "valid": None, "expired": None}
        for section in ("redirections", "streams", "users", "access_lists"):
            assert stats[section] is None, f"{section} reported {stats[section]!r}, not None"
        assert len(stats["failures"]) == 6

    def test_one_failure_entry_per_failed_section(self):
        stats = _DashboardClient().get_dashboard_stats()
        assert len(stats["failures"]) == 3
        joined = " | ".join(stats["failures"])
        assert "certificates" in joined
        assert "streams" in joined
        assert "access lists" in joined

    def test_failure_text_carries_npms_own_message(self):
        stats = _DashboardClient().get_dashboard_stats()
        assert any("db locked" in f for f in stats["failures"])

    def test_an_empty_but_healthy_npm_reports_zeros(self):
        stats = _HealthyEmptyClient().get_dashboard_stats()
        assert stats["failures"] == []
        assert stats["proxy_hosts"] == {"total": 0, "enabled": 0, "disabled": 0}
        assert stats["users"] == 0
        assert stats["streams"] == 0

    def test_expired_certificates_are_counted_from_expires_on(self):
        class _CertClient(_HealthyEmptyClient):
            def list_certificates(self):
                return [
                    {"id": 1, "expires_on": _expires_in(timedelta(days=45, minutes=5))},
                    {"id": 2, "expires_on": _expires_in(timedelta(days=-5, minutes=-5))},
                    {"id": 3, "expires_on": None},  # unreadable counts as valid, not expired
                ]

        stats = _CertClient().get_dashboard_stats()
        assert stats["certificates"] == {"total": 3, "valid": 2, "expired": 1}


# =============================================================================
# full_backup
# =============================================================================

_LETSENCRYPT_CERT = {"id": 1, "nice_name": "app.example.com", "provider": "letsencrypt"}
_UPLOADED_CERT = {"id": 2, "nice_name": "internal.lan", "provider": "other"}


class _BackupClient(_StubClient):
    """A small fixed inventory. Cert 1 exports its key; cert 2 is an uploaded
    certificate whose key NPM will not hand over."""

    def __init__(self):
        self.downloaded = []

    def list_users(self):
        return [{"id": 1, "email": "admin@example.com"}]

    def get(self, endpoint, **kwargs):
        if endpoint == "/settings":
            return _FakeResponse(200, json_body=[{"id": "default-site", "value": "congratulations"}])
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def list_access_lists(self):
        return [{"id": 7, "name": "internal"}]

    def get_access_list(self, list_id):
        return {"id": list_id, "name": "internal", "items": []}

    def list_hosts(self):
        return [{"id": 12, "domain_names": ["app.example.com"], "forward_host": "10.0.0.5"}]

    def list_certificates(self):
        return [dict(_LETSENCRYPT_CERT), dict(_UPLOADED_CERT)]

    def download_certificate(self, cert_id, output_dir, cert_name):
        self.downloaded.append(cert_id)
        if cert_id == _UPLOADED_CERT["id"]:
            raise npm_api.CertificateDownloadError(
                f"certificate {cert_id}: JSON route: response carried no key material"
            )
        # Mirror what the real implementation writes, including the mode.
        out = Path(output_dir)
        key = npm_api.write_secret(out / f"{cert_name}.key", "-----BEGIN PRIVATE KEY-----\n")
        crt = out / f"{cert_name}.crt"
        crt.write_text("-----BEGIN CERTIFICATE-----\n")
        return [key, crt]


class TestFullBackup:

    def test_without_keys_no_key_material_is_written_anywhere(self, workdir):
        client = _BackupClient()

        result = client.full_backup(str(workdir), include_keys=False)

        assert list(workdir.rglob("*.key")) == []
        assert client.downloaded == []  # not even attempted
        assert result.complete and result.failures == [] and result.key_failures == []

    def test_without_keys_metadata_is_still_captured(self, workdir):
        client = _BackupClient()

        client.full_backup(str(workdir), include_keys=False)

        assert (workdir / ".ssl" / "app.example.com" / "certificate_meta.json").exists()
        assert (workdir / ".Proxy_Hosts" / "app.example.com" / "proxy_config.json").exists()
        assert (workdir / "full_config_latest.json").is_symlink()

        full_config = json.loads((workdir / "full_config_latest.json").read_text())
        assert set(full_config) == {"users", "settings", "access_lists",
                                    "proxy_hosts", "certificates"}

    def test_with_keys_the_key_lands_owner_readable_only(self, workdir):
        client = _BackupClient()

        client.full_backup(str(workdir), include_keys=True)

        keys = list(workdir.rglob("*.key"))
        assert len(keys) == 1
        assert _mode(keys[0]) == 0o600

    def test_an_unexportable_certificate_is_not_a_backup_failure(self, workdir):
        # Uploaded certificates fail here on every single run; treating that as
        # fatal would make every scheduled backup exit non-zero.
        client = _BackupClient()

        result = client.full_backup(str(workdir), include_keys=True)

        assert result.failures == []
        assert result.complete is True

    def test_an_unexportable_certificate_is_reported_as_a_key_failure(self, workdir):
        client = _BackupClient()

        result = client.full_backup(str(workdir), include_keys=True)

        assert len(result.key_failures) == 1
        failure = result.key_failures[0]
        assert failure.cert_id == _UPLOADED_CERT["id"]
        assert failure.name == _UPLOADED_CERT["nice_name"]
        assert failure.provider == "other"
        assert "no key material" in failure.reason

    def test_a_failing_section_marks_the_backup_incomplete(self, workdir):
        # A scheduled run has to be able to fail loudly rather than exit 0 over
        # a half-written backup.
        class _BrokenHostsClient(_BackupClient):
            def list_hosts(self):
                raise requests.HTTPError("500 Server Error")

        result = _BrokenHostsClient().full_backup(str(workdir), include_keys=False)

        assert result.complete is False
        assert len(result.failures) == 1
        assert "proxy hosts" in result.failures[0]

    def test_a_failing_section_does_not_stop_the_others(self, workdir):
        class _BrokenHostsClient(_BackupClient):
            def list_hosts(self):
                raise requests.HTTPError("500 Server Error")

        _BrokenHostsClient().full_backup(str(workdir), include_keys=False)

        assert list((workdir / ".user").glob("users_*.json"))
        assert list((workdir / ".access_lists").glob("access_lists_*.json"))

    def test_result_path_points_at_the_output_directory(self, workdir):
        result = _BackupClient().full_backup(str(workdir), include_keys=False)
        assert Path(result.path) == workdir

    def test_a_stale_latest_symlink_is_replaced(self, workdir):
        # exists() follows the link, so a symlink pointing at a pruned backup
        # read as absent and symlink_to() then raised FileExistsError.
        (workdir / "full_config_latest.json").symlink_to("full_config_pruned.json")

        _BackupClient().full_backup(str(workdir), include_keys=False)

        latest = workdir / "full_config_latest.json"
        assert latest.is_symlink() and latest.exists()


# =============================================================================
# CertKeyFailure.container_paths
# =============================================================================

class TestCertKeyFailurePaths:
    """The remedy printed to the user is a docker cp command, so the path has
    to be right — npm-api speaks HTTP and cannot look inside the container."""

    def _failure(self, provider):
        return npm_api.CertKeyFailure(
            cert_id=42, name="app.example.com", provider=provider,
            reason="response carried no key material",
        )

    def test_letsencrypt_points_only_at_the_issued_location(self):
        assert self._failure("letsencrypt").container_paths == \
            ["/etc/letsencrypt/live/npm-42"]

    def test_another_provider_points_only_at_the_upload_location(self):
        assert self._failure("other").container_paths == ["/data/custom_ssl/npm-42"]

    def test_missing_provider_offers_both_rather_than_guessing(self):
        # NPM is under no obligation to send the field, and one confident wrong
        # path is worse than two candidates.
        assert self._failure(None).container_paths == \
            ["/data/custom_ssl/npm-42", "/etc/letsencrypt/live/npm-42"]

    def test_string_form_identifies_the_certificate(self):
        assert str(self._failure(None)) == \
            "certificate 42 (app.example.com): response carried no key material"


# =============================================================================
# BackupResult
# =============================================================================

class TestBackupResult:

    def test_complete_when_nothing_failed(self):
        assert npm_api.BackupResult(path="/tmp/backup").complete is True

    def test_key_failures_alone_do_not_make_it_incomplete(self):
        result = npm_api.BackupResult(
            path="/tmp/backup",
            key_failures=[npm_api.CertKeyFailure(1, "app.example.com", "other", "no key")],
        )
        assert result.complete is True

    def test_section_failures_make_it_incomplete(self):
        assert npm_api.BackupResult(path="/tmp/backup", failures=["users: 500"]).complete is False


if __name__ == "__main__":
    # Also runnable without pytest installed as a console script entry point.
    sys.exit(pytest.main([__file__, "-q"]))
