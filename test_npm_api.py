#!/usr/bin/env python3
"""Offline unit tests for npm_api.py.

Deliberately a separate file. npm-api ships as one self-contained module that
gets deployed by copy-pasting npm_api.py as text, so nothing here may become a
runtime dependency of the tool: these tests import npm_api, never the reverse.

Standard library only, on the same principle. The suite runs on unittest rather
than pytest so that a machine that can run npm-api can also run its tests: the
only third-party import here is requests, which npm_api.py already requires.

Everything runs without a live NPM and without network. The API client is
exercised by subclassing NPMClient with a no-op __init__ (the real one creates
token and backup directories) and overriding only the few methods the code
under test calls. Anything touching disk goes into a tempfile directory that is
removed afterwards.

Run from the repo root:

    python3 -m unittest discover -v

or run this file directly:

    python3 test_npm_api.py
"""

import io
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

# The tool is a script, not an installed package, and some Python builds strip
# the working directory from sys.path. Anchor the import on this file's own
# directory so the suite runs the same way under `unittest discover` and when
# this file is executed directly.
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


class _WorkdirTestCase(unittest.TestCase):
    """Base class for the tests that touch disk.

    Gives each test a private directory, resolved so symlinked temp roots don't
    skew path-containment assertions, removed when the test ends. addCleanup
    rather than tearDown so the removal still happens if setUp itself is
    extended later and fails partway.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="npm_api_test_")
        self.addCleanup(tmp.cleanup)
        self.workdir = Path(tmp.name).resolve()


class _RecordingConsole:
    """Stand-in for a Rich Console that keeps the markup instead of rendering it.

    The bulk helpers decide a great deal that no return value exposes — which
    IDs were missing, whether certificate coverage could be checked at all,
    which host a write failed on — and say it only in print(). Substituting the
    console object beats capturing the stream: nothing is rendered, so an
    assertion cannot be broken by terminal width wrapping a long hostname or by
    Rich swallowing markup, and console.status() stays available.
    """

    def __init__(self):
        self.lines = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    # apply_domain_changes and bulk-update run their loop inside
    # `with console.status(...) as status:` and call status.update() per host.
    # Both are spinner chrome rather than output, so they record nothing.
    def status(self, *args, **kwargs):
        return self

    def update(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    @property
    def text(self):
        return "\n".join(self.lines)


class _ConsoleTestCase(unittest.TestCase):
    """Base class for the tests that read what was printed.

    npm_api reports on two consoles by design: `console` writes to stderr and
    carries every diagnostic, `out_console` writes to stdout and carries only
    --json payloads, so a piped `--json` run stays machine-readable. Everything
    in the bulk infrastructure prints on the stderr one, which is what this
    patches.
    """

    def setUp(self):
        super().setUp()
        self.console = _RecordingConsole()
        patcher = mock.patch.object(npm_api, "console", self.console)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertPrinted(self, needle):
        self.assertIn(needle, self.console.text,
                      msg=f"nothing printed contained {needle!r}:\n{self.console.text}")

    def assertNotPrinted(self, needle):
        self.assertNotIn(needle, self.console.text,
                         msg=f"unexpectedly printed {needle!r}:\n{self.console.text}")


def _mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def _expires_in(delta):
    """An expires_on value relative to now, in the ISO form NPM emits with a
    trailing Z. Relative so the suite does not rot as real time passes."""
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# domain_prefix
# =============================================================================

class TestDomainPrefix(unittest.TestCase):
    """The part of a name carried over when rebasing onto another domain."""

    def test_single_subdomain_label(self):
        self.assertEqual(npm_api.domain_prefix("ex.example.com"), "ex")

    def test_keeps_every_subdomain_label(self):
        # Regression: this took only the first label, so rebasing a host that
        # held sub.ex.example.com produced sub.example.net, dropping "ex".
        self.assertEqual(npm_api.domain_prefix("sub.ex.example.com"), "sub.ex")

    def test_deep_subdomain_keeps_all_labels(self):
        self.assertEqual(npm_api.domain_prefix("a.b.c.example.com"), "a.b.c")

    def test_apex_has_no_prefix(self):
        # None means "skip this one"; rebasing an apex would invent a label.
        self.assertIsNone(npm_api.domain_prefix("example.com"))

    def test_single_label_has_no_prefix(self):
        self.assertIsNone(npm_api.domain_prefix("localhost"))

    def test_wildcard_label_is_a_prefix_like_any_other(self):
        self.assertEqual(npm_api.domain_prefix("*.example.com"), "*")

    def test_trailing_dot_and_surrounding_space_ignored(self):
        self.assertEqual(npm_api.domain_prefix("  ex.example.com.  "), "ex")

    def test_multipart_suffix_keeps_one_label_too_many(self):
        # Documented limitation, not a bug: the registrable base is assumed to
        # be two labels. Getting .co.uk right needs public-suffix data that
        # neither this tool nor NPM carries. Asserted so the behaviour is a
        # decision on record rather than a surprise.
        self.assertEqual(npm_api.domain_prefix("ex.example.co.uk"), "ex.example")


# =============================================================================
# dedupe_domains
# =============================================================================

class TestDedupeDomains(unittest.TestCase):
    """Rewriting one base onto another can collide with a name the host
    already carries; NPM would otherwise store the same name twice."""

    def test_preserves_order_of_first_occurrence(self):
        self.assertEqual(
            npm_api.dedupe_domains(["b.example.com", "a.example.com", "c.example.com"]),
            ["b.example.com", "a.example.com", "c.example.com"],
        )

    def test_case_insensitive(self):
        self.assertEqual(
            npm_api.dedupe_domains(["App.Example.com", "app.example.com", "APP.EXAMPLE.COM"]),
            ["App.Example.com"],
        )

    def test_keeps_the_first_spelling_not_the_last(self):
        self.assertEqual(
            npm_api.dedupe_domains(["APP.example.com", "app.example.com"]),
            ["APP.example.com"],
        )

    def test_surrounding_whitespace_does_not_defeat_the_match(self):
        self.assertEqual(
            npm_api.dedupe_domains([" app.example.com ", "app.example.com"]),
            [" app.example.com "],
        )

    def test_drops_empty_and_whitespace_only_entries(self):
        self.assertEqual(
            npm_api.dedupe_domains(["a.example.com", "", "   ", "b.example.com"]),
            ["a.example.com", "b.example.com"],
        )

    def test_empty_input(self):
        self.assertEqual(npm_api.dedupe_domains([]), [])


# =============================================================================
# coerce_field_value
# =============================================================================

class TestCoerceFieldValue(unittest.TestCase):
    """CLI "field=value" strings into the JSON types NPM expects."""

    def test_true_in_any_case(self):
        for given in ("true", "True", "  TRUE  "):
            with self.subTest(given=given):
                self.assertIs(npm_api.coerce_field_value("block_exploits", given), True)

    def test_false_in_any_case(self):
        for given in ("false", "False", " FALSE "):
            with self.subTest(given=given):
                self.assertIs(npm_api.coerce_field_value("block_exploits", given), False)

    def test_null_spellings(self):
        for given in ("null", "none", "NULL", "None"):
            with self.subTest(given=given):
                self.assertIsNone(npm_api.coerce_field_value("advanced_config", given))

    def test_json_array_literal(self):
        self.assertEqual(
            npm_api.coerce_field_value("locations", '[{"path": "/api"}]'),
            [{"path": "/api"}],
        )

    def test_json_object_literal(self):
        self.assertEqual(
            npm_api.coerce_field_value("meta", '{"letsencrypt_agree": true}'),
            {"letsencrypt_agree": True},
        )

    def test_leading_whitespace_still_reads_as_json(self):
        self.assertEqual(npm_api.coerce_field_value("locations", '  ["/api"]'), ["/api"])

    def test_json_literal_wins_over_comma_splitting_for_list_fields(self):
        self.assertEqual(
            npm_api.coerce_field_value("domain_names", '["a.example.com", "b.example.com"]'),
            ["a.example.com", "b.example.com"],
        )

    def test_malformed_json_names_the_field(self):
        with self.assertRaisesRegex(ValueError, "locations"):
            npm_api.coerce_field_value("locations", "[not json")

    def test_list_field_splits_on_commas_and_strips(self):
        self.assertEqual(
            npm_api.coerce_field_value(
                "domain_names", "a.example.com, b.example.com ,c.example.com"
            ),
            ["a.example.com", "b.example.com", "c.example.com"],
        )

    def test_list_field_drops_empty_segments(self):
        self.assertEqual(
            npm_api.coerce_field_value("domain_names", "a.example.com,,b.example.com,"),
            ["a.example.com", "b.example.com"],
        )

    def test_free_text_field_keeps_its_commas(self):
        # advanced_config is nginx config, not a list; splitting it would
        # corrupt every directive containing a comma.
        self.assertEqual(
            npm_api.coerce_field_value("advanced_config", "add_header X-A a, b;"),
            "add_header X-A a, b;",
        )

    def test_plain_integer(self):
        self.assertEqual(npm_api.coerce_field_value("forward_port", "8080"), 8080)

    def test_negative_integer(self):
        self.assertEqual(npm_api.coerce_field_value("forward_port", "-1"), -1)

    def test_malformed_numeric_stays_a_string(self):
        # Regression: an lstrip("-").isdigit() test accepted "--5" and then
        # int() raised an uncaught ValueError. Strict matching sends it on as
        # a string and lets NPM reject it.
        self.assertEqual(npm_api.coerce_field_value("forward_port", "--5"), "--5")

    def test_other_non_integers_stay_strings(self):
        for given in ("5.5", "1e3", "12abc", "0x10", "+5"):
            with self.subTest(given=given):
                self.assertEqual(npm_api.coerce_field_value("forward_port", given), given)

    def test_zero_clears_a_link_field(self):
        # 0 is NPM's "nothing linked" for these two, so `bulk-update
        # certificate_id 0` has to clear rather than point at host 0.
        for field in sorted(npm_api.HOST_UNSET_ON_ZERO_FIELDS):
            with self.subTest(field=field):
                self.assertIsNone(npm_api.coerce_field_value(field, "0"))

    def test_zero_is_a_real_value_everywhere_else(self):
        result = npm_api.coerce_field_value("forward_port", "0")
        self.assertEqual(result, 0)
        self.assertIsInstance(result, int)

    def test_nonzero_link_field_stays_an_integer(self):
        self.assertEqual(npm_api.coerce_field_value("certificate_id", "7"), 7)

    def test_unrecognised_value_passes_through_unchanged(self):
        self.assertEqual(
            npm_api.coerce_field_value("forward_host", "backend.internal.lan"),
            "backend.internal.lan",
        )


# =============================================================================
# cert_covers_domain
# =============================================================================

class TestCertCoversDomain(unittest.TestCase):
    """Three-valued on purpose: True, False, or None for "cannot tell"."""

    def test_exact_match(self):
        cert = {"domain_names": ["app.example.com"]}
        self.assertIs(npm_api.cert_covers_domain(cert, "app.example.com"), True)

    def test_match_is_case_and_trailing_dot_insensitive(self):
        cert = {"domain_names": ["App.Example.COM."]}
        self.assertIs(npm_api.cert_covers_domain(cert, "  APP.example.com.  "), True)

    def test_non_matching_name(self):
        cert = {"domain_names": ["other.example.com"]}
        self.assertIs(npm_api.cert_covers_domain(cert, "app.example.com"), False)

    def test_wildcard_covers_one_label(self):
        cert = {"domain_names": ["*.example.com"]}
        self.assertIs(npm_api.cert_covers_domain(cert, "app.example.com"), True)

    def test_wildcard_does_not_cover_two_labels(self):
        # RFC 6125: *.example.com is not valid for app.eu.example.com. Getting
        # this wrong would attach a cert that browsers then reject.
        cert = {"domain_names": ["*.example.com"]}
        self.assertIs(npm_api.cert_covers_domain(cert, "app.eu.example.com"), False)

    def test_wildcard_does_not_cover_the_apex(self):
        cert = {"domain_names": ["*.example.com"]}
        self.assertIs(npm_api.cert_covers_domain(cert, "example.com"), False)

    def test_matches_any_entry_in_the_list(self):
        cert = {"domain_names": ["a.example.com", "*.internal.lan", "b.example.com"]}
        self.assertIs(npm_api.cert_covers_domain(cert, "app.internal.lan"), True)

    def test_unusable_metadata_is_unknown_not_absent(self):
        # NPM keeps domain_names as metadata only and never consults it when
        # serving TLS, so for uploaded certs it drifts. A recorded "*.internal,"
        # can belong to a cert that really does serve *.internal.lan; answering
        # False here would refuse a valid assignment.
        cert = {"domain_names": ["*.internal,"]}
        self.assertIsNone(npm_api.cert_covers_domain(cert, "app.internal.lan"))

    def test_entries_without_a_usable_name_are_skipped(self):
        for entry in ("*.internal,", "a.example.com b.example.com",
                      "a.example.com;b.example.com", "localhost", ""):
            with self.subTest(entry=entry):
                cert = {"domain_names": [entry]}
                self.assertIsNone(npm_api.cert_covers_domain(cert, "app.example.com"))

    def test_empty_domain_list_is_unknown(self):
        self.assertIsNone(npm_api.cert_covers_domain({"domain_names": []}, "app.example.com"))

    def test_missing_domain_list_is_unknown(self):
        self.assertIsNone(npm_api.cert_covers_domain({}, "app.example.com"))

    def test_null_domain_list_is_unknown(self):
        self.assertIsNone(npm_api.cert_covers_domain({"domain_names": None}, "app.example.com"))

    def test_one_usable_entry_makes_the_answer_definite(self):
        # Junk alongside a real name is still a real answer: the usable entry
        # was checked and did not match.
        cert = {"domain_names": ["*.internal,", "other.example.com"]}
        self.assertIs(npm_api.cert_covers_domain(cert, "app.example.com"), False)


# =============================================================================
# cert_days_remaining / cert_status_label
# =============================================================================

class TestCertExpiry(unittest.TestCase):
    """NPM sends no "expired" flag, so validity is derived from expires_on."""

    def test_future_expiry(self):
        cert = {"expires_on": _expires_in(timedelta(days=45, minutes=5))}
        self.assertEqual(npm_api.cert_days_remaining(cert), 45)

    def test_naive_timestamp_in_npms_own_format(self):
        # NPM's usual shape is "YYYY-MM-DD HH:MM:SS" with no zone; it must not
        # collide with an aware `now` and raise on the subtraction.
        naive = (datetime.now() + timedelta(days=10, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(npm_api.cert_days_remaining({"expires_on": naive}), 10)

    def test_expired_certificate_is_negative(self):
        cert = {"expires_on": _expires_in(timedelta(days=-3, minutes=-5))}
        self.assertLess(npm_api.cert_days_remaining(cert), 0)

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
        self.assertEqual(npm_api.cert_days_remaining(cert), -4)

    def test_a_certificate_expired_within_the_last_day_still_reads_expired(self):
        # The case that makes flooring the right choice above.
        cert = {"expires_on": _expires_in(timedelta(hours=-1))}
        self.assertLess(npm_api.cert_days_remaining(cert), 0)
        self.assertIn("EXPIRED", npm_api.cert_status_label(cert))

    def test_missing_expires_on(self):
        self.assertIsNone(npm_api.cert_days_remaining({}))

    def test_empty_expires_on(self):
        self.assertIsNone(npm_api.cert_days_remaining({"expires_on": ""}))

    def test_unparseable_expires_on(self):
        self.assertIsNone(npm_api.cert_days_remaining({"expires_on": "not-a-date"}))

    def test_valid_label(self):
        cert = {"expires_on": _expires_in(timedelta(days=45, minutes=5))}
        self.assertIn("VALID", npm_api.cert_status_label(cert))

    def test_warning_label_inside_the_window(self):
        cert = {"expires_on": _expires_in(timedelta(days=7, minutes=5))}
        self.assertIn("7d LEFT", npm_api.cert_status_label(cert))

    def test_warning_window_boundary_is_inclusive(self):
        cert = {"expires_on": _expires_in(
            timedelta(days=npm_api.CERT_EXPIRY_WARN_DAYS, minutes=5))}
        label = npm_api.cert_status_label(cert)
        self.assertIn("LEFT", label)
        self.assertNotIn("VALID", label)

    def test_one_day_past_the_window_is_valid(self):
        cert = {"expires_on": _expires_in(
            timedelta(days=npm_api.CERT_EXPIRY_WARN_DAYS + 1, minutes=5))}
        self.assertIn("VALID", npm_api.cert_status_label(cert))

    def test_expired_label_reports_age(self):
        cert = {"expires_on": _expires_in(timedelta(days=-10, minutes=-5))}
        label = npm_api.cert_status_label(cert)
        self.assertIn("EXPIRED", label)
        self.assertIn("11d AGO", label)

    def test_unknown_label_is_not_a_failure_claim(self):
        # An unreadable date must not render as expired; that would push
        # someone into regenerating a working certificate.
        for cert in ({}, {"expires_on": "not-a-date"}):
            label = npm_api.cert_status_label(cert)
            self.assertIn("UNKNOWN", label, msg=f"for cert {cert!r}")
            self.assertNotIn("EXPIRED", label, msg=f"for cert {cert!r}")


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


class TestHostConfigPayload(unittest.TestCase):
    """Copy-by-exclusion: whatever NPM sends is written back untouched unless
    it is explicitly known to be unwritable."""

    def test_strips_server_assigned_fields(self):
        payload = npm_api.host_config_payload(_host_fixture())
        for key in ("id", "created_on", "modified_on", "owner_user_id"):
            self.assertNotIn(key, payload)

    def test_strips_expanded_objects_but_keeps_their_ids(self):
        # Echoing the expanded object back sends a nested object where the API
        # wants an integer, and the write fails.
        payload = npm_api.host_config_payload(_host_fixture())
        for key in ("certificate", "owner", "access_list"):
            self.assertNotIn(key, payload)
        self.assertEqual(payload["certificate_id"], 4)
        self.assertEqual(payload["access_list_id"], 2)

    def test_readonly_set_is_fully_covered(self):
        payload = npm_api.host_config_payload(_host_fixture())
        self.assertEqual(npm_api.HOST_READONLY_FIELDS & payload.keys(), set())

    def test_strips_runtime_meta_but_keeps_configuration_meta(self):
        payload = npm_api.host_config_payload(_host_fixture())
        self.assertEqual(payload["meta"], {"letsencrypt_agree": True})

    def test_meta_absent_becomes_empty_dict(self):
        payload = npm_api.host_config_payload({"domain_names": ["app.example.com"]})
        self.assertEqual(payload["meta"], {})

    def test_meta_null_becomes_empty_dict(self):
        payload = npm_api.host_config_payload({"meta": None})
        self.assertEqual(payload["meta"], {})

    def test_preserves_fields_this_script_has_never_heard_of(self):
        # The whole point of copying by exclusion: an allowlist written against
        # an older NPM silently reset trust_forwarded_proto when 2.15 added it.
        host = _host_fixture()
        host["some_future_npm_field"] = {"nested": ["value"]}
        payload = npm_api.host_config_payload(host)
        self.assertEqual(payload["some_future_npm_field"], {"nested": ["value"]})
        self.assertIs(payload["trust_forwarded_proto"], True)

    def test_overrides_replace_existing_values(self):
        payload = npm_api.host_config_payload(
            _host_fixture(), {"forward_port": 9090, "enabled": False}
        )
        self.assertEqual(payload["forward_port"], 9090)
        self.assertIs(payload["enabled"], False)

    def test_overrides_can_add_and_null_fields(self):
        payload = npm_api.host_config_payload(
            _host_fixture(), {"certificate_id": None, "brand_new": "x"}
        )
        self.assertIsNone(payload["certificate_id"])
        self.assertEqual(payload["brand_new"], "x")

    def test_source_host_is_not_mutated(self):
        # Callers reuse the fetched host afterwards, e.g. to print a summary.
        host = _host_fixture()
        npm_api.host_config_payload(host, {"forward_port": 9090})
        self.assertEqual(host["id"], 12)
        self.assertEqual(host["forward_port"], 8080)
        self.assertIs(host["meta"]["nginx_online"], True)


# =============================================================================
# format_http_error
# =============================================================================

class TestFormatHttpError(unittest.TestCase):
    """requests stringifies an HTTPError as "400 Client Error: Bad Request for
    url: ...", which buries the reason NPM actually gave."""

    def test_npm_error_object(self):
        response = _FakeResponse(400, json_body={"error": {"message": "Domain already in use"}})
        exc = requests.HTTPError("400 Client Error", response=response)
        self.assertEqual(npm_api.format_http_error(exc), "HTTP 400: Domain already in use")

    def test_error_given_as_a_bare_string(self):
        response = _FakeResponse(403, json_body={"error": "Forbidden"})
        exc = requests.HTTPError("403", response=response)
        self.assertEqual(npm_api.format_http_error(exc), "HTTP 403: Forbidden")

    def test_json_body_in_some_other_shape(self):
        response = _FakeResponse(422, json_body={"detail": "bad input"})
        exc = requests.HTTPError("422", response=response)
        result = npm_api.format_http_error(exc)
        self.assertTrue(result.startswith("HTTP 422: "),
                        msg=f"expected an 'HTTP 422: ' prefix, got {result!r}")
        self.assertIn("bad input", result)

    def test_non_json_body_is_reported_verbatim(self):
        # A reverse proxy in front of NPM answers with HTML, not NPM's JSON.
        response = _FakeResponse(502, text="<html><body>Bad Gateway</body></html>")
        exc = requests.HTTPError("502", response=response)
        self.assertEqual(
            npm_api.format_http_error(exc),
            "HTTP 502: <html><body>Bad Gateway</body></html>",
        )

    def test_long_non_json_body_is_truncated(self):
        response = _FakeResponse(502, text="x" * 5000)
        exc = requests.HTTPError("502", response=response)
        self.assertEqual(npm_api.format_http_error(exc), "HTTP 502: " + "x" * 200)

    def test_empty_non_json_body_gives_the_status_alone(self):
        response = _FakeResponse(500, text="   ")
        exc = requests.HTTPError("500", response=response)
        self.assertEqual(npm_api.format_http_error(exc), "HTTP 500")

    def test_exception_with_no_response_falls_back_to_its_own_message(self):
        # Connection errors never reach a status code.
        exc = requests.ConnectionError("Connection refused")
        self.assertEqual(npm_api.format_http_error(exc), "Connection refused")

    def test_plain_exception(self):
        self.assertEqual(npm_api.format_http_error(ValueError("boom")), "boom")


# =============================================================================
# write_secret
# =============================================================================

class TestWriteSecret(_WorkdirTestCase):
    """Private keys and API tokens must never exist world-readable, not even
    for the instant between write_text() and chmod()."""

    def test_creates_owner_only_file_with_the_content(self):
        path = npm_api.write_secret(self.workdir / "token.txt", "s3cret-token")
        self.assertEqual(path.read_text(), "s3cret-token")
        self.assertEqual(_mode(path), 0o600)

    def test_overwriting_a_world_readable_file_tightens_the_mode(self):
        # O_CREAT leaves an existing file's mode alone, so write_secret has to
        # unlink first. Without that, a token file created by an older version
        # would stay 0644 forever.
        path = self.workdir / "token.txt"
        path.write_text("old")
        path.chmod(0o644)

        npm_api.write_secret(path, "new")

        self.assertEqual(path.read_text(), "new")
        self.assertEqual(_mode(path), 0o600)

    def test_replaces_a_symlink_instead_of_writing_through_it(self):
        # Writing through the link would spray the secret into whatever the
        # link points at, and leave that file's permissive mode in place.
        target = self.workdir / "innocent.txt"
        target.write_text("untouched")
        link = self.workdir / "token.txt"
        link.symlink_to(target)

        npm_api.write_secret(link, "s3cret-token")

        self.assertEqual(target.read_text(), "untouched")
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), "s3cret-token")
        self.assertEqual(_mode(link), 0o600)


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
        if not endpoint.endswith("/certificates"):
            raise AssertionError(f"unexpected endpoint {endpoint}")
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


class TestDownloadCertificate(_WorkdirTestCase):

    def test_json_route_writes_key_at_owner_only_mode(self):
        client = _JsonRouteClient({
            "certificate": "-----BEGIN CERTIFICATE-----\nleaf\n",
            "private": "-----BEGIN PRIVATE KEY-----\nkey\n",
            "intermediate": "-----BEGIN CERTIFICATE-----\nchain\n",
        })

        written = client.download_certificate(1, str(self.workdir), "example.com")

        names = {p.name for p in written}
        self.assertEqual(names, {"example.com.key", "example.com.crt",
                                 "example.com.chain.crt", "example.com_metadata.json"})
        self.assertEqual(_mode(self.workdir / "example.com.key"), 0o600)

    def test_json_route_with_empty_key_material_is_a_failure(self):
        # NPM answers 200 with empty bodies for certs whose key it does not
        # hold. Writing an empty .key reads as a successful backup of an
        # unusable key, so it has to raise instead.
        client = _ZipRouteClient(_zip_bytes({}))
        client.get = lambda endpoint, **kw: (
            _FakeResponse(200, json_body={"certificate": "", "private": ""})
            if endpoint.endswith("/certificates") else _FakeResponse(404)
        )

        with self.assertRaisesRegex(npm_api.CertificateDownloadError, "no key material"):
            client.download_certificate(1, str(self.workdir), "example.com")

        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_zip_member_escaping_into_a_prefix_sibling_is_rejected(self):
        # Regression, and the exact case the old guard got wrong: it compared
        # resolved paths with str.startswith, so ".../out-evil/pwned.txt"
        # passed the ".../out" prefix test and extracted outside the target.
        out = self.workdir / "out"
        out.mkdir()
        sibling = self.workdir / "out-evil"
        sibling.mkdir()

        client = _ZipRouteClient(_zip_bytes({"../out-evil/pwned.txt": "owned"}))

        with self.assertRaises(npm_api.CertificateDownloadError) as caught:
            client.download_certificate(1, str(out), "example.com")

        self.assertIn("skipped unsafe path", str(caught.exception))
        self.assertEqual(list(sibling.iterdir()), [])
        # temp zip cleaned up
        self.assertFalse((out / "example.com.download.zip").exists())

    def test_safe_members_survive_alongside_a_rejected_one(self):
        out = self.workdir / "out"
        out.mkdir()
        sibling = self.workdir / "out-evil"
        sibling.mkdir()

        client = _ZipRouteClient(_zip_bytes({
            "../out-evil/pwned.txt": "owned",
            "fullchain.pem": "-----BEGIN CERTIFICATE-----\n",
        }))

        written = client.download_certificate(1, str(out), "example.com")

        self.assertEqual([p.name for p in written], ["fullchain.pem"])
        self.assertTrue((out / "fullchain.pem").exists())
        self.assertEqual(list(sibling.iterdir()), [])

    def test_extracted_key_material_is_chmodded(self):
        # The archive's stored mode is whatever NPM chose; keys taken from it
        # used never to be tightened at all.
        client = _ZipRouteClient(_zip_bytes({"privkey.pem": "-----BEGIN PRIVATE KEY-----\n"}))

        client.download_certificate(1, str(self.workdir), "example.com")

        self.assertEqual(_mode(self.workdir / "privkey.pem"), 0o600)

    def test_both_routes_failing_names_each_attempt(self):
        client = _ZipRouteClient(b"")
        client.get = lambda endpoint, **kw: _FakeResponse(404)

        with self.assertRaises(npm_api.CertificateDownloadError) as caught:
            client.download_certificate(9, str(self.workdir), "example.com")

        message = str(caught.exception)
        self.assertIn("certificate 9", message)
        self.assertIn("JSON route", message)
        self.assertIn("ZIP route", message)

    def test_certificate_name_is_sanitised_into_the_output_directory(self):
        # Defense in depth: the name comes from NPM, not from the user, but it
        # still lands in a filename.
        client = _JsonRouteClient({"certificate": "leaf", "private": "key"})

        written = client.download_certificate(1, str(self.workdir), "../../etc/passwd")

        for path in written:
            self.assertEqual(path.parent, self.workdir)


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


class TestDashboardStats(unittest.TestCase):
    """A failed section reports None, never 0 — "0 proxy hosts" used to read
    as a fact when it actually meant the request had failed."""

    def test_working_sections_report_real_counts(self):
        stats = _DashboardClient().get_dashboard_stats()
        self.assertEqual(stats["proxy_hosts"], {"total": 3, "enabled": 2, "disabled": 1})
        self.assertEqual(stats["redirections"], 2)
        self.assertEqual(stats["users"], 1)

    def test_failed_sections_report_none(self):
        stats = _DashboardClient().get_dashboard_stats()
        self.assertEqual(stats["certificates"],
                         {"total": None, "valid": None, "expired": None})
        self.assertIsNone(stats["streams"])
        self.assertIsNone(stats["access_lists"])

    def test_every_section_failing_reports_none_never_zero(self):
        stats = _TotallyBrokenClient().get_dashboard_stats()

        self.assertEqual(stats["proxy_hosts"],
                         {"total": None, "enabled": None, "disabled": None})
        self.assertEqual(stats["certificates"],
                         {"total": None, "valid": None, "expired": None})
        for section in ("redirections", "streams", "users", "access_lists"):
            self.assertIsNone(
                stats[section],
                msg=f"{section} reported {stats[section]!r}, not None",
            )
        self.assertEqual(len(stats["failures"]), 6)

    def test_one_failure_entry_per_failed_section(self):
        stats = _DashboardClient().get_dashboard_stats()
        self.assertEqual(len(stats["failures"]), 3)
        joined = " | ".join(stats["failures"])
        self.assertIn("certificates", joined)
        self.assertIn("streams", joined)
        self.assertIn("access lists", joined)

    def test_failure_text_carries_npms_own_message(self):
        stats = _DashboardClient().get_dashboard_stats()
        self.assertTrue(
            any("db locked" in f for f in stats["failures"]),
            msg=f"no failure mentioned 'db locked': {stats['failures']!r}",
        )

    def test_an_empty_but_healthy_npm_reports_zeros(self):
        stats = _HealthyEmptyClient().get_dashboard_stats()
        self.assertEqual(stats["failures"], [])
        self.assertEqual(stats["proxy_hosts"], {"total": 0, "enabled": 0, "disabled": 0})
        self.assertEqual(stats["users"], 0)
        self.assertEqual(stats["streams"], 0)

    def test_expired_certificates_are_counted_from_expires_on(self):
        class _CertClient(_HealthyEmptyClient):
            def list_certificates(self):
                return [
                    {"id": 1, "expires_on": _expires_in(timedelta(days=45, minutes=5))},
                    {"id": 2, "expires_on": _expires_in(timedelta(days=-5, minutes=-5))},
                    {"id": 3, "expires_on": None},  # unreadable counts as valid, not expired
                ]

        stats = _CertClient().get_dashboard_stats()
        self.assertEqual(stats["certificates"], {"total": 3, "valid": 2, "expired": 1})


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


class TestFullBackup(_WorkdirTestCase):

    def test_without_keys_no_key_material_is_written_anywhere(self):
        client = _BackupClient()

        result = client.full_backup(str(self.workdir), include_keys=False)

        self.assertEqual(list(self.workdir.rglob("*.key")), [])
        self.assertEqual(client.downloaded, [], msg="not even attempted")
        self.assertTrue(result.complete)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.key_failures, [])

    def test_without_keys_metadata_is_still_captured(self):
        client = _BackupClient()

        client.full_backup(str(self.workdir), include_keys=False)

        self.assertTrue(
            (self.workdir / ".ssl" / "app.example.com" / "certificate_meta.json").exists())
        self.assertTrue(
            (self.workdir / ".Proxy_Hosts" / "app.example.com" / "proxy_config.json").exists())
        self.assertTrue((self.workdir / "full_config_latest.json").is_symlink())

        full_config = json.loads((self.workdir / "full_config_latest.json").read_text())
        self.assertEqual(set(full_config), {"users", "settings", "access_lists",
                                            "proxy_hosts", "certificates"})

    def test_with_keys_the_key_lands_owner_readable_only(self):
        client = _BackupClient()

        client.full_backup(str(self.workdir), include_keys=True)

        keys = list(self.workdir.rglob("*.key"))
        self.assertEqual(len(keys), 1)
        self.assertEqual(_mode(keys[0]), 0o600)

    def test_an_unexportable_certificate_is_not_a_backup_failure(self):
        # Uploaded certificates fail here on every single run; treating that as
        # fatal would make every scheduled backup exit non-zero.
        client = _BackupClient()

        result = client.full_backup(str(self.workdir), include_keys=True)

        self.assertEqual(result.failures, [])
        self.assertIs(result.complete, True)

    def test_an_unexportable_certificate_is_reported_as_a_key_failure(self):
        client = _BackupClient()

        result = client.full_backup(str(self.workdir), include_keys=True)

        self.assertEqual(len(result.key_failures), 1)
        failure = result.key_failures[0]
        self.assertEqual(failure.cert_id, _UPLOADED_CERT["id"])
        self.assertEqual(failure.name, _UPLOADED_CERT["nice_name"])
        self.assertEqual(failure.provider, "other")
        self.assertIn("no key material", failure.reason)

    def test_a_failing_section_marks_the_backup_incomplete(self):
        # A scheduled run has to be able to fail loudly rather than exit 0 over
        # a half-written backup.
        class _BrokenHostsClient(_BackupClient):
            def list_hosts(self):
                raise requests.HTTPError("500 Server Error")

        result = _BrokenHostsClient().full_backup(str(self.workdir), include_keys=False)

        self.assertIs(result.complete, False)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("proxy hosts", result.failures[0])

    def test_a_failing_section_does_not_stop_the_others(self):
        class _BrokenHostsClient(_BackupClient):
            def list_hosts(self):
                raise requests.HTTPError("500 Server Error")

        _BrokenHostsClient().full_backup(str(self.workdir), include_keys=False)

        self.assertTrue(list((self.workdir / ".user").glob("users_*.json")),
                        msg="the users section was not written")
        self.assertTrue(list((self.workdir / ".access_lists").glob("access_lists_*.json")),
                        msg="the access lists section was not written")

    def test_result_path_points_at_the_output_directory(self):
        result = _BackupClient().full_backup(str(self.workdir), include_keys=False)
        self.assertEqual(Path(result.path), self.workdir)

    def test_a_stale_latest_symlink_is_replaced(self):
        # exists() follows the link, so a symlink pointing at a pruned backup
        # read as absent and symlink_to() then raised FileExistsError.
        (self.workdir / "full_config_latest.json").symlink_to("full_config_pruned.json")

        _BackupClient().full_backup(str(self.workdir), include_keys=False)

        latest = self.workdir / "full_config_latest.json"
        self.assertTrue(latest.is_symlink())
        self.assertTrue(latest.exists())


# =============================================================================
# CertKeyFailure.container_paths
# =============================================================================

class TestCertKeyFailurePaths(unittest.TestCase):
    """The remedy printed to the user is a docker cp command, so the path has
    to be right — npm-api speaks HTTP and cannot look inside the container."""

    def _failure(self, provider):
        return npm_api.CertKeyFailure(
            cert_id=42, name="app.example.com", provider=provider,
            reason="response carried no key material",
        )

    def test_letsencrypt_points_only_at_the_issued_location(self):
        self.assertEqual(self._failure("letsencrypt").container_paths,
                         ["/etc/letsencrypt/live/npm-42"])

    def test_another_provider_points_only_at_the_upload_location(self):
        self.assertEqual(self._failure("other").container_paths,
                         ["/data/custom_ssl/npm-42"])

    def test_missing_provider_offers_both_rather_than_guessing(self):
        # NPM is under no obligation to send the field, and one confident wrong
        # path is worse than two candidates.
        self.assertEqual(self._failure(None).container_paths,
                         ["/data/custom_ssl/npm-42", "/etc/letsencrypt/live/npm-42"])

    def test_string_form_identifies_the_certificate(self):
        self.assertEqual(
            str(self._failure(None)),
            "certificate 42 (app.example.com): response carried no key material",
        )


# =============================================================================
# BackupResult
# =============================================================================

class TestBackupResult(unittest.TestCase):

    def test_complete_when_nothing_failed(self):
        self.assertIs(npm_api.BackupResult(path="/tmp/backup").complete, True)

    def test_key_failures_alone_do_not_make_it_incomplete(self):
        result = npm_api.BackupResult(
            path="/tmp/backup",
            key_failures=[npm_api.CertKeyFailure(1, "app.example.com", "other", "no key")],
        )
        self.assertIs(result.complete, True)

    def test_section_failures_make_it_incomplete(self):
        self.assertIs(
            npm_api.BackupResult(path="/tmp/backup", failures=["users: 500"]).complete,
            False,
        )


# =============================================================================
# Bulk-command infrastructure: shared doubles
# =============================================================================

def _hosts_fixture():
    """Three hosts across two base domains, one of them oddly cased."""
    return [
        {"id": 12, "domain_names": ["app.example.com"], "forward_host": "10.0.0.5"},
        {"id": 13, "domain_names": ["api.internal.lan", "www.example.com"],
         "forward_host": "10.0.0.6"},
        {"id": 14, "domain_names": ["Shop.Internal.LAN"], "forward_host": "10.0.0.7"},
    ]


class _HostsClient(_StubClient):
    """Serves a fixed inventory to select_hosts."""

    def __init__(self, hosts=None):
        self.hosts = _hosts_fixture() if hosts is None else hosts

    def list_hosts(self):
        return self.hosts


class _UpdateRecordingClient(_StubClient):
    """Records every update_host call and fails for chosen host IDs.

    The call is recorded before the failure is raised, so a test can tell "the
    write was attempted and NPM rejected it" from "the host was skipped".
    """

    def __init__(self, fail_ids=(), error=None):
        self.calls = []
        self._fail_ids = set(fail_ids)
        self._error = error or requests.HTTPError(
            "400 Client Error",
            response=_FakeResponse(400, json_body={"error": {"message": "Domain already in use"}}),
        )

    def update_host(self, host_id, updates):
        self.calls.append((host_id, updates))
        if host_id in self._fail_ids:
            raise self._error
        return {"id": host_id, **updates}

    @property
    def written_ids(self):
        return [host_id for host_id, _ in self.calls]


class _CertLookupClient(_StubClient):
    """One certificate, or one failure, plus a record of the lookups made."""

    def __init__(self, cert=None, error=None):
        self.cert = cert
        self.error = error
        self.lookups = []

    def get_certificate(self, cert_id):
        self.lookups.append(cert_id)
        if self.error is not None:
            raise self.error
        return self.cert


# =============================================================================
# select_hosts
# =============================================================================

class TestSelectHosts(_ConsoleTestCase):
    """The gate in front of every bulk write.

    Whatever it returns gets rewritten, so the interesting property is not what
    it selects but what it refuses to select.
    """

    def _select(self, ids=None, pattern=None, interactive=False, hosts=None, **kwargs):
        return npm_api.select_hosts(_HostsClient(hosts), ids, pattern, interactive, **kwargs)

    def _ids_of(self, hosts):
        return [h["id"] for h in hosts]

    # --- no selector -------------------------------------------------------

    def test_no_selector_refuses_to_act(self):
        # The single most important assertion in this file. Falling through to
        # "everything" is what `bulk-remove-domain com` used to do: no --ids, no
        # --pattern, no prompt, and every host in the estate rewritten. The
        # guard has to raise, not return an empty list, because an empty list
        # reads to a caller as "nothing matched" and is not an error.
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._select()

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("--ids")
        self.assertPrinted("--pattern")
        self.assertPrinted("--interactive")

    def test_interactive_false_is_not_a_selector(self):
        # --interactive is a flag, so callers pass interactive=False rather than
        # omitting it. False must land on the refusal, not skip past it.
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._select(ids=None, pattern=None, interactive=False)
        self.assertEqual(caught.exception.exit_code, 1)

    def test_empty_selector_strings_are_no_selector_at_all(self):
        # `--ids ""` and `--pattern ""` are falsy, so they fall through to the
        # refusal rather than selecting nothing or, worse, everything.
        for ids, pattern in (("", ""), (None, ""), ("", None)):
            with self.subTest(ids=ids, pattern=pattern):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    self._select(ids=ids, pattern=pattern)
                self.assertEqual(caught.exception.exit_code, 1)

    def test_empty_host_list_returns_empty_without_raising(self):
        # Checked before the refusal above, and safely so: an NPM with no hosts
        # has nothing for a missing selector to damage. Callers then hit their
        # own "no hosts selected" branch and stop.
        self.assertEqual(self._select(hosts=[]), [])
        self.assertPrinted("No proxy hosts found")

    # --- --ids -------------------------------------------------------------

    def test_ids_select_exactly_those_hosts(self):
        self.assertEqual(self._ids_of(self._select(ids="12,14")), [12, 14])

    def test_ids_tolerate_whitespace_around_the_commas(self):
        # "12, 13" is what anyone typing a list actually writes, and quoting it
        # in a shell keeps the spaces.
        for given in ("12, 14", " 12 , 14 ", "12 ,14"):
            with self.subTest(given=given):
                self.assertEqual(self._ids_of(self._select(ids=given)), [12, 14])

    def test_ids_ignore_empty_segments(self):
        # A trailing comma from an edited command line is not an error.
        self.assertEqual(self._ids_of(self._select(ids="12,,14,")), [12, 14])

    def test_selection_order_follows_the_host_list_not_the_option(self):
        # Worth pinning down before a command with a designated survivor (host
        # merge) reads hosts[0] and assumes it is the first ID typed. It is not:
        # NPM's ordering wins.
        self.assertEqual(self._ids_of(self._select(ids="14,12")), [12, 14])

    def test_non_numeric_ids_exit_1_rather_than_raising_valueerror(self):
        # int() on "abc" used to escape as an unhandled ValueError and print a
        # traceback at the user.
        for given in ("abc", "12,abc", "12;13", "12.5"):
            with self.subTest(given=given):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    self._select(ids=given)
                self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("comma-separated numbers")

    def test_unknown_ids_warn_by_name_and_the_rest_still_apply(self):
        # Silently dropping an unmatched ID would let `--ids 12,99` report a
        # clean run having touched one host out of the two that were asked for.
        selected = self._select(ids="99,12,98")

        self.assertEqual(self._ids_of(selected), [12])
        self.assertPrinted("No such host(s): 98, 99")

    def test_ids_matching_nothing_return_empty_and_warn(self):
        self.assertEqual(self._select(ids="98,99"), [])
        self.assertPrinted("No such host(s)")

    # --- --pattern ---------------------------------------------------------

    def test_pattern_works_as_a_glob(self):
        self.assertEqual(self._ids_of(self._select(pattern="*.internal.lan")), [13, 14])

    def test_pattern_works_as_a_plain_substring(self):
        # Both spellings are accepted so --pattern means the same thing whether
        # or not the user thought to add a star.
        self.assertEqual(self._ids_of(self._select(pattern="internal.lan")), [13, 14])

    def test_pattern_matching_is_case_insensitive(self):
        # Host 14 is stored as "Shop.Internal.LAN"; DNS does not care about case
        # and neither may the selector, in either direction.
        for given in ("*.INTERNAL.LAN", "*.internal.lan", "Internal.LAN", "internal.lan"):
            with self.subTest(pattern=given):
                self.assertEqual(self._ids_of(self._select(pattern=given)), [13, 14])

    def test_pattern_matches_a_host_on_any_of_its_domains(self):
        # Host 13 carries api.internal.lan and www.example.com; either name
        # brings the whole host into the selection.
        self.assertEqual(self._ids_of(self._select(pattern="www.example.com")), [13])

    def test_pattern_matching_nothing_selects_nothing(self):
        # The other half of the fall-through guard: no match means no hosts, not
        # all hosts.
        self.assertEqual(self._select(pattern="*.example.net"), [])

    def test_ids_take_precedence_over_pattern(self):
        # Both options given is ambiguous; --ids is the more specific of the two
        # and wins, so --pattern cannot widen an explicit list.
        self.assertEqual(self._ids_of(self._select(ids="12", pattern="internal.lan")), [12])

    # --- --interactive -----------------------------------------------------

    def _interactive(self, answer, **kwargs):
        with mock.patch.object(npm_api.typer, "prompt", return_value=answer) as prompt:
            selected = npm_api.select_hosts(_HostsClient(), None, None, True, **kwargs)
        return selected, prompt

    def test_interactive_all_selects_every_host(self):
        selected, prompt = self._interactive("all")

        self.assertEqual(self._ids_of(selected), [12, 13, 14])
        # err=True keeps the prompt off stdout, which --json commands reserve.
        prompt.assert_called_once_with("Selection", err=True)

    def test_interactive_all_is_case_and_whitespace_tolerant(self):
        for given in ("ALL", " all ", "All\n"):
            with self.subTest(answer=given):
                selected, _ = self._interactive(given)
                self.assertEqual(self._ids_of(selected), [12, 13, 14])

    def test_interactive_index_list_is_one_based(self):
        # The menu is numbered from 1, so answering "1,3" must not hand back the
        # host at index 3.
        selected, _ = self._interactive("1,3")
        self.assertEqual(self._ids_of(selected), [12, 14])

    def test_interactive_tolerates_whitespace_between_indices(self):
        selected, _ = self._interactive(" 1 , 3 ")
        self.assertEqual(self._ids_of(selected), [12, 14])

    def test_interactive_out_of_range_indices_are_ignored(self):
        # Typing past the end of the menu drops that entry and keeps the rest,
        # rather than raising IndexError on a list the user can see.
        selected, _ = self._interactive("1,99")
        self.assertEqual(self._ids_of(selected), [12])

    def test_interactive_zero_and_negative_indices_are_ignored(self):
        # "0" is the off-by-one a 1-based menu invites. It must not resolve to
        # all_hosts[-1] and rewrite the last host in the list.
        selected, _ = self._interactive("0,-1")
        self.assertEqual(selected, [])

    def test_interactive_non_numeric_selection_exits_1(self):
        for given in ("one", "1;2", ""):
            with self.subTest(answer=given):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    self._interactive(given)
                self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("Invalid selection")

    def test_interactive_menu_lists_ids_and_domains(self):
        self._interactive("all")
        self.assertPrinted("ID 12")
        self.assertPrinted("app.example.com")

    def test_detail_field_is_shown_in_the_menu(self):
        # bulk-update passes the field being changed so the menu shows the value
        # about to be overwritten.
        self._interactive("all", detail_field="forward_host")
        self.assertPrinted("forward_host=10.0.0.5")

    def test_detail_field_absent_from_a_host_reads_as_na(self):
        with mock.patch.object(npm_api.typer, "prompt", return_value="all"):
            npm_api.select_hosts(_HostsClient(), None, None, True, detail_field="certificate_id")
        self.assertPrinted("certificate_id=N/A")


# =============================================================================
# confirm_bulk
# =============================================================================

class TestConfirmBulk(_ConsoleTestCase):
    """The last stop before a destructive write."""

    def test_yes_skips_the_prompt_entirely(self):
        # -y has to be genuinely non-interactive: a prompt here would hang a
        # cron run forever rather than failing.
        with mock.patch.object(npm_api.typer, "confirm") as confirm:
            self.assertIsNone(npm_api.confirm_bulk(True))
        confirm.assert_not_called()

    def test_accepting_returns_and_lets_the_caller_continue(self):
        with mock.patch.object(npm_api.typer, "confirm", return_value=True):
            self.assertIsNone(npm_api.confirm_bulk(False))
        self.assertNotPrinted("Cancelled")

    def test_declining_exits_zero_not_one(self):
        # Declining is the user getting what they asked for, so it is a success.
        # Exiting 1 would make `npm-api ... <<< n` look like a failed run to any
        # script wrapping it, and to `set -e`.
        with mock.patch.object(npm_api.typer, "confirm", return_value=False):
            with self.assertRaises(npm_api.typer.Exit) as caught:
                npm_api.confirm_bulk(False)

        self.assertEqual(caught.exception.exit_code, 0)
        self.assertPrinted("Cancelled")

    def test_prompt_text_is_passed_through_and_asked_on_stderr(self):
        # Per-command wording matters for the destructive commands, and err=True
        # keeps the question off stdout.
        with mock.patch.object(npm_api.typer, "confirm", return_value=True) as confirm:
            npm_api.confirm_bulk(False, "Merge these hosts?")
        confirm.assert_called_once_with("Merge these hosts?", err=True)

    def test_default_prompt_is_used_when_none_is_given(self):
        with mock.patch.object(npm_api.typer, "confirm", return_value=True) as confirm:
            npm_api.confirm_bulk(False)
        self.assertEqual(confirm.call_args.args, ("Apply these changes?",))


# =============================================================================
# print_bulk_summary
# =============================================================================

class TestPrintBulkSummary(_ConsoleTestCase):
    """The exit status of every bulk command comes from here."""

    def test_a_clean_run_returns_normally(self):
        self.assertIsNone(npm_api.print_bulk_summary(3, 0))
        self.assertPrinted("Successful: 3")

    def test_any_failure_exits_1(self):
        # The bug this replaced: each command printed its own summary and then
        # fell off the end, so a run where every single host failed still exited
        # 0 and a wrapping script carried on.
        for success, errors in ((0, 1), (5, 1), (0, 5)):
            with self.subTest(success=success, errors=errors):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    npm_api.print_bulk_summary(success, errors)
                self.assertEqual(caught.exception.exit_code, 1)

    def test_the_failure_count_is_reported(self):
        with self.assertRaises(npm_api.typer.Exit):
            npm_api.print_bulk_summary(1, 2)
        self.assertPrinted("Failed: 2")

    def test_skipped_hosts_are_reported_but_are_not_failures(self):
        # Skipping is routine — a host that already carries the domain, or one
        # that merge finds nothing to move. It must not colour the exit status.
        self.assertIsNone(npm_api.print_bulk_summary(2, 0, skipped=3))
        self.assertPrinted("Skipped: 3")

    def test_zero_skipped_is_left_out_of_the_summary(self):
        npm_api.print_bulk_summary(2, 0, skipped=0)
        self.assertNotPrinted("Skipped")

    def test_skipped_and_failed_together_still_exit_1(self):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            npm_api.print_bulk_summary(1, 1, skipped=1)
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("Skipped: 1")


# =============================================================================
# apply_domain_changes
# =============================================================================

def _change(host_id, resulting, current=None, new=None):
    return {
        "host_id": host_id,
        "current_domains": current or ["app.example.com"],
        "new_domains": new or [],
        "resulting_domains": resulting,
    }


class TestApplyDomainChanges(_ConsoleTestCase):
    """The write loop shared by the bulk domain commands."""

    def _describe(self, change):
        return f"now {', '.join(change['resulting_domains'])}"

    def test_writes_only_the_domain_list(self):
        # update_host reads the host back and copies every other field forward,
        # so the payload here must name domain_names and nothing else; an extra
        # key would override a field the user never asked to change.
        client = _UpdateRecordingClient()

        npm_api.apply_domain_changes(
            client, [_change(12, ["app.example.com", "www.example.com"])], self._describe)

        self.assertEqual(
            client.calls,
            [(12, {"domain_names": ["app.example.com", "www.example.com"]})],
        )

    def test_resulting_domains_is_the_field_that_gets_written(self):
        # Not current_domains and not new_domains: the caller has already worked
        # out the final list, including dedupe, and this loop must not re-derive
        # it from the other two.
        client = _UpdateRecordingClient()
        change = _change(12, ["final.example.com"],
                         current=["old.example.com"], new=["ignored.example.com"])

        npm_api.apply_domain_changes(client, [change], self._describe)

        self.assertEqual(client.calls[0][1], {"domain_names": ["final.example.com"]})

    def test_every_change_is_applied_in_order(self):
        client = _UpdateRecordingClient()

        npm_api.apply_domain_changes(
            client,
            [_change(12, ["a.example.com"]), _change(13, ["b.example.com"]),
             _change(14, ["c.example.com"])],
            self._describe,
        )

        self.assertEqual(client.written_ids, [12, 13, 14])

    def test_one_failing_host_does_not_abandon_the_rest(self):
        # A partial run is the normal outcome of a bulk write — one host has a
        # domain conflict, the other twenty are fine. Stopping at the first
        # rejection would leave the estate half-changed with no way to tell how
        # far it got.
        client = _UpdateRecordingClient(fail_ids=[13])

        with self.assertRaises(npm_api.typer.Exit) as caught:
            npm_api.apply_domain_changes(
                client,
                [_change(12, ["a.example.com"]), _change(13, ["b.example.com"]),
                 _change(14, ["c.example.com"])],
                self._describe,
            )

        self.assertEqual(caught.exception.exit_code, 1)
        # 14 comes after the failure, so its presence is the real assertion.
        self.assertEqual(client.written_ids, [12, 13, 14])

    def test_a_failure_is_counted_and_carries_npms_own_message(self):
        client = _UpdateRecordingClient(fail_ids=[13])

        with self.assertRaises(npm_api.typer.Exit):
            npm_api.apply_domain_changes(
                client, [_change(12, ["a.example.com"]), _change(13, ["b.example.com"])],
                self._describe)

        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")
        self.assertPrinted("Domain already in use")

    def test_describe_reports_the_successes_only(self):
        client = _UpdateRecordingClient(fail_ids=[13])
        described = []

        def describe(change):
            described.append(change["host_id"])
            return "ok"

        with self.assertRaises(npm_api.typer.Exit):
            npm_api.apply_domain_changes(
                client, [_change(12, ["a.example.com"]), _change(13, ["b.example.com"])],
                describe)

        self.assertEqual(described, [12])

    def test_a_clean_run_returns_normally(self):
        client = _UpdateRecordingClient()
        self.assertIsNone(
            npm_api.apply_domain_changes(client, [_change(12, ["a.example.com"])], self._describe))

    def test_an_empty_change_list_is_a_clean_no_op(self):
        client = _UpdateRecordingClient()
        npm_api.apply_domain_changes(client, [], self._describe)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Successful: 0")

    def test_only_http_errors_are_absorbed(self):
        # Deliberately narrow, and asserted so it stays a decision. An HTTPError
        # means NPM answered and refused this one host, which the loop can count
        # and move past. A dropped connection means the next host would fail the
        # same way, so it propagates and the command dies with a traceback and a
        # non-zero status rather than logging twenty identical failures.
        client = _UpdateRecordingClient(
            fail_ids=[13], error=requests.ConnectionError("Connection refused"))

        with self.assertRaises(requests.ConnectionError):
            npm_api.apply_domain_changes(
                client,
                [_change(12, ["a.example.com"]), _change(13, ["b.example.com"]),
                 _change(14, ["c.example.com"])],
                self._describe,
            )

        self.assertEqual(client.written_ids, [12, 13],
                         msg="the loop should stop at a connection failure")


# =============================================================================
# validate_certificate_assignment
# =============================================================================

class TestValidateCertificateAssignment(_ConsoleTestCase):
    """The guard in front of `bulk-update certificate_id`.

    NPM wraps its whole `listen 443 ssl` block in a conditional on the linked
    certificate, so a host pointed at an ID that no longer exists is rendered
    with no TLS listener at all: no error, no warning, the site simply stops
    answering on 443.
    """

    _HOSTS = [
        {"id": 12, "domain_names": ["app.example.com"]},
        {"id": 13, "domain_names": ["api.internal.lan"]},
    ]

    def _cert(self, domains, expires=timedelta(days=90, minutes=5)):
        return {"id": 4, "domain_names": domains, "expires_on": _expires_in(expires)}

    def test_no_certificate_requested_needs_no_lookup(self):
        # `--cert none` clears the link. There is nothing to validate, and a
        # round trip for it would just be one more thing that can fail.
        client = _CertLookupClient()

        self.assertIs(npm_api.validate_certificate_assignment(client, None, self._HOSTS), True)

        self.assertEqual(client.lookups, [])
        self.assertEqual(self.console.text, "")

    def test_a_missing_certificate_is_refused(self):
        # The one case that returns False. Everything else is advisory.
        client = _CertLookupClient(
            error=requests.HTTPError("404 Not Found", response=_FakeResponse(404)))

        self.assertIs(npm_api.validate_certificate_assignment(client, 99, self._HOSTS), False)

        self.assertEqual(client.lookups, [99])
        self.assertPrinted("Certificate 99 does not exist")
        self.assertPrinted("no TLS listener")

    def test_a_covered_host_is_approved_quietly(self):
        client = _CertLookupClient(self._cert(["*.example.com", "*.internal.lan"]))

        self.assertIs(npm_api.validate_certificate_assignment(client, 4, self._HOSTS), True)

        self.assertNotPrinted("not covered")
        self.assertNotPrinted("coverage not verified")

    def test_uncovered_domains_warn_without_blocking(self):
        # Advisory on purpose: NPM accepts the assignment, and a certificate
        # about to be reissued with the missing SAN is a real workflow. The
        # warning names the host and the domain so the operator can judge.
        client = _CertLookupClient(self._cert(["*.example.com"]))

        self.assertIs(npm_api.validate_certificate_assignment(client, 4, self._HOSTS), True)

        self.assertPrinted("Host 13: not covered")
        self.assertPrinted("api.internal.lan")
        self.assertNotPrinted("Host 12: not covered")

    def test_unusable_metadata_is_cannot_tell_rather_than_mismatch(self):
        # NPM keeps domain_names on a certificate as metadata and never consults
        # it when serving TLS, so for uploaded certs it drifts into junk. Reading
        # "*.internal," as a mismatch would paper every run with warnings about
        # certificates that are in fact correct.
        client = _CertLookupClient(self._cert(["*.internal,"]))

        self.assertIs(npm_api.validate_certificate_assignment(client, 4, self._HOSTS), True)

        self.assertPrinted("coverage not verified")
        self.assertNotPrinted("not covered")

    def test_an_empty_domain_list_is_also_cannot_tell(self):
        client = _CertLookupClient(self._cert([]))

        self.assertIs(npm_api.validate_certificate_assignment(client, 4, self._HOSTS), True)

        self.assertPrinted("coverage not verified")
        self.assertPrinted("empty")

    def test_a_real_mismatch_is_still_reported_when_junk_sits_beside_it(self):
        # One usable entry makes the answer definite for the domains it fails to
        # match, so the warning must not be downgraded to a note.
        client = _CertLookupClient(self._cert(["*.internal,", "*.example.com"]))

        npm_api.validate_certificate_assignment(client, 4, self._HOSTS)

        self.assertPrinted("Host 13: not covered")

    def test_an_expired_certificate_is_surfaced(self):
        # Assigning an expired certificate is legal and leaves every browser
        # refusing the site, so the state has to be visible before confirming.
        client = _CertLookupClient(
            self._cert(["*.example.com", "*.internal.lan"], timedelta(days=-10, minutes=-5)))

        self.assertIs(npm_api.validate_certificate_assignment(client, 4, self._HOSTS), True)

        self.assertPrinted("EXPIRED")

    def test_a_certificate_expiring_soon_is_surfaced(self):
        client = _CertLookupClient(
            self._cert(["*.example.com", "*.internal.lan"], timedelta(days=7, minutes=5)))

        npm_api.validate_certificate_assignment(client, 4, self._HOSTS)

        self.assertPrinted("7d LEFT")

    def test_an_unreadable_expiry_is_not_reported_as_expired(self):
        client = _CertLookupClient({"id": 4, "domain_names": ["*.example.com"],
                                    "expires_on": None})

        npm_api.validate_certificate_assignment(client, 4, self._HOSTS)

        self.assertPrinted("UNKNOWN")
        self.assertNotPrinted("EXPIRED")

    def test_the_certificate_is_fetched_once_however_many_hosts(self):
        client = _CertLookupClient(self._cert(["*.example.com"]))

        npm_api.validate_certificate_assignment(client, 4, self._HOSTS * 10)

        self.assertEqual(client.lookups, [4])

    def test_no_hosts_still_reports_the_certificate(self):
        # bulk-update can reach here with a selection that later turns out to be
        # empty; the summary line should still print rather than the function
        # falling over on an empty loop.
        client = _CertLookupClient(self._cert(["*.example.com"]))

        self.assertIs(npm_api.validate_certificate_assignment(client, 4, []), True)

        self.assertPrinted("Certificate 4")

    def test_null_domain_names_warns_rather_than_crashing(self):
        # Regression. This read `", ".join(cert.get("domain_names", []))`, and
        # cert.get returns None rather than the default when NPM sends the key
        # holding an explicit null — str.join(None) then raised "TypeError: can
        # only join an iterable". cert_covers_domain already guarded the same
        # shape (see test_null_domain_list_is_unknown), so the null is a shape
        # this codebase expects.
        #
        # It mattered more than a cosmetic nit because this is the
        # deleted-certificate guard: it raised before looking at coverage, from
        # inside `bulk-update certificate_id`, and the traceback was
        # indistinguishable from a bug in the tool rather than a warning about
        # the certificate.
        client = _CertLookupClient({"id": 4, "domain_names": None, "expires_on": None})

        self.assertIs(npm_api.validate_certificate_assignment(client, 4, self._HOSTS), True)

        # "empty" rather than a crash, and coverage still could not be judged
        self.assertPrinted("Certificate 4")
        self.assertPrinted("empty")


# =============================================================================
# host merge: shared doubles
# =============================================================================

def _merge_host(host_id, domains, **overrides):
    """A proxy host as NPM returns it, carrying every field merge inspects.

    Spelled out in full rather than built per test so that a test overriding
    one field is testing that field alone: an absent key reads as None, which
    _comparable folds to 0, so a partial fixture quietly agrees with a zero on
    the other side and the difference under test never appears.
    """
    host = {
        "id": host_id,
        "domain_names": list(domains),
        "forward_scheme": "http",
        "forward_host": "10.0.0.5",
        "forward_port": 8080,
        "certificate_id": None,
        "ssl_forced": False,
        "hsts_enabled": False,
        "http2_support": False,
        "allow_websocket_upgrade": True,
        "block_exploits": True,
        "caching_enabled": False,
        "access_list_id": 0,
        "advanced_config": "",
        "locations": [],
        "enabled": True,
        "meta": {"letsencrypt_agree": True},
    }
    host.update(overrides)
    return host


def _npm_http_error(status=400, message="Domain already in use"):
    """An HTTPError carrying a body in NPM's shape, for format_http_error."""
    return requests.HTTPError(
        f"{status} Error",
        response=_FakeResponse(status, json_body={"error": {"message": message}}))


class _MergeConfig:
    """The one Config attribute write_merge_snapshot reads.

    Config.backup_dir is a read-only property derived from data_dir and the
    server address, so pointing a real Config at a temp directory would mean
    reproducing that whole layout to exercise one mkdir and one filename.
    """

    def __init__(self, backup_dir):
        self.backup_dir = str(backup_dir)


class _MergeClient(_StubClient):
    """Every write merge makes, on one list, in the order it made them.

    Merge's central safety property is an ordering rather than an argument:
    each source is deleted before the target claims its names, because NPM
    refuses to let two hosts hold the same domain. Recording deletes, updates
    and re-creates separately would lose exactly the fact under test, so they
    share a list and the assertions read the interleaving off it.
    """

    def __init__(self, target, sources, backup_dir, *, delete_error_ids=(),
                 delete_refuse_ids=(), update_fail_calls=(), error=None,
                 restore_error=None, certificate=None):
        self.config = _MergeConfig(backup_dir)
        self.hosts = [target] + list(sources)
        self.calls = []
        self.cert_lookups = []
        self._delete_error_ids = set(delete_error_ids)
        self._delete_refuse_ids = set(delete_refuse_ids)
        # Keyed by call number, not host ID: every update in a merge is aimed
        # at the same --into host, so the ID cannot distinguish them.
        self._update_fail_calls = set(update_fail_calls)
        self._error = error or _npm_http_error()
        self._restore_error = restore_error
        self._certificate = certificate
        self._update_count = 0
        self._next_new_id = 100

    # --- reads -------------------------------------------------------------

    def list_hosts(self):
        return self.hosts

    def get_host(self, host_id):
        for host in self.hosts:
            if host.get("id") == host_id:
                return host
        raise requests.HTTPError("404 Not Found", response=_FakeResponse(404))

    def get_certificate(self, cert_id):
        self.cert_lookups.append(cert_id)
        if self._certificate is None:
            raise requests.HTTPError("404 Not Found", response=_FakeResponse(404))
        return self._certificate

    # --- writes ------------------------------------------------------------

    def delete_host(self, host_id):
        self.calls.append(("delete", host_id))
        if host_id in self._delete_error_ids:
            raise self._error
        # The real delete_host reports a refusal by returning False rather than
        # raising: it checks the status code itself and never calls
        # raise_for_status.
        return host_id not in self._delete_refuse_ids

    def update_host(self, host_id, updates):
        self._update_count += 1
        self.calls.append(("update", host_id, updates))
        if self._update_count in self._update_fail_calls:
            raise self._error
        return {"id": host_id, **updates}

    def create_host_from(self, source, overrides):
        self.calls.append(("create", source.get("id")))
        if self._restore_error is not None:
            raise self._restore_error
        # NPM assigns the ID on create; the old one cannot be asked for.
        self._next_new_id += 1
        return dict(source, id=self._next_new_id)

    # --- views the assertions read -----------------------------------------

    @property
    def kinds(self):
        return [call[0] for call in self.calls]

    @property
    def deleted_ids(self):
        return [call[1] for call in self.calls if call[0] == "delete"]

    @property
    def updates(self):
        return [(call[1], call[2]) for call in self.calls if call[0] == "update"]

    @property
    def recreated_ids(self):
        return [call[1] for call in self.calls if call[0] == "create"]


# =============================================================================
# _comparable
# =============================================================================

class TestComparable(unittest.TestCase):
    """NPM spells the same flag as a boolean on one host and an integer on the
    next, and uses both 0 and null for "nothing linked", so a plain != between
    two hosts reports differences that are not there."""

    def test_absent_and_off_all_fold_to_zero(self):
        for given in (False, 0, None):
            with self.subTest(given=given):
                self.assertEqual(npm_api._comparable(given), 0)

    def test_true_folds_to_one(self):
        self.assertEqual(npm_api._comparable(True), 1)

    def test_a_boolean_matches_its_integer_spelling(self):
        self.assertEqual(npm_api._comparable(True), npm_api._comparable(1))
        self.assertEqual(npm_api._comparable(False), npm_api._comparable(0))

    def test_other_values_pass_through_unchanged(self):
        for given in (5, 8080, "http", "10.0.0.5", "", [], {"a": 1}):
            with self.subTest(given=given):
                self.assertEqual(npm_api._comparable(given), given)

    def test_an_empty_string_is_not_folded_into_absent(self):
        # Only advanced_config mixes "" with null, and describe_host_differences
        # handles that field separately. Folding "" here would make an empty
        # forward_host look like a missing one.
        self.assertNotEqual(npm_api._comparable(""), npm_api._comparable(None))


# =============================================================================
# _forward_label
# =============================================================================

class TestForwardLabel(unittest.TestCase):
    """Where a host's traffic ends up, as one printable string."""

    def test_renders_scheme_host_and_port(self):
        self.assertEqual(
            npm_api._forward_label(_merge_host(12, ["app.example.com"])),
            "http://10.0.0.5:8080",
        )

    def test_https_upstream(self):
        host = _merge_host(12, ["app.example.com"], forward_scheme="https",
                           forward_host="backend.internal.lan", forward_port=443)
        self.assertEqual(npm_api._forward_label(host),
                         "https://backend.internal.lan:443")

    def test_absent_fields_degrade_rather_than_raise(self):
        # It only ever appears inside a message that is already reporting an
        # oddity, so a host missing forward_scheme should print "None" and let
        # the operator see that, not abort the refusal that was being explained.
        self.assertEqual(npm_api._forward_label({}), "None://None:None")


# =============================================================================
# describe_host_differences
# =============================================================================

class TestDescribeHostDifferences(unittest.TestCase):
    """What a source's domains lose by moving onto the target.

    Advisory only — merge never refuses over any of it, because adopting the
    target's configuration is precisely what --into asks for. The property that
    matters is that it neither invents differences nor hides real ones.
    """

    # One differing pair per notable field, so each label can be provoked on
    # its own.
    _DIFFERING = {
        "enabled": (True, False),
        "certificate_id": (4, 5),
        "ssl_forced": (True, False),
        "hsts_enabled": (True, False),
        "http2_support": (True, False),
        "allow_websocket_upgrade": (True, False),
        "block_exploits": (True, False),
        "caching_enabled": (True, False),
        "access_list_id": (2, 3),
        "advanced_config": ("add_header X-A a;", "add_header X-B b;"),
        "locations": ([], [{"path": "/api"}]),
    }

    def _pair(self, **overrides):
        target_overrides = {k: v[0] for k, v in overrides.items()}
        source_overrides = {k: v[1] for k, v in overrides.items()}
        return (_merge_host(12, ["app.example.com"], **target_overrides),
                _merge_host(13, ["old.example.com"], **source_overrides))

    def test_identical_hosts_differ_in_nothing(self):
        target, source = self._pair()
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_differing_domain_names_are_not_a_difference(self):
        # The domains are the whole point of the merge, not something lost by it.
        target = _merge_host(12, ["app.example.com"])
        source = _merge_host(13, ["old.example.com", "legacy.example.com"])
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_every_notable_field_is_detected_on_its_own(self):
        labels = dict(npm_api.MERGE_NOTABLE_FIELDS)
        # Fails when a field is added to MERGE_NOTABLE_FIELDS without a case
        # here, rather than letting the new field go quietly untested.
        self.assertEqual(set(self._DIFFERING), set(labels))

        for key, values in self._DIFFERING.items():
            with self.subTest(field=key):
                target, source = self._pair(**{key: values})
                self.assertEqual(npm_api.describe_host_differences(target, source),
                                 [labels[key]])

    def test_differences_are_reported_in_field_order(self):
        target, source = self._pair(ssl_forced=(True, False),
                                    caching_enabled=(True, False),
                                    enabled=(True, False))
        self.assertEqual(npm_api.describe_host_differences(target, source),
                         ["enabled", "force SSL", "caching"])

    def test_locations_are_compared_by_count_not_by_content(self):
        # Deliberately shallow. A custom location is a nested object with its
        # own forward target and nginx snippet; diffing it properly would be a
        # feature of its own, and the preview only needs to say "this host has
        # custom locations and the target's will be used instead". Two
        # different single-location lists therefore read as the same.
        target, source = self._pair(
            locations=([{"path": "/api", "forward_host": "10.0.0.5"}],
                       [{"path": "/admin", "forward_host": "10.9.9.9"}]))
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_a_differing_number_of_locations_is_reported(self):
        target, source = self._pair(locations=([], [{"path": "/api"}]))
        self.assertEqual(npm_api.describe_host_differences(target, source),
                         ["custom locations"])

    def test_null_and_empty_locations_are_the_same(self):
        target, source = self._pair(locations=(None, []))
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_advanced_config_unset_in_either_spelling_is_the_same(self):
        # NPM returns null for a host that never had one and "" for a host whose
        # config was cleared, and the UI leaves a stray newline behind.
        for left, right in ((None, ""), ("", "  "), (None, "\n"), (None, None)):
            with self.subTest(left=left, right=right):
                target, source = self._pair(advanced_config=(left, right))
                self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_advanced_config_ignores_surrounding_whitespace(self):
        target, source = self._pair(
            advanced_config=("add_header X-A a;", "  add_header X-A a;\n"))
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_advanced_config_reports_a_real_text_change(self):
        target, source = self._pair(advanced_config=("", "add_header X-A a;"))
        self.assertEqual(npm_api.describe_host_differences(target, source),
                         ["advanced config"])

    def test_certificate_id_zero_and_null_both_mean_no_certificate(self):
        target, source = self._pair(certificate_id=(0, None))
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_access_list_id_zero_and_null_both_mean_no_access_list(self):
        target, source = self._pair(access_list_id=(0, None))
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_ssl_forced_false_and_zero_are_the_same_setting(self):
        target, source = self._pair(ssl_forced=(False, 0))
        self.assertEqual(npm_api.describe_host_differences(target, source), [])

    def test_ssl_forced_true_and_one_are_the_same_setting(self):
        target, source = self._pair(ssl_forced=(True, 1))
        self.assertEqual(npm_api.describe_host_differences(target, source), [])


# =============================================================================
# write_merge_snapshot
# =============================================================================

class TestWriteMergeSnapshot(_WorkdirTestCase):
    """The floor under a merge.

    Merge deletes hosts and NPM has no undo. The in-process rollback can only
    recreate a source under a new ID, and cannot run at all if the process is
    killed partway through, so this file is the only thing guaranteeing the
    original configuration still exists somewhere.
    """

    def _write(self, target, sources, directory=None):
        config = _MergeConfig(directory or self.workdir)
        return npm_api.write_merge_snapshot(config, target, sources)

    def test_lands_in_the_backup_directory_named_for_the_target(self):
        path = self._write(_merge_host(12, ["app.example.com"]),
                           [_merge_host(13, ["old.example.com"])])

        self.assertEqual(path.parent, self.workdir)
        self.assertTrue(path.name.startswith("pre_merge_12_"), path.name)
        self.assertTrue(path.name.endswith(".json"), path.name)

    def test_is_owner_readable_only(self):
        # advanced_config carries auth headers and internal hostnames, and this
        # file holds it for every host the merge touches.
        path = self._write(_merge_host(12, ["app.example.com"]),
                           [_merge_host(13, ["old.example.com"])])
        self.assertEqual(_mode(path), 0o600)

    def test_holds_the_target_and_every_source_verbatim(self):
        target = _merge_host(12, ["app.example.com"], advanced_config="add_header X-A a;")
        sources = [_merge_host(13, ["old.example.com"], certificate_id=7),
                   _merge_host(14, ["legacy.example.com"], locations=[{"path": "/api"}])]

        payload = json.loads(self._write(target, sources).read_text())

        self.assertEqual(payload["target"], target)
        self.assertEqual(payload["sources"], sources)
        self.assertIn("created", payload)

    def test_creates_the_backup_directory_when_it_does_not_exist(self):
        # First run on a fresh install reaches here before anything else has
        # written a backup.
        directory = self.workdir / "nested" / "backups"
        path = self._write(_merge_host(12, ["app.example.com"]), [], directory)

        self.assertTrue(path.exists())
        self.assertEqual(path.parent, directory)


# =============================================================================
# _restore_merge_source
# =============================================================================

class _RestoreClient(_StubClient):
    """Recreates a host, or refuses to."""

    def __init__(self, error=None, new_id=101):
        self.error = error
        self.new_id = new_id
        self.calls = []

    def create_host_from(self, source, overrides):
        self.calls.append((source.get("id"), overrides))
        if self.error is not None:
            raise self.error
        return dict(source, id=self.new_id)


class TestRestoreMergeSource(_ConsoleTestCase):
    """Undoing one deleted source when the target then refused its domains.

    Everything it has to say is said in print(), and each message exists
    because the operator has to act on it by hand afterwards.
    """

    _SOURCE = _merge_host(13, ["old.example.com", "legacy.example.com"])
    _SNAPSHOT = Path("/var/tmp/npm-api/backups/pre_merge_12_2026_08_24__10_00_00.json")

    def test_recreates_the_host_from_its_recorded_configuration(self):
        client = _RestoreClient()

        npm_api._restore_merge_source(client, self._SOURCE, self._SNAPSHOT)

        self.assertEqual(client.calls, [(13, {})])

    def test_reports_the_new_id_and_that_it_is_a_new_one(self):
        # The ID changes and nothing can stop it changing, so the message has to
        # say so: scripts, runbooks and the snapshot itself still name the old
        # one, and NPM offers no way to ask for a particular ID on create.
        npm_api._restore_merge_source(_RestoreClient(new_id=101), self._SOURCE,
                                      self._SNAPSHOT)

        self.assertPrinted("Host 13 recreated as host 101")
        self.assertPrinted("NPM assigns a new ID")

    def test_a_failed_restore_names_the_domains_and_the_snapshot(self):
        # The worst outcome merge can reach: a host is gone, its domains answer
        # nothing, and the only remaining copy of its configuration is on disk.
        # Printing the path is what makes that copy findable.
        client = _RestoreClient(error=_npm_http_error(500, "Internal Error"))

        npm_api._restore_merge_source(client, self._SOURCE, self._SNAPSHOT)

        self.assertPrinted("ROLLBACK FAILED")
        self.assertPrinted("Internal Error")
        self.assertPrinted("old.example.com, legacy.example.com")
        self.assertPrinted(str(self._SNAPSHOT))

    def test_an_npm_error_is_handled_like_an_http_error(self):
        # NPMError is this tool's own operational failure — an unreachable
        # server, a reply that is not NPM's. Letting it out would abandon the
        # remaining sources mid-merge with one host already deleted.
        client = _RestoreClient(error=npm_api.NPMError("NPM is unreachable"))

        npm_api._restore_merge_source(client, self._SOURCE, self._SNAPSHOT)

        self.assertPrinted("ROLLBACK FAILED")
        self.assertPrinted("NPM is unreachable")

    def test_a_failed_restore_returns_rather_than_raising(self):
        client = _RestoreClient(error=_npm_http_error(500))
        self.assertIsNone(
            npm_api._restore_merge_source(client, self._SOURCE, self._SNAPSHOT))


# =============================================================================
# host merge
# =============================================================================

class _MergeCommandTestCase(_WorkdirTestCase, _ConsoleTestCase):
    """Runs host_merge end to end against a stub, on a temp backup directory.

    host_merge is a Typer command and also a plain function, and is called here
    as the latter. Every argument is passed explicitly: the defaults on the
    signature are typer.OptionInfo objects rather than the values they
    describe, so an omitted argument would arrive as an OptionInfo and be
    truthy.
    """

    def _client(self, target, sources, **kwargs):
        return _MergeClient(target, sources, self.workdir, **kwargs)

    def _merge(self, client, **overrides):
        options = dict(into=12, host_ids=None, pattern=None, cert=None,
                       allow_different_targets=False, preview=False, yes=True,
                       interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_merge(**options)

    def _merge_expecting_exit(self, client, **overrides):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._merge(client, **overrides)
        return caught.exception


class TestHostMergeSafety(_MergeCommandTestCase):
    """What merge must refuse to do.

    It is the only command in the tool that deletes a host the user did not
    name for deletion, and NPM has no undo, so the interesting assertions are
    about calls that must not happen.
    """

    def test_a_pattern_matching_the_target_never_deletes_the_target(self):
        # --pattern will routinely match the --into host too, since it is being
        # merged into precisely because it shares a naming scheme with the
        # others. Deleting the host that was meant to survive is the one outcome
        # merge must never produce, so the target is dropped from the sources
        # rather than the run being refused.
        target = _merge_host(12, ["app.example.com"])
        source = _merge_host(13, ["old.example.com"])
        client = self._client(target, [source])

        self._merge(client, pattern="example.com")

        self.assertEqual(client.deleted_ids, [13])
        self.assertNotIn(12, client.deleted_ids)

    def test_only_the_target_matching_leaves_nothing_to_do(self):
        target = _merge_host(12, ["app.example.com"])
        other = _merge_host(13, ["unrelated.internal.lan"])
        client = self._client(target, [other])

        self._merge(client, pattern="app.example.com")

        self.assertEqual(client.calls, [])
        self.assertPrinted("Nothing left to merge into host 12")

    def test_no_matching_hosts_writes_nothing(self):
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._merge(client, pattern="nothing.example.net")

        self.assertEqual(client.calls, [])
        self.assertPrinted("No hosts selected")

    def test_a_missing_into_host_exits_1_before_touching_anything(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        exit_exc = self._merge_expecting_exit(client, into=99, host_ids="13")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Host ID 99 not found")

    def test_a_differing_forward_target_refuses_and_changes_nothing(self):
        # Merging moves the source's domains onto the target's server block, so
        # a source pointing at another backend would have its traffic silently
        # repointed. That is a refusal rather than a warning because nothing in
        # the request says the user meant it.
        for field, value in (("forward_scheme", "https"),
                             ("forward_host", "10.9.9.9"),
                             ("forward_port", 9090)):
            with self.subTest(field=field):
                client = self._client(_merge_host(12, ["app.example.com"]),
                                      [_merge_host(13, ["old.example.com"],
                                                   **{field: value})])

                exit_exc = self._merge_expecting_exit(client, host_ids="13")

                self.assertEqual(exit_exc.exit_code, 1)
                self.assertEqual(client.deleted_ids, [])
                self.assertEqual(client.updates, [])

    def test_the_refusal_names_both_upstreams_and_the_escape_hatch(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"],
                                           forward_port=9090)])

        self._merge_expecting_exit(client, host_ids="13")

        self.assertPrinted("http://10.0.0.5:9090")
        self.assertPrinted("http://10.0.0.5:8080")
        self.assertPrinted("differs on port")
        self.assertPrinted("--allow-different-targets")

    def test_allow_different_targets_proceeds_with_a_warning(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"],
                                           forward_scheme="https")])

        self._merge(client, host_ids="13", allow_different_targets=True)

        self.assertEqual(client.deleted_ids, [13])
        self.assertPrinted("differs on scheme")
        self.assertNotPrinted("Refusing to merge")

    def test_an_unwritable_snapshot_stops_the_merge_before_the_first_delete(self):
        # The snapshot is the only record of what the sources looked like, and
        # the deletes are irreversible, so the ordering is the guarantee: no
        # host is removed until its configuration is on disk.
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        with mock.patch.object(npm_api, "write_merge_snapshot",
                               side_effect=OSError("Read-only file system")):
            exit_exc = self._merge_expecting_exit(client, host_ids="13")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Read-only file system")
        self.assertPrinted("Refusing to delete hosts")


class TestHostMergeOrdering(_MergeCommandTestCase):
    """Delete first, then extend the target, one source at a time.

    NPM will not let two hosts hold the same domain name, so the name has to be
    free before the target can claim it. Doing it per source rather than in one
    pass means a failure strands one host instead of all of them.
    """

    def test_each_source_is_deleted_before_the_target_takes_its_domains(self):
        target = _merge_host(12, ["app.example.com"])
        client = self._client(target, [_merge_host(13, ["a.example.com"]),
                                       _merge_host(14, ["b.example.com"])])

        self._merge(client, host_ids="13,14")

        self.assertEqual(client.kinds, ["delete", "update", "delete", "update"])
        self.assertEqual(client.deleted_ids, [13, 14])

    def test_the_target_grows_one_source_at_a_time(self):
        target = _merge_host(12, ["app.example.com"])
        client = self._client(target, [_merge_host(13, ["a.example.com"]),
                                       _merge_host(14, ["b.example.com"])])

        self._merge(client, host_ids="13,14")

        self.assertEqual([payload["domain_names"] for _, payload in client.updates],
                         [["app.example.com", "a.example.com"],
                          ["app.example.com", "a.example.com", "b.example.com"]])

    def test_every_update_is_aimed_at_the_into_host(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com"]),
                               _merge_host(14, ["b.example.com"])])

        self._merge(client, host_ids="13,14")

        self.assertEqual([host_id for host_id, _ in client.updates], [12, 12])

    def test_the_union_is_target_first_and_deduped(self):
        # A domain carried by two sources, and one spelled in another case, both
        # have to collapse: NPM would otherwise store the same name twice on one
        # host and nginx would serve whichever server_name it saw first.
        target = _merge_host(12, ["app.example.com", "www.example.com"])
        client = self._client(target, [
            _merge_host(13, ["dup.example.com", "APP.example.com"]),
            _merge_host(14, ["dup.example.com", "last.example.com"]),
        ])

        self._merge(client, host_ids="13,14")

        _, final = client.updates[-1]
        self.assertEqual(final["domain_names"],
                         ["app.example.com", "www.example.com", "dup.example.com",
                          "last.example.com"])


class TestHostMergeCertificate(_MergeCommandTestCase):
    """One NPM host is one nginx server block with one certificate, so the
    merged host's certificate has to cover every domain in the result."""

    _CERT = {"id": 4, "domain_names": ["*.example.com"],
             "expires_on": _expires_in(timedelta(days=90))}

    def test_omitting_cert_inherits_the_targets_certificate(self):
        target = _merge_host(12, ["app.example.com"], certificate_id=4)
        client = self._client(target, [_merge_host(13, ["old.example.com"])],
                              certificate=self._CERT)

        self._merge(client, host_ids="13")

        _, payload = client.updates[0]
        self.assertEqual(payload["certificate_id"], 4)
        self.assertEqual(client.cert_lookups, [4])

    def test_omitting_cert_leaves_the_ssl_flags_alone(self):
        # Nothing about the certificate is changing, so ssl_forced and HSTS are
        # not in the payload at all and update_host carries the target's current
        # values through untouched.
        target = _merge_host(12, ["app.example.com"], certificate_id=4,
                             ssl_forced=True, hsts_enabled=True)
        client = self._client(target, [_merge_host(13, ["old.example.com"])],
                              certificate=self._CERT)

        self._merge(client, host_ids="13")

        _, payload = client.updates[0]
        self.assertNotIn("ssl_forced", payload)
        self.assertNotIn("hsts_enabled", payload)

    def test_cert_overrides_the_targets_certificate(self):
        target = _merge_host(12, ["app.example.com"], certificate_id=4)
        client = self._client(target, [_merge_host(13, ["old.example.com"])],
                              certificate=dict(self._CERT, id=5))

        self._merge(client, host_ids="13", cert="5")

        _, payload = client.updates[0]
        self.assertEqual(payload["certificate_id"], 5)
        self.assertEqual(client.cert_lookups, [5])

    def test_cert_none_also_clears_force_ssl_and_hsts(self):
        # An SSL-forced host with no certificate is strictly worse than plain
        # HTTP: NPM renders the redirect to https:// but omits the whole
        # `listen 443 ssl` block, so every request bounces to a port nothing is
        # listening on.
        target = _merge_host(12, ["app.example.com"], certificate_id=4,
                             ssl_forced=True, hsts_enabled=True)
        client = self._client(target, [_merge_host(13, ["old.example.com"])])

        self._merge(client, host_ids="13", cert="none")

        _, payload = client.updates[0]
        self.assertIsNone(payload["certificate_id"])
        self.assertIs(payload["ssl_forced"], False)
        self.assertIs(payload["hsts_enabled"], False)

    def test_a_certificate_that_does_not_exist_stops_the_merge(self):
        # The failure mode this whole tool was written to prevent, in its worst
        # form. NPM wraps the entire `listen 443 ssl` block in a conditional on
        # the linked certificate, so a host pointed at an ID that is not there
        # is rendered with no TLS listener at all and reports no error.
        #
        # validate_certificate_assignment returns False for exactly this case,
        # and merge originally ignored the return: it printed the refusal, then
        # deleted the sources anyway and pointed the survivor at the dead ID.
        # Irreversible, silent, and reported as a success.
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        exit_exception = self._merge_expecting_exit(client, host_ids="13", cert="99")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Certificate 99 does not exist")

    def test_an_inherited_certificate_that_is_gone_also_stops_the_merge(self):
        # Same refusal when the dead ID came from the --into host rather than
        # from --cert. Merge is actively reassigning that certificate to a
        # larger set of domains, so "the target already had it" does not make
        # writing it again safe.
        target = _merge_host(12, ["app.example.com"], certificate_id=4)
        client = self._client(target, [_merge_host(13, ["old.example.com"])])

        exit_exception = self._merge_expecting_exit(client, host_ids="13")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("--cert none")

    def test_cert_none_needs_no_certificate_lookup(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        self._merge(client, host_ids="13", cert="none")

        self.assertEqual(client.cert_lookups, [])

    def test_sources_serving_https_today_are_named_when_the_target_has_none(self):
        # Their domains silently drop to HTTP-only, which no other message in
        # the run would mention.
        target = _merge_host(12, ["app.example.com"], certificate_id=None)
        client = self._client(target, [_merge_host(13, ["old.example.com"],
                                                   certificate_id=7)])

        self._merge(client, host_ids="13")

        self.assertPrinted("serve HTTPS today")
        self.assertPrinted("HTTP-only")


class TestHostMergeFailures(_MergeCommandTestCase):
    """One source failing must not abandon the rest, and must not exit 0."""

    def test_a_refused_delete_leaves_the_target_alone_for_that_source(self):
        # delete_host reports a refusal by returning False rather than raising,
        # so a caller testing only for exceptions would go on to hand the
        # target a domain the source still holds and NPM would reject it.
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com"]),
                               _merge_host(14, ["b.example.com"])],
                              delete_refuse_ids={13})

        exit_exc = self._merge_expecting_exit(client, host_ids="13,14")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.kinds, ["delete", "delete", "update"])
        _, payload = client.updates[0]
        self.assertEqual(payload["domain_names"], ["app.example.com", "b.example.com"])
        self.assertPrinted("NPM refused the delete")

    def test_a_failed_delete_is_not_rolled_back(self):
        # Nothing was destroyed, so there is nothing to recreate; calling the
        # rollback here would duplicate a host that still exists.
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com"])],
                              delete_error_ids={13})

        exit_exc = self._merge_expecting_exit(client, host_ids="13")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.kinds, ["delete"])
        self.assertPrinted("could not delete")

    def test_a_rejected_update_recreates_the_source_and_carries_on(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com"]),
                               _merge_host(14, ["b.example.com"])],
                              update_fail_calls={1})

        exit_exc = self._merge_expecting_exit(client, host_ids="13,14")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.kinds,
                         ["delete", "update", "create", "delete", "update"])
        self.assertEqual(client.recreated_ids, [13])

    def test_a_rolled_back_source_does_not_leak_into_later_updates(self):
        # Host 13's domains never reached the target, so the second update must
        # ask for the target's own names plus host 14's and nothing else —
        # otherwise it would claim a name the recreated host now holds.
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com"]),
                               _merge_host(14, ["b.example.com"])],
                              update_fail_calls={1})

        self._merge_expecting_exit(client, host_ids="13,14")

        _, second = client.updates[1]
        self.assertEqual(second["domain_names"], ["app.example.com", "b.example.com"])

    def test_an_npm_error_from_the_update_is_caught_like_an_http_error(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com"])],
                              update_fail_calls={1},
                              error=npm_api.NPMError("NPM is unreachable"))

        exit_exc = self._merge_expecting_exit(client, host_ids="13")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.recreated_ids, [13])
        self.assertPrinted("NPM is unreachable")

    def test_a_failed_rollback_names_the_lost_domains_and_the_snapshot(self):
        # The host is gone, its domains answer nothing, and the snapshot is the
        # only surviving copy of its configuration. Both facts have to be in the
        # same message, because that is the one the operator acts on.
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com", "alias.example.com"])],
                              update_fail_calls={1},
                              restore_error=_npm_http_error(500, "Internal Error"))

        self._merge_expecting_exit(client, host_ids="13")

        self.assertPrinted("ROLLBACK FAILED")
        lost = [line for line in self.console.lines if "not being served" in line]
        self.assertEqual(len(lost), 1, self.console.text)
        self.assertIn("a.example.com, alias.example.com", lost[0])
        self.assertIn("pre_merge_12_", lost[0])
        self.assertIn(str(self.workdir), lost[0])

    def test_partial_failure_exits_1_with_the_other_sources_merged(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["a.example.com"]),
                               _merge_host(14, ["b.example.com"])],
                              delete_error_ids={13})

        exit_exc = self._merge_expecting_exit(client, host_ids="13,14")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.updates and client.updates[-1][1]["domain_names"],
                         ["app.example.com", "b.example.com"])
        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")


class TestHostMergeCleanRun(_MergeCommandTestCase):
    """The ordinary path: nothing fails, nothing is refused, exit 0."""

    def test_a_clean_run_returns_normally(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        self._merge(client, host_ids="13")

        self.assertEqual(client.kinds, ["delete", "update"])
        self.assertPrinted("Successful: 1")
        self.assertNotPrinted("Failed:")

    def test_the_resulting_domain_list_is_reported(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        self._merge(client, host_ids="13")

        self.assertPrinted("Host 12 now serves: app.example.com, old.example.com")

    def test_the_run_leaves_a_snapshot_on_disk(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        self._merge(client, host_ids="13")

        written = list(self.workdir.glob("pre_merge_12_*.json"))
        self.assertEqual(len(written), 1, written)
        self.assertEqual(_mode(written[0]), 0o600)
        self.assertEqual(
            [source["id"] for source in json.loads(written[0].read_text())["sources"]],
            [13])

    def test_the_preview_announces_the_deletes_before_they_happen(self):
        client = self._client(_merge_host(12, ["app.example.com"]),
                              [_merge_host(13, ["old.example.com"])])

        self._merge(client, host_ids="13", preview=True)

        self.assertPrinted("Merge Preview")
        self.assertPrinted("Deleting host(s) 13")
        self.assertPrinted("no way to undo")


if __name__ == "__main__":
    unittest.main()
