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
import os
import re
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


# Rich decides whether to emit colour from the stream it is writing to, and
# that decision has changed between releases: on rich 13 a captured StringIO
# gets plain text, on rich 15 the same capture came back carrying SGR codes.
# Any assertion that looks for a bare character in rendered output is at the
# mercy of that, because the reset sequence \x1b[0m contains a literal "0".
# Strip the codes before asserting so the suite tests what was rendered rather
# than which version of rich rendered it.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """`text` with any ANSI colour/style sequences removed."""
    return _ANSI_SGR.sub("", text)


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


def assert_owner_only(case, path):
    """Assert mode 0600, or skip where the platform cannot express it.

    Windows has no POSIX permission bits. CPython's os.open honours only the
    write bit there, so a file created with 0o600 reads back as 0o666 and
    every other account on the machine can read it — which for the private
    keys and pre-merge snapshots these tests cover is the whole point of the
    assertion.

    Skipped rather than relaxed, and skipped rather than the suite quietly
    dropping the check: this is not a flaky test or a Windows quirk to work
    around, it is a guarantee the tool genuinely does not provide there.
    SECURITY.md says so. Anything the test asserted before this point has
    already run, so the non-permission coverage still applies.
    """
    if os.name == "nt":
        case.skipTest("POSIX permission bits are not implemented on Windows; "
                      "see SECURITY.md")
    case.assertEqual(_mode(path), 0o600)


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
# require_domain_argument
# =============================================================================

class TestRequireDomainArgument(_ConsoleTestCase):
    """The gate in front of the bulk rewriters' string surgery.

    `bulk-replace-domain "$OLD" "$NEW"` with OLD unset is one keystroke away
    from every scripted invocation, and "" is the one value that turns those
    commands into a shredder rather than a rename.
    """

    def test_a_real_domain_passes_silently(self):
        self.assertIsNone(npm_api.require_domain_argument("example.com", "The old base domain"))
        self.assertEqual(self.console.lines, [])

    def test_an_empty_string_is_refused(self):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            npm_api.require_domain_argument("", "The old base domain")
        self.assertEqual(caught.exception.exit_code, 1)

    def test_whitespace_only_is_refused_too(self):
        # A shell that expanded to a bare space is the same accident as one
        # that expanded to nothing, and strip() is what the rewriters would
        # have applied anyway.
        for given in (" ", "\t", "\n", "   "):
            with self.subTest(given=given):
                with self.assertRaises(npm_api.typer.Exit):
                    npm_api.require_domain_argument(given, "The new base domain")

    def test_the_message_names_which_argument_was_blank(self):
        # bulk-replace-domain takes two of them, so "a domain was empty" would
        # leave the operator guessing which half of the command to fix.
        with self.assertRaises(npm_api.typer.Exit):
            npm_api.require_domain_argument("", "The old base domain")
        self.assertPrinted("The old base domain is blank")

    def test_a_domain_that_only_looks_odd_is_left_alone(self):
        # Blankness only. Whether a name is well formed is a separate question
        # this helper deliberately does not answer, or a legitimate host NPM
        # already accepts would stop being editable.
        for given in ("localhost", "com", ".", "*", "ex."):
            with self.subTest(given=given):
                npm_api.require_domain_argument(given, "The new base domain")


# =============================================================================
# require_nonempty_domain_names
# =============================================================================

class TestRequireNonemptyDomainNames(_ConsoleTestCase):
    """No write may leave a host answering to nothing.

    split skips a host it would empty and bulk-remove-domain skips one too;
    update and bulk-update write domain_names straight through, and
    coerce_field_value folds every spelling of "nothing" into [], so they were
    the hole in that rule.
    """

    def test_a_populated_list_passes(self):
        npm_api.require_nonempty_domain_names("domain_names", ["app.example.com"])
        self.assertEqual(self.console.lines, [])

    def test_an_empty_list_is_refused(self):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            npm_api.require_nonempty_domain_names("domain_names", [])
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("domain_names is empty")

    def test_a_null_is_refused_as_well(self):
        # `domain_names=null` coerces to None rather than [], and reaches
        # update_host just as readily.
        with self.assertRaises(npm_api.typer.Exit):
            npm_api.require_nonempty_domain_names("domain_names", None)

    def test_every_spelling_coerce_field_value_folds_into_nothing_is_caught(self):
        # Stated through coerce_field_value rather than against a literal [],
        # so a change to how the field is parsed cannot open the hole again.
        for raw in ("", " ", ",", " , ", "[]", "null"):
            with self.subTest(raw=raw):
                value = npm_api.coerce_field_value("domain_names", raw)
                with self.assertRaises(npm_api.typer.Exit):
                    npm_api.require_nonempty_domain_names("domain_names", value)

    def test_another_field_going_empty_is_none_of_its_business(self):
        # Emptying advanced_config or locations is a legitimate edit; only
        # domain_names leaves a server block with no server_name.
        for field, value in (("locations", []), ("advanced_config", None),
                             ("forward_host", "")):
            with self.subTest(field=field):
                npm_api.require_nonempty_domain_names(field, value)
        self.assertEqual(self.console.lines, [])

    def test_a_falsy_value_that_is_not_a_list_is_not_mistaken_for_empty(self):
        # coerce_field_value returns 0 for "0", and 0 is falsy — but it is a
        # value that was typed, not an absent one, so it is NPM's to reject.
        npm_api.require_nonempty_domain_names("domain_names", 0)
        self.assertEqual(self.console.lines, [])


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
        assert_owner_only(self, path)

    def test_overwriting_a_world_readable_file_tightens_the_mode(self):
        # O_CREAT leaves an existing file's mode alone, so write_secret has to
        # unlink first. Without that, a token file created by an older version
        # would stay 0644 forever.
        path = self.workdir / "token.txt"
        path.write_text("old")
        path.chmod(0o644)

        npm_api.write_secret(path, "new")

        self.assertEqual(path.read_text(), "new")
        assert_owner_only(self, path)

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
        assert_owner_only(self, link)


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
        assert_owner_only(self, self.workdir / "example.com.key")

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

        assert_owner_only(self, self.workdir / "privkey.pem")

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
        assert_owner_only(self, keys[0])

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

    def test_a_failing_section_is_named_inside_the_file(self):
        # The document is written either way, so from the inside a section
        # whose fetch failed looks exactly like one that held nothing. restore
        # deletes what a backup says nothing about, so the file has to say it.
        class _BrokenHostsClient(_BackupClient):
            def list_hosts(self):
                raise requests.HTTPError("500 Server Error")

        _BrokenHostsClient().full_backup(str(self.workdir), include_keys=False)

        written = json.loads((self.workdir / "full_config_latest.json").read_text())
        self.assertNotIn("proxy_hosts", written)
        self.assertIn("proxy_hosts", written["incomplete_sections"])
        self.assertIn("500 Server Error", written["incomplete_sections"]["proxy_hosts"])

    def test_the_recorded_key_is_the_section_name_not_the_printed_label(self):
        # result.failures says "access lists" because a human reads it. restore
        # looks the section up by key and would never find the spaced spelling.
        class _BrokenAclClient(_BackupClient):
            def list_access_lists(self):
                raise requests.HTTPError("500 Server Error")

        _BrokenAclClient().full_backup(str(self.workdir), include_keys=False)

        written = json.loads((self.workdir / "full_config_latest.json").read_text())
        self.assertEqual(list(written["incomplete_sections"]), ["access_lists"])

    def test_a_clean_backup_grows_no_incomplete_sections_key(self):
        # Every reader would otherwise have to know to ignore an empty one.
        _BackupClient().full_backup(str(self.workdir), include_keys=False)

        written = json.loads((self.workdir / "full_config_latest.json").read_text())
        self.assertNotIn("incomplete_sections", written)

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


class _BulkDomainClient(_StubClient):
    """Enough of a client to run a bulk domain command end to end.

    get_host answers out of the same inventory list_hosts served, so
    host_changed_since sees nothing moving underneath the confirmation prompt
    and the tests below are about the command rather than about that guard.
    """

    def __init__(self, hosts):
        self.hosts = hosts
        self.calls = []

    def list_hosts(self):
        return self.hosts

    def get_host(self, host_id):
        for host in self.hosts:
            if host.get("id") == host_id:
                return host
        raise AssertionError(f"the test asked for an absent host: {host_id}")

    def update_host(self, host_id, updates):
        self.calls.append((host_id, updates))
        return {"id": host_id, **updates}

    @property
    def written_domains(self):
        return [updates.get("domain_names") for _, updates in self.calls]


class _UpdateRecordingClient(_StubClient):
    """Records every update_host call and fails for chosen host IDs.

    The call is recorded before the failure is raised, so a test can tell "the
    write was attempted and NPM rejected it" from "the host was skipped".
    """

    def __init__(self, fail_ids=(), error=None, changed_ids=()):
        self.calls = []
        self.reads = []
        self._fail_ids = set(fail_ids)
        # Hosts whose re-read comes back carrying a domain nobody planned for,
        # standing in for an edit made in the NPM UI while the confirmation
        # prompt was on screen.
        self._changed_ids = set(changed_ids)
        self._error = error or requests.HTTPError(
            "400 Client Error",
            response=_FakeResponse(400, json_body={"error": {"message": "Domain already in use"}}),
        )

    def get_host(self, host_id):
        # apply_domain_changes re-reads every host through this before writing
        # it. Unchanged by default, so the tests about the write loop itself
        # are unaffected by the guard sitting in front of it.
        self.reads.append(host_id)
        domains = ["app.example.com"]
        if host_id in self._changed_ids:
            domains = domains + ["added-during-prompt.example.com"]
        return {"id": host_id, "domain_names": domains}

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

    def test_ids_matching_nothing_warn_then_refuse(self):
        # Was "return empty and warn". Returning empty made a --ids list of
        # hosts that have all been deleted exit 0 through the caller's "No
        # hosts selected" branch, which in a cron log is indistinguishable
        # from a batch that ran clean. The per-ID warning is still printed —
        # it names *which* IDs — and the refusal follows it.
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._select(ids="98,99")

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("No such host(s)")
        self.assertPrinted("matched no hosts")

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

    def test_pattern_matching_nothing_refuses_rather_than_selecting_nothing(self):
        # The other half of the fall-through guard still holds — no match means
        # no hosts, emphatically not all hosts — but "no hosts" is now a
        # non-zero exit rather than a quiet empty list, since a misspelt base
        # domain in a scheduled --pattern otherwise reports success.
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._select(pattern="*.example.net")

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("--pattern")
        self.assertPrinted("matched no hosts")

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
        # all_hosts[-1] and rewrite the last host in the list. Dropping both
        # leaves nothing selected, which is now the refusal rather than an
        # empty list — the operator answered the prompt and got nothing, and
        # should be told so rather than shown a clean exit.
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._interactive("0,-1")

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("matched no hosts")

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
# select_hosts: a selector that matched nothing
# =============================================================================

class TestSelectorMatchedNothing(_ConsoleTestCase):
    """A selector was given and found no hosts. That is a failure.

    Every caller treats an empty return as a quiet no-op and exits 0, so three
    separate mistakes used to read as a clean run in a cron log: `--ids ,`
    (a shell join over an empty array), `--ids 99` for a host somebody deleted
    last week, and a `--pattern` with a typo in the base domain. Only `--ids ""`
    was caught, and then only by accident — it is falsy, so it fell through to
    the "no selector at all" refusal.
    """

    def _select(self, ids=None, pattern=None, interactive=False, hosts=None):
        return npm_api.select_hosts(_HostsClient(hosts), ids, pattern, interactive)

    def _expect_refusal(self, **kwargs):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._select(**kwargs)
        self.assertEqual(caught.exception.exit_code, 1)
        return caught.exception

    # --- the three spellings of "found nothing" ----------------------------

    def test_ids_naming_only_absent_hosts_is_an_error(self):
        self._expect_refusal(ids="98,99")
        self.assertPrinted("matched no hosts")

    def test_a_pattern_matching_nothing_is_an_error(self):
        self._expect_refusal(pattern="*.example.net")
        self.assertPrinted("matched no hosts")

    def test_an_interactive_answer_selecting_nothing_is_an_error(self):
        with mock.patch.object(npm_api.typer, "prompt", return_value="0"):
            with self.assertRaises(npm_api.typer.Exit) as caught:
                npm_api.select_hosts(_HostsClient(), None, None, True)

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("matched no hosts")

    def test_an_ids_list_that_parses_to_nothing_is_an_error(self):
        # The case that started this: `--ids ","` is `--ids ""` with one more
        # character, and the two must not disagree about whether the run
        # succeeded. Both now exit 1 — by different routes, since "" is falsy
        # and reaches the no-selector refusal instead.
        for given in (",", ",,", " , ", " "):
            with self.subTest(given=given):
                self._expect_refusal(ids=given)

    # --- kept distinct from "no selector at all" ---------------------------

    def test_no_selector_at_all_keeps_its_own_message(self):
        # Two different faults with two different fixes: this one means the
        # operator has to add an option, the other means the option they added
        # is wrong. Confusing them would send them looking in the wrong place.
        with self.assertRaises(npm_api.typer.Exit):
            self._select()

        self.assertPrinted("Please specify --ids, --pattern, or --interactive")
        self.assertNotPrinted("matched no hosts")

    def test_an_empty_selection_does_not_offer_the_list_of_options(self):
        with self.assertRaises(npm_api.typer.Exit):
            self._select(pattern="*.example.net")

        self.assertNotPrinted("Please specify")

    # --- what it tells the operator ----------------------------------------

    def test_the_message_names_the_selector_that_came_up_empty(self):
        with self.assertRaises(npm_api.typer.Exit):
            self._select(pattern="*.example.net")

        self.assertPrinted("--pattern")
        self.assertPrinted("*.example.net")

    def test_the_ids_message_names_ids_not_pattern(self):
        with self.assertRaises(npm_api.typer.Exit):
            self._select(ids="99")

        self.assertPrinted("--ids")
        self.assertNotPrinted("--pattern")

    def test_the_unknown_id_warning_still_names_which_ids(self):
        # The refusal says the selector matched nothing; the warning above it
        # says which IDs were missing. Losing the second to gain the first
        # would be a downgrade.
        with self.assertRaises(npm_api.typer.Exit):
            self._select(ids="98,99")

        self.assertPrinted("No such host(s): 98, 99")

    def test_a_pattern_that_is_rich_markup_does_not_crash_the_refusal(self):
        # --pattern is echoed back verbatim, and "[/]" is both a plausible
        # fnmatch typo and a Rich closing tag with nothing to close, which
        # raises MarkupError out of the error path itself.
        for pattern in ("[/]", "[red]", "[a", "]"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    self._select(pattern=pattern)
                self.assertEqual(caught.exception.exit_code, 1)

    # --- what is still not an error ----------------------------------------

    def test_an_estate_with_no_hosts_at_all_is_still_not_an_error(self):
        # Not the operator getting a selector wrong, and there is nothing here
        # for a bulk command to damage either way.
        self.assertEqual(self._select(ids="12", hosts=[]), [])
        self.assertPrinted("No proxy hosts found")

    def test_a_partial_match_is_not_an_error(self):
        # One of the two IDs exists, so the run has something to do. The other
        # is still reported.
        self.assertEqual([h["id"] for h in self._select(ids="12,99")], [12])
        self.assertPrinted("No such host(s): 99")

    def test_interactive_all_is_never_empty(self):
        with mock.patch.object(npm_api.typer, "prompt", return_value="all"):
            selected = npm_api.select_hosts(_HostsClient(), None, None, True)
        self.assertEqual(len(selected), 3)


class TestMalformedPatterns(_ConsoleTestCase):
    """--pattern is handed to fnmatch, whose brackets compile to a regex.

    An unbalanced bracket is a plausible typo — a half-finished character
    class, or a shell that ate a quote — and fnmatch.translate builds a real
    regular expression out of whatever it is given. The property worth pinning
    is that such a pattern comes out as the ordinary "matched nothing"
    refusal, and never as an re.error escaping a command that has not written
    anything yet.
    """

    MALFORMED = ("[a", "[!", "a]b", "[", "]", "?", "[]", "*[a-")

    def test_a_malformed_glob_is_refused_rather_than_raised(self):
        # The exit code is pinned to 1 rather than merely asserting that
        # something was raised: an fnmatch traceback is also "something
        # raised", and the whole point here is telling those two apart.
        for pattern in self.MALFORMED:
            with self.subTest(pattern=pattern):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    npm_api.select_hosts(_HostsClient(), None, pattern, False)
                self.assertEqual(caught.exception.exit_code, 1)

    def test_the_refusal_is_the_ordinary_matched_nothing_one(self):
        # Not a special case for bad syntax. A malformed glob matches no name,
        # which is a selector that found nothing like any other.
        with self.assertRaises(npm_api.typer.Exit):
            npm_api.select_hosts(_HostsClient(), None, "[a", False)

        self.assertPrinted("matched no hosts")

    def test_a_valid_glob_matching_everything_is_not_refused(self):
        # "**" is well-formed and matches every name. It has to come through
        # the guard above untouched, or the refusal is catching valid patterns
        # along with the broken ones.
        selected = npm_api.select_hosts(_HostsClient(), None, "**", False)

        self.assertEqual([h["id"] for h in selected], [12, 13, 14])


class TestEmptySelectionReachesEveryBulkCommand(_ConsoleTestCase):
    """The refusal lives in select_hosts, so every caller inherits it.

    Asserted per command rather than trusted, because each one wraps the call
    in its own `if not hosts: return` and any of them could have grown a way
    to swallow the exit.
    """

    def _expect_exit_1(self, call):
        with mock.patch.object(npm_api, "get_client", lambda: _HostsClient()):
            with self.assertRaises(npm_api.typer.Exit) as caught:
                call()
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("matched no hosts")

    def test_bulk_add_domain_exits_1(self):
        self._expect_exit_1(lambda: npm_api.host_bulk_add_domain(
            new_domain="example.net", host_ids="99", pattern=None,
            preview=False, yes=True, interactive=False))

    def test_bulk_remove_domain_exits_1(self):
        self._expect_exit_1(lambda: npm_api.host_bulk_remove_domain(
            domain_pattern="example.com", host_ids="99", pattern=None,
            preview=False, yes=True, interactive=False))

    def test_bulk_replace_domain_exits_1(self):
        self._expect_exit_1(lambda: npm_api.host_bulk_replace_domain(
            old_domain="example.com", new_domain="example.net", host_ids="99",
            pattern=None, preview=False, yes=True, interactive=False))

    def test_bulk_update_exits_1(self):
        self._expect_exit_1(lambda: npm_api.host_bulk_update(
            field="forward_host", value="10.0.0.9", host_ids="99", pattern=None,
            preview=False, yes=True, interactive=False))

    def test_split_exits_1(self):
        self._expect_exit_1(lambda: npm_api.host_split(
            match="*.internal.lan", cert="none", host_ids="99", pattern=None,
            preview=False, yes=True, interactive=False))

    def test_no_bulk_command_writes_anything_first(self):
        # The selector is resolved at the top of each command, so an empty
        # selection cannot reach a write. _HostsClient raises on any method
        # other than list_hosts.
        for label, call in (
            ("bulk-add-domain", lambda: npm_api.host_bulk_add_domain(
                new_domain="example.net", host_ids="99", pattern=None,
                preview=True, yes=True, interactive=False)),
            ("bulk-update", lambda: npm_api.host_bulk_update(
                field="forward_host", value="10.0.0.9", host_ids="99",
                pattern=None, preview=True, yes=True, interactive=False)),
        ):
            with self.subTest(command=label):
                self._expect_exit_1(call)


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
# host_changed_since
# =============================================================================

class _RereadClient(_StubClient):
    """Serves whatever the test says the host looks like now.

    A callable rather than a dict so a test can make the second read differ
    from the first, and so "the host is gone" is expressed the way NPM
    expresses it — a 404 out of get_host, not a None.
    """

    def __init__(self, current=None, error=None):
        self.current = current
        self.error = error
        self.reads = []

    def get_host(self, host_id):
        self.reads.append(host_id)
        if self.error is not None:
            raise self.error
        return self.current


class TestHostChangedSince(_ConsoleTestCase):
    """The re-read that stands between a stale plan and a destructive write.

    Every bulk command reads its hosts, previews, waits for a human, then
    writes what it worked out before the wait. This helper is the only thing
    that notices the world moved during that wait.
    """

    def _host(self, domains, **overrides):
        return dict({"id": 12, "domain_names": list(domains),
                     "modified_on": "2026-08-30T10:00:00.000Z"}, **overrides)

    def test_an_unchanged_host_reports_no_change(self):
        before = self._host(["app.example.com"])
        client = _RereadClient(self._host(["app.example.com"]))

        self.assertIsNone(npm_api.host_changed_since(client, before))

    def test_the_host_is_re_read_through_the_client(self):
        # The whole point: the caller's dict cannot be trusted to have moved,
        # because the real get_host parses fresh JSON on every call and hands
        # back an object nobody else holds a reference to.
        client = _RereadClient(self._host(["app.example.com"]))

        npm_api.host_changed_since(client, self._host(["app.example.com"]))

        self.assertEqual(client.reads, [12])

    def test_an_added_domain_is_reported(self):
        before = self._host(["app.example.com"])
        client = _RereadClient(self._host(["app.example.com", "new.example.com"]))

        changed = npm_api.host_changed_since(client, before)

        self.assertIsNotNone(changed)
        self.assertIn("new.example.com", changed)

    def test_a_removed_domain_is_reported(self):
        before = self._host(["app.example.com", "api.example.com"])
        client = _RereadClient(self._host(["app.example.com"]))

        changed = npm_api.host_changed_since(client, before)

        self.assertIsNotNone(changed)
        self.assertIn("api.example.com", changed)

    def test_a_reordered_domain_list_is_not_a_change(self):
        # NPM returns the list in whatever order it stored it, and a reorder
        # changes nothing about what the host serves. Refusing a write over one
        # would make the guard fire on runs where nothing happened.
        before = self._host(["app.example.com", "api.example.com"])
        client = _RereadClient(self._host(["api.example.com", "app.example.com"]))

        self.assertIsNone(npm_api.host_changed_since(client, before))

    def test_a_changed_modified_on_is_a_change_even_with_the_same_domains(self):
        # The domains are the field these commands overwrite, but they are not
        # the only field an operator can have touched between the preview and
        # the write.
        before = self._host(["app.example.com"])
        client = _RereadClient(
            self._host(["app.example.com"], modified_on="2026-08-30T10:04:00.000Z"))

        changed = npm_api.host_changed_since(client, before)

        self.assertIsNotNone(changed)
        self.assertIn("2026-08-30T10:04:00.000Z", changed)

    def test_a_changed_domain_list_is_caught_within_one_second(self):
        # Both fields are compared precisely because either alone is blind.
        # NPM's modified_on has second resolution, so two edits inside one
        # second carry the same timestamp; domain_names is what catches them.
        before = self._host(["app.example.com"])
        client = _RereadClient(self._host(["evil.example.com"]))

        self.assertIsNotNone(npm_api.host_changed_since(client, before))

    def test_a_host_that_no_longer_exists_is_a_change(self):
        client = _RereadClient(
            error=requests.HTTPError("404 Not Found", response=_FakeResponse(404)))

        changed = npm_api.host_changed_since(client, self._host(["app.example.com"]))

        self.assertIsNotNone(changed)
        self.assertIn("no longer exists", changed)

    def test_a_null_domain_list_on_either_side_does_not_raise(self):
        # NPM sends domain_names as an explicit null on some records, and a
        # get() default only applies when the key is absent.
        for before_domains, after_domains in ((None, ["a.example.com"]),
                                              (["a.example.com"], None),
                                              (None, None)):
            with self.subTest(before=before_domains, after=after_domains):
                client = _RereadClient(self._host([], domain_names=after_domains))
                result = npm_api.host_changed_since(
                    client, self._host([], domain_names=before_domains))
                if before_domains == after_domains:
                    self.assertIsNone(result)
                else:
                    self.assertIsNotNone(result)

    def test_exactly_one_read_is_made_per_call(self):
        # One re-read per host is the agreed cost. A guard that listed the whole
        # estate would turn a 57-host run into 57 full inventory fetches.
        client = _RereadClient(self._host(["app.example.com"]))

        npm_api.host_changed_since(client, self._host(["app.example.com"]))

        self.assertEqual(len(client.reads), 1)


# =============================================================================
# apply_domain_changes
# =============================================================================

def _change(host_id, resulting, current=None, new=None):
    # "host" is the copy of the host taken before the confirmation prompt, which
    # apply_domain_changes hands to host_changed_since to compare a fresh read
    # against. It carries the domain list _UpdateRecordingClient.get_host
    # returns, so an unmodified host reads as unmodified.
    return {
        "host_id": host_id,
        "host": {"id": host_id, "domain_names": ["app.example.com"]},
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

    # --- the host moved while the confirmation prompt was up ----------------

    def test_a_host_that_changed_since_the_preview_is_not_written(self):
        # resulting_domains was worked out before the prompt and is written as
        # a full replacement, so writing it over a host that has since gained a
        # domain would erase that domain with no error and no log line naming
        # it. Skipped instead.
        client = _UpdateRecordingClient(changed_ids=[13])

        with self.assertRaises(npm_api.typer.Exit):
            npm_api.apply_domain_changes(
                client, [_change(12, ["a.example.com"]), _change(13, ["b.example.com"])],
                self._describe)

        self.assertEqual(client.written_ids, [12],
                         msg="host 13 changed under the plan and must not be written")

    def test_a_changed_host_is_named_along_with_what_changed(self):
        client = _UpdateRecordingClient(changed_ids=[13])

        with self.assertRaises(npm_api.typer.Exit):
            npm_api.apply_domain_changes(
                client, [_change(13, ["b.example.com"])], self._describe)

        self.assertPrinted("Host 13")
        self.assertPrinted("added-during-prompt.example.com")

    def test_a_changed_host_counts_as_a_failure_not_a_skip(self):
        # It exits non-zero through the existing summary machinery: a run that
        # silently declined to do part of what was asked must not look clean to
        # the script that invoked it.
        client = _UpdateRecordingClient(changed_ids=[13])

        with self.assertRaises(npm_api.typer.Exit) as caught:
            npm_api.apply_domain_changes(
                client, [_change(12, ["a.example.com"]), _change(13, ["b.example.com"])],
                self._describe)

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")

    def test_one_changed_host_does_not_abandon_the_rest(self):
        client = _UpdateRecordingClient(changed_ids=[13])

        with self.assertRaises(npm_api.typer.Exit):
            npm_api.apply_domain_changes(
                client,
                [_change(12, ["a.example.com"]), _change(13, ["b.example.com"]),
                 _change(14, ["c.example.com"])],
                self._describe)

        # 14 comes after the skip, so its presence is the real assertion.
        self.assertEqual(client.written_ids, [12, 14])

    def test_describe_is_not_called_for_a_changed_host(self):
        client = _UpdateRecordingClient(changed_ids=[13])
        described = []

        with self.assertRaises(npm_api.typer.Exit):
            npm_api.apply_domain_changes(
                client, [_change(12, ["a.example.com"]), _change(13, ["b.example.com"])],
                lambda change: described.append(change["host_id"]) or "ok")

        self.assertEqual(described, [12])

    def test_one_extra_read_per_host_and_no_more(self):
        # The agreed cost of the guard. A 57-host run makes 57 extra GETs, not
        # a re-listing of the estate per host.
        client = _UpdateRecordingClient()

        npm_api.apply_domain_changes(
            client,
            [_change(12, ["a.example.com"]), _change(13, ["b.example.com"]),
             _change(14, ["c.example.com"])],
            self._describe)

        self.assertEqual(client.reads, [12, 13, 14])


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
    """The one Config attribute write_state_snapshot reads.

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
                       [{"path": "/admin", "forward_host": "192.0.2.10"}]))
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
# write_state_snapshot
# =============================================================================

class TestWriteStateSnapshot(_WorkdirTestCase):
    """The floor under merge and restore.

    Both delete objects NPM has no undo for. Their in-process rollbacks can
    only recreate under a new ID, and cannot run at all if the process is
    killed partway through, so this file is the only thing guaranteeing the
    original configuration still exists somewhere.
    """

    def _write(self, target, sources, directory=None):
        config = _MergeConfig(directory or self.workdir)
        return npm_api.write_state_snapshot(config, "pre_merge_12",
                                            {"target": target, "sources": sources})

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
        assert_owner_only(self, path)

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

    def test_no_matching_hosts_writes_nothing_and_exits_1(self):
        # The pair to test_only_the_target_matching_leaves_nothing_to_do above,
        # which must stay a clean exit: there the pattern *did* match, and
        # merge dropped the target from its own source list. Here it matched
        # nothing at all, which is the operator's selector being wrong.
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        exit_exc = self._merge_expecting_exit(client, pattern="nothing.example.net")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("matched no hosts")

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
                             ("forward_host", "192.0.2.10"),
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

        with mock.patch.object(npm_api, "write_state_snapshot",
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
        assert_owner_only(self, written[0])
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


# =============================================================================
# host merge: the world moving while the confirmation prompt is up
# =============================================================================

class TestHostMergeConcurrentChange(_MergeCommandTestCase):
    """Merge reads its hosts, previews, waits for a human, then writes.

    The read and the write are separated by however long the operator takes to
    answer, which is the one interval in the program measured in minutes rather
    than milliseconds. The merged domain list is built from the target read
    before that wait and written back as a full replacement, so a domain added
    in the NPM UI meanwhile would be silently overwritten.

    The target is checked once, after the confirmation and before the loop, and
    a change there fails the whole merge rather than one source of it: every
    source's domains land on that one host, so the list to be written is stale
    in its entirety. The operator re-runs, sees a preview reflecting the change,
    and approves what will actually happen.
    """

    def _stale_target(self, client):
        """Replace host 12 with a copy carrying an extra domain.

        REPLACES the dict in the stub's host list rather than mutating it. The
        real get_host parses fresh JSON on every call, so a caller holding an
        earlier result holds an independent copy; mutating the shared dict in
        place would hand host_merge the new value through a reference the real
        client never gives it, and the test would pass without the code being
        right.
        """
        def add_a_domain_while_the_prompt_is_up(*args, **kwargs):
            client.hosts[0] = _merge_host(
                12, ["app.example.com", "added-during-prompt.example.com"])
        return add_a_domain_while_the_prompt_is_up

    def _merge_with_a_changing_target(self, client, **overrides):
        with mock.patch.object(npm_api, "confirm_bulk",
                               self._stale_target(client)):
            with self.assertRaises(npm_api.typer.Exit) as caught:
                self._merge(client, **overrides)
        return caught.exception

    def _target_and_source(self):
        return (_merge_host(12, ["app.example.com"]),
                [_merge_host(13, ["old.example.com"])])

    def test_a_target_changed_during_the_prompt_aborts_the_merge(self):
        target, sources = self._target_and_source()
        client = self._client(target, sources)

        exit_exc = self._merge_with_a_changing_target(client, host_ids="13")

        self.assertEqual(exit_exc.exit_code, 1)

    def test_nothing_is_written_when_the_target_changed(self):
        # The assertion that matters: no delete, no extend, no snapshot. Merge
        # is the only command here that deletes a host the user did not name for
        # deletion, so a stale run has to stop before the first destructive
        # write, not partway through.
        target, sources = self._target_and_source()
        client = self._client(target, sources)

        self._merge_with_a_changing_target(client, host_ids="13")

        self.assertEqual(client.calls, [])
        self.assertEqual(client.deleted_ids, [])
        self.assertEqual(client.updates, [])

    def test_the_abort_happens_before_the_snapshot_is_written(self):
        # Nothing was touched, so there is nothing to recover, and a snapshot
        # file implying otherwise is worse than none.
        target, sources = self._target_and_source()
        client = self._client(target, sources)

        self._merge_with_a_changing_target(client, host_ids="13")

        self.assertEqual(list(self.workdir.glob("pre_merge_12_*.json")), [])

    def test_the_message_names_the_host_and_what_changed(self):
        target, sources = self._target_and_source()
        client = self._client(target, sources)

        self._merge_with_a_changing_target(client, host_ids="13")

        self.assertPrinted("Host 12 changed while the confirmation prompt was up")
        self.assertPrinted("added-during-prompt.example.com")
        self.assertPrinted("Refusing to merge")

    def test_an_unchanged_target_merges_as_before(self):
        # The guard has to be invisible on the ordinary path, which is every
        # run but the rare one this class is about.
        target, sources = self._target_and_source()
        client = self._client(target, sources)

        self._merge(client, host_ids="13")

        self.assertEqual(client.kinds, ["delete", "update"])
        self.assertPrinted("Host 12 now serves: app.example.com, old.example.com")

    def test_the_target_is_checked_once_rather_than_once_per_source(self):
        # Merge writes to the target on every iteration, so its modified_on
        # legitimately moves as a result of our own writes. Checking inside the
        # loop would make the second source fail on the first source's success.
        target = _merge_host(12, ["app.example.com"])
        sources = [_merge_host(13, ["a.example.com"]),
                   _merge_host(14, ["b.example.com"]),
                   _merge_host(15, ["c.example.com"])]
        client = self._client(target, sources)

        self._merge(client, host_ids="13,14,15")

        self.assertPrinted("Successful: 3")
        self.assertNotPrinted("Failed:")

    def test_a_source_deleted_from_under_the_run_is_skipped_not_deleted(self):
        # A source is deleted outright, so a domain added to it while the prompt
        # was up would go with it — and it is not in the merged list either,
        # since that was built before the change.
        target = _merge_host(12, ["app.example.com"])
        sources = [_merge_host(13, ["a.example.com"]),
                   _merge_host(14, ["b.example.com"])]
        client = self._client(target, sources)

        def change_source_14(*args, **kwargs):
            client.hosts[2] = _merge_host(14, ["b.example.com", "late.example.com"])

        with mock.patch.object(npm_api, "confirm_bulk", change_source_14):
            with self.assertRaises(npm_api.typer.Exit) as caught:
                self._merge(client, host_ids="13,14")

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertEqual(client.deleted_ids, [13],
                         msg="host 14 changed under the plan and must not be deleted")
        self.assertPrinted("Host 14")
        self.assertPrinted("late.example.com")
        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")

    def test_a_source_that_vanished_is_reported_as_gone(self):
        target = _merge_host(12, ["app.example.com"])
        sources = [_merge_host(13, ["a.example.com"])]
        client = self._client(target, sources)

        def delete_source_13(*args, **kwargs):
            client.hosts = [h for h in client.hosts if h.get("id") != 13]

        with mock.patch.object(npm_api, "confirm_bulk", delete_source_13):
            with self.assertRaises(npm_api.typer.Exit):
                self._merge(client, host_ids="13")

        self.assertEqual(client.deleted_ids, [])
        self.assertPrinted("no longer exists")


# =============================================================================
# restore: shared doubles
# =============================================================================

def _backup_cert(cert_id, domains, nice_name=""):
    """A certificate as a backup carries it: metadata only, no key material."""
    return {"id": cert_id, "domain_names": list(domains), "nice_name": nice_name}


def _backup_acl(acl_id, name, **overrides):
    """An access list as full_backup writes it, items and clients expanded.

    Both nested rows carry their own `id` and an `access_list_id` pointing at
    the instance the backup came from, which is the whole reason
    restore_acl_payload rebuilds them field by field rather than echoing them.
    """
    acl = {
        "id": acl_id,
        "name": name,
        "satisfy_any": False,
        "pass_auth": False,
        "items": [{"id": 90, "access_list_id": acl_id,
                   "username": "ops", "password": "s3cret"}],
        "clients": [{"id": 91, "access_list_id": acl_id,
                     "address": "10.0.0.0/8", "directive": "allow"}],
    }
    acl.update(overrides)
    return acl


def _backup_setting(setting_id, value="congratulations"):
    return {"id": setting_id, "name": setting_id, "value": value, "meta": {}}


class _RestoreCommandClient(_StubClient):
    """A whole NPM instance, with every write recorded on one ordered list.

    Restore's correctness is almost entirely an ordering: the pre-restore
    snapshot before the first delete, hosts before access lists on the way out
    (NPM will not drop an access list a host still references), access lists
    before hosts on the way back in (a host carries access_list_id, so the new
    IDs have to exist first). Separate per-method logs would lose exactly that.

    The operations restore must never perform are recorded too rather than left
    unstubbed. An unstubbed method would fail loudly, which is the right
    default, but "no user was created" is a property worth asserting directly
    instead of inferring from the absence of a crash.
    """

    def __init__(self, backup_dir, *, hosts=(), acls=(), certs=(), settings=(),
                 delete_host_error_ids=(), delete_host_refuse_ids=(),
                 delete_acl_error_ids=(), acl_create_failures=(),
                 host_create_failures=(), setting_failures=(), error=None):
        self.config = _MergeConfig(backup_dir)
        self._hosts = list(hosts)
        self._acls = list(acls)
        self._certs = list(certs)
        self._settings = list(settings)
        self.calls = []
        self._delete_host_error_ids = set(delete_host_error_ids)
        self._delete_host_refuse_ids = set(delete_host_refuse_ids)
        self._delete_acl_error_ids = set(delete_acl_error_ids)
        self._acl_create_failures = set(acl_create_failures)
        self._host_create_failures = set(host_create_failures)
        self._setting_failures = set(setting_failures)
        self._error = error or _npm_http_error()
        self._next_acl_id = 500
        self._next_host_id = 900

    # --- reads -------------------------------------------------------------

    def list_hosts(self):
        return self._hosts

    def list_access_lists(self):
        return self._acls

    def list_certificates(self):
        return self._certs

    def list_settings(self):
        return self._settings

    # --- writes ------------------------------------------------------------

    def delete_host(self, host_id):
        self.calls.append(("delete_host", host_id))
        if host_id in self._delete_host_error_ids:
            raise self._error
        return host_id not in self._delete_host_refuse_ids

    def delete_access_list(self, list_id):
        self.calls.append(("delete_acl", list_id))
        if list_id in self._delete_acl_error_ids:
            raise self._error
        return True

    def create_access_list(self, name, satisfy_any=False, pass_auth=False,
                           items=None, clients=None):
        kwargs = {"name": name, "satisfy_any": satisfy_any, "pass_auth": pass_auth,
                  "items": items, "clients": clients}
        self.calls.append(("create_acl", name, kwargs))
        if name in self._acl_create_failures:
            raise self._error
        self._next_acl_id += 1
        return {"id": self._next_acl_id, "name": name}

    def create_host_from(self, source, overrides):
        self.calls.append(("create_host", source.get("id"), overrides))
        if source.get("id") in self._host_create_failures:
            raise self._error
        self._next_host_id += 1
        return dict(source, id=self._next_host_id, **overrides)

    def update_setting(self, setting_id, payload):
        self.calls.append(("setting", setting_id, payload))
        if setting_id in self._setting_failures:
            raise self._error
        return {"id": setting_id, **payload}

    # --- operations restore must never perform ------------------------------

    def create_user(self, username, email, password):
        self.calls.append(("create_user", username))
        return {"id": 1}

    def delete_user(self, user_id):
        self.calls.append(("delete_user", user_id))
        return True

    def generate_certificate(self, *args, **kwargs):
        self.calls.append(("create_cert", args))
        return {"id": 1}

    def delete_certificate(self, cert_id):
        self.calls.append(("delete_cert", cert_id))
        return True

    def download_certificate(self, cert_id, output_dir, cert_name):
        self.calls.append(("download_cert", cert_id))
        return []

    # --- views the assertions read ------------------------------------------

    FORBIDDEN = ("create_user", "delete_user", "create_cert", "delete_cert",
                 "download_cert")

    @property
    def kinds(self):
        return [call[0] for call in self.calls]

    @property
    def forbidden_calls(self):
        return [call for call in self.calls if call[0] in self.FORBIDDEN]

    @property
    def deleted_host_ids(self):
        return [c[1] for c in self.calls if c[0] == "delete_host"]

    @property
    def deleted_acl_ids(self):
        return [c[1] for c in self.calls if c[0] == "delete_acl"]

    @property
    def created_acls(self):
        return [c[2] for c in self.calls if c[0] == "create_acl"]

    @property
    def created_hosts(self):
        return [(c[1], c[2]) for c in self.calls if c[0] == "create_host"]

    @property
    def written_settings(self):
        return [(c[1], c[2]) for c in self.calls if c[0] == "setting"]


# =============================================================================
# load_backup
# =============================================================================

class TestLoadBackup(_WorkdirTestCase):
    """Turning whatever the user typed into a backup object, or into a sentence.

    Every failure here has to arrive as NPMError: main() prints that on one
    line and exits, while a JSONDecodeError or a KeyError reaches the terminal
    as a traceback and reads like a bug in the tool rather than a truncated
    file or a mistyped path.
    """

    _BACKUP = {"proxy_hosts": [{"id": 1, "domain_names": ["app.example.com"]}],
               "access_lists": [], "certificates": [], "settings": [], "users": []}

    def _write(self, name, payload=None, directory=None):
        directory = directory or self.workdir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps(self._BACKUP if payload is None else payload))
        return path

    def test_a_file_path_is_read_directly(self):
        path = self._write("full_config_2026_08_24__11_20_02.json")

        loaded = npm_api.load_backup(str(path))

        self.assertEqual(loaded["path"], path)
        self.assertEqual(loaded["data"], self._BACKUP)

    def test_a_directory_follows_its_latest_link(self):
        self._write("full_config_2026_01_01__00_00_00.json", {"proxy_hosts": []})
        newest = self._write("full_config_2026_08_24__11_20_02.json")
        (self.workdir / "full_config_latest.json").symlink_to(newest.name)

        loaded = npm_api.load_backup(str(self.workdir))

        self.assertEqual(loaded["data"], self._BACKUP)

    def test_a_dangling_latest_link_falls_back_to_the_newest_real_backup(self):
        # Regression. The fallback was written and commented, but never fired:
        # full_config_latest.json matches full_config_*.json, and Path.glob
        # lists dangling symlinks because it does not stat them. Sorting is
        # lexicographic and every real backup is full_config_<digit>..., so
        # 'l' > '2' put the broken link last every time and candidates[-1]
        # picked it. `restore <dir>` then failed with "No such backup",
        # naming a link the user can see sitting in a directory full of
        # perfectly good backups.
        #
        # Not hypothetical: full_backup carries its own comment about a link
        # left pointing at a pruned backup, so the codebase already expects
        # this state to occur.
        self._write("full_config_2026_01_01__00_00_00.json", {"proxy_hosts": []})
        newest = self._write("full_config_2026_08_24__11_20_02.json")
        (self.workdir / "full_config_latest.json").symlink_to("full_config_gone.json")

        loaded = npm_api.load_backup(str(self.workdir))

        self.assertEqual(loaded["path"], newest)
        self.assertEqual(loaded["data"], self._BACKUP)

    def test_a_dangling_latest_link_alone_reports_the_directory(self):
        # Nothing to fall back to. The message has to name the directory that
        # was searched, not the broken link: "No such backup: .../latest.json"
        # points at a file the user can see and tells them nothing.
        (self.workdir / "full_config_latest.json").symlink_to("full_config_gone.json")

        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.load_backup(str(self.workdir))

        self.assertIn(str(self.workdir), str(caught.exception))
        self.assertNotIn("full_config_latest.json", str(caught.exception))

    def test_the_latest_link_is_resolved_to_the_file_it_points_at(self):
        # The path is printed, recorded in the pre-restore snapshot and is what
        # the user quotes afterwards, so reporting the link would name a file
        # that means something different a backup later.
        newest = self._write("full_config_2026_08_24__11_20_02.json")
        (self.workdir / "full_config_latest.json").symlink_to(newest.name)

        loaded = npm_api.load_backup(str(self.workdir))

        self.assertEqual(loaded["path"], newest)
        self.assertFalse(loaded["path"].is_symlink())

    def test_a_directory_with_no_latest_link_takes_the_newest_by_name(self):
        # full_backup stamps files YYYY_MM_DD__HH_MM_SS, which sorts
        # lexicographically in chronological order, so the name is a usable
        # stand-in for the mtime and survives a copy that did not preserve it.
        self._write("full_config_2026_01_01__00_00_00.json", {"proxy_hosts": []})
        newest = self._write("full_config_2026_08_24__11_20_02.json")

        self.assertEqual(npm_api.load_backup(str(self.workdir))["path"], newest)

    def test_a_directory_holding_no_backup_names_the_directory(self):
        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.load_backup(str(self.workdir))

        self.assertIn(str(self.workdir), str(caught.exception))
        self.assertIn("full_config", str(caught.exception))

    def test_a_directory_of_unrelated_files_is_still_no_backup(self):
        (self.workdir / "notes.txt").write_text("hello")
        (self.workdir / "hosts_2026_08_24.json").write_text("{}")

        with self.assertRaises(npm_api.NPMError):
            npm_api.load_backup(str(self.workdir))

    def test_a_nonexistent_path_names_the_path(self):
        missing = self.workdir / "nowhere" / "full_config_2026.json"

        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.load_backup(str(missing))

        self.assertIn(str(missing), str(caught.exception))

    def test_invalid_json_names_the_file_rather_than_raising_jsondecodeerror(self):
        path = self.workdir / "full_config_2026_08_24__11_20_02.json"
        # A backup truncated by a full disk or an interrupted copy is the
        # realistic way to get here.
        path.write_text('{"proxy_hosts": [{"id": 1}')

        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.load_backup(str(path))

        self.assertIn(str(path), str(caught.exception))
        self.assertIn("not valid JSON", str(caught.exception))

    def test_an_empty_file_is_not_valid_json(self):
        path = self.workdir / "full_config_2026_08_24__11_20_02.json"
        path.write_text("")

        with self.assertRaises(npm_api.NPMError):
            npm_api.load_backup(str(path))

    def test_a_json_list_is_not_a_backup_object(self):
        # Well-formed JSON of the wrong shape: `data.get(...)` would raise
        # AttributeError several screens later, inside the restore loop.
        path = self._write("full_config_2026_08_24__11_20_02.json",
                           [{"id": 1}, {"id": 2}])

        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.load_backup(str(path))

        self.assertIn(str(path), str(caught.exception))
        self.assertIn("backup object", str(caught.exception))

    def test_an_object_holding_none_of_the_backup_sections_is_refused(self):
        # Pointing restore at some other JSON file is an easy mistake, and the
        # command would otherwise read it as an empty backup.
        path = self._write("full_config_2026_08_24__11_20_02.json",
                           {"nginx": "config", "version": 3})

        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.load_backup(str(path))

        self.assertIn(str(path), str(caught.exception))
        for section in npm_api.BACKUP_SECTIONS:
            self.assertIn(section, str(caught.exception))

    def test_an_empty_object_is_refused(self):
        path = self._write("full_config_2026_08_24__11_20_02.json", {})

        with self.assertRaises(npm_api.NPMError):
            npm_api.load_backup(str(path))

    def test_any_one_known_section_is_enough_to_read_it(self):
        # A backup whose other sections failed is still worth loading; restore
        # decides for itself whether what survived is anything it can use.
        for section in npm_api.BACKUP_SECTIONS:
            with self.subTest(section=section):
                path = self._write(f"full_config_{section}.json", {section: []})
                self.assertEqual(npm_api.load_backup(str(path))["data"], {section: []})


# =============================================================================
# backup_section
# =============================================================================

class TestBackupSection(unittest.TestCase):
    """Whether a backup can speak for a section at all.

    The distinction the whole restore hangs on: "there were none" is an
    instruction to delete, "we never found out" is not, and both used to read
    as the same empty list.
    """

    def test_a_populated_section_comes_back_with_no_reason(self):
        rows, reason = npm_api.backup_section(
            {"proxy_hosts": [{"id": 1}]}, "proxy_hosts")

        self.assertEqual(rows, [{"id": 1}])
        self.assertIsNone(reason)

    def test_an_explicitly_empty_section_is_honoured(self):
        # [] is a positive statement about the source instance, not an absence,
        # and restoring it over a live section is the correct thing to do.
        rows, reason = npm_api.backup_section({"proxy_hosts": []}, "proxy_hosts")

        self.assertEqual(rows, [])
        self.assertIsNone(reason)

    def test_an_absent_section_is_refused_rather_than_read_as_empty(self):
        rows, reason = npm_api.backup_section({"access_lists": []}, "proxy_hosts")

        self.assertIsNone(rows)
        self.assertIn("no such section", reason)

    def test_a_null_section_is_refused(self):
        rows, reason = npm_api.backup_section({"proxy_hosts": None}, "proxy_hosts")

        self.assertIsNone(rows)
        self.assertIn("null", reason)

    def test_a_section_of_the_wrong_type_names_the_type(self):
        for value, expected in (({"12": {}}, "dict"), ("app.lan", "str"), (3, "int")):
            with self.subTest(value=value):
                rows, reason = npm_api.backup_section(
                    {"proxy_hosts": value}, "proxy_hosts")

                self.assertIsNone(rows)
                self.assertIn(expected, reason)

    def test_a_section_recorded_as_incomplete_is_refused_even_when_present(self):
        # A partial fetch that got far enough to assign something is still a
        # section the backup cannot speak for.
        rows, reason = npm_api.backup_section(
            {"proxy_hosts": [{"id": 1}],
             "incomplete_sections": {"proxy_hosts": "500 Server Error"}},
            "proxy_hosts")

        self.assertIsNone(rows)
        self.assertIn("500 Server Error", reason)

    def test_the_recorded_reason_only_covers_its_own_section(self):
        data = {"proxy_hosts": [{"id": 1}], "access_lists": [],
                "incomplete_sections": {"settings": "403 Forbidden"}}

        self.assertIsNone(npm_api.backup_section(data, "proxy_hosts")[1])
        self.assertIsNone(npm_api.backup_section(data, "access_lists")[1])
        self.assertIsNotNone(npm_api.backup_section(data, "settings")[1])

    def test_an_incomplete_sections_key_of_the_wrong_type_is_ignored(self):
        # Only full_backup writes that key; anything else under it came from a
        # hand-edit, and reading it as a marker would refuse a good section.
        for recorded in ("proxy_hosts", ["proxy_hosts"], 7):
            with self.subTest(recorded=recorded):
                rows, reason = npm_api.backup_section(
                    {"proxy_hosts": [{"id": 1}], "incomplete_sections": recorded},
                    "proxy_hosts")

                self.assertEqual(rows, [{"id": 1}])
                self.assertIsNone(reason)


# =============================================================================
# validate_backup_rows
# =============================================================================

class TestValidateBackupRows(unittest.TestCase):
    """The gate that has to close before the first delete.

    Everything it catches used to surface as an AttributeError from the rebuild
    loop, which runs only once the target has been emptied.
    """

    def test_a_well_formed_backup_passes(self):
        npm_api.validate_backup_rows({
            "proxy_hosts": [_merge_host(1, ["app.example.com"])],
            "access_lists": [_backup_acl(4, "internal")],
            "certificates": [_backup_cert(7, ["app.example.com"])],
            "settings": [_backup_setting("default-site")],
        })

    def test_absent_and_null_sections_are_left_to_backup_section(self):
        # They are skipped rather than restored, so their shape decides
        # nothing and refusing over them would reject a usable backup.
        npm_api.validate_backup_rows({"proxy_hosts": None, "access_lists": []})

    def test_a_section_that_is_not_a_list_is_refused(self):
        for key in npm_api.VALIDATED_SECTIONS:
            with self.subTest(key=key):
                with self.assertRaises(npm_api.NPMError) as caught:
                    npm_api.validate_backup_rows({key: {"12": {}}})

                self.assertIn(key, str(caught.exception))
                self.assertIn("dict", str(caught.exception))

    def test_a_row_that_is_not_a_record_names_the_section_and_the_index(self):
        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.validate_backup_rows(
                {"proxy_hosts": [_merge_host(1, ["a.lan"]), None]})

        self.assertIn("proxy_hosts", str(caught.exception))
        self.assertIn("1", str(caught.exception))

    def test_every_section_has_its_rows_checked(self):
        for key in npm_api.VALIDATED_SECTIONS:
            with self.subTest(key=key):
                with self.assertRaises(npm_api.NPMError):
                    npm_api.validate_backup_rows({key: [None]})

    def test_an_access_list_whose_items_are_not_a_list_is_refused(self):
        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.validate_backup_rows({
                "access_lists": [_backup_acl(4, "internal",
                                             items={"ops": {"password": "s3cret"}})]})

        self.assertIn("items", str(caught.exception))

    def test_a_null_item_row_is_refused(self):
        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.validate_backup_rows(
                {"access_lists": [_backup_acl(4, "internal", items=[None])]})

        self.assertIn("items", str(caught.exception))
        self.assertIn("0", str(caught.exception))

    def test_a_null_client_row_is_refused(self):
        with self.assertRaises(npm_api.NPMError) as caught:
            npm_api.validate_backup_rows(
                {"access_lists": [_backup_acl(4, "internal", clients=[None])]})

        self.assertIn("clients", str(caught.exception))

    def test_absent_items_and_clients_are_not_invented(self):
        # restore_acl_payload reads both as empty, which is a real access list
        # NPM will accept — an older backup that omitted them is still usable.
        npm_api.validate_backup_rows({"access_lists": [{"id": 4, "name": "internal"}]})

    def test_null_items_and_clients_are_accepted(self):
        npm_api.validate_backup_rows(
            {"access_lists": [{"id": 4, "name": "x", "items": None, "clients": None}]})


# =============================================================================
# cert_match_key
# =============================================================================

class TestCertMatchKey(unittest.TestCase):
    """A certificate's domain list reduced to something comparable.

    None rather than an empty frozenset when nothing usable is there: an empty
    frozenset equals every other empty frozenset, so two certificates with junk
    metadata would match each other and, worse, a lookup for one would happily
    return the other.
    """

    def test_lowercases_and_strips(self):
        self.assertEqual(npm_api.cert_match_key({"domain_names": ["  App.Example.COM "]}),
                         frozenset({"app.example.com"}))

    def test_order_does_not_matter(self):
        first = npm_api.cert_match_key(
            {"domain_names": ["a.example.com", "b.example.com"]})
        second = npm_api.cert_match_key(
            {"domain_names": ["b.example.com", "a.example.com"]})
        self.assertEqual(first, second)

    def test_repeats_collapse(self):
        self.assertEqual(
            npm_api.cert_match_key({"domain_names": ["a.example.com", "A.EXAMPLE.COM"]}),
            frozenset({"a.example.com"}))

    def test_an_empty_list_has_no_key(self):
        self.assertIsNone(npm_api.cert_match_key({"domain_names": []}))

    def test_a_missing_list_has_no_key(self):
        self.assertIsNone(npm_api.cert_match_key({}))

    def test_an_explicit_null_list_has_no_key(self):
        # NPM sends domain_names as a literal null on some uploaded
        # certificates, and dict.get returns that null rather than the default.
        self.assertIsNone(npm_api.cert_match_key({"domain_names": None}))

    def test_whitespace_only_entries_have_no_key(self):
        self.assertIsNone(npm_api.cert_match_key({"domain_names": ["", "   ", "\n"]}))

    def test_a_usable_entry_beside_junk_still_yields_a_key(self):
        self.assertEqual(
            npm_api.cert_match_key({"domain_names": ["  ", "app.example.com"]}),
            frozenset({"app.example.com"}))

    def test_no_key_is_none_not_an_empty_frozenset(self):
        # The distinction callers rely on: `if key is not None` has to be able
        # to tell "nothing recorded" from "recorded nothing".
        self.assertIsNot(npm_api.cert_match_key({"domain_names": []}), frozenset())


# =============================================================================
# map_certificates
# =============================================================================

class TestMapCertificates(unittest.TestCase):
    """Backup certificate IDs onto the target's, or onto nothing.

    Certificates are never restored — uploading them would mean POSTing private
    keys to an endpoint that is plain HTTP by default — so every host in the
    backup has to be re-pointed at a certificate that is already here.
    """

    def test_a_backup_id_is_never_carried_through(self):
        # The single most important property in restore. NPM assigns
        # certificate IDs on create, so a backup's 7 means nothing in another
        # instance: writing it back unchecked points the host at whatever
        # happens to be numbered 7 here, or at nothing at all, and NPM renders
        # a host with a dead certificate ID as having no TLS listener rather
        # than failing.
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["app.example.com"])],
            [_backup_cert(3, ["app.example.com"])])

        self.assertEqual(mapping, {7: 3})
        self.assertNotEqual(mapping[7], 7)

    def test_matching_is_order_and_case_insensitive(self):
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["B.example.com", "a.example.com"])],
            [_backup_cert(3, ["a.EXAMPLE.com", "b.example.com"])])

        self.assertEqual(mapping, {7: 3})

    def test_a_certificate_with_no_counterpart_maps_to_none(self):
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["gone.example.com"])],
            [_backup_cert(3, ["app.example.com"])])

        self.assertEqual(mapping, {7: None})

    def test_no_certificates_here_maps_everything_to_none(self):
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["app.example.com"]), _backup_cert(8, ["api.example.com"])],
            [])

        self.assertEqual(mapping, {7: None, 8: None})

    def test_every_backup_certificate_gets_an_entry(self):
        # restore_host_overrides looks each one up by ID; a missing key would
        # read as None anyway, but only by accident.
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["app.example.com"]), _backup_cert(8, ["api.example.com"])],
            [_backup_cert(3, ["app.example.com"])])

        self.assertEqual(set(mapping), {7, 8})

    def test_an_empty_backup_maps_nothing(self):
        self.assertEqual(
            npm_api.map_certificates([], [_backup_cert(3, ["app.example.com"])]), {})

    def test_a_backup_certificate_with_unusable_metadata_falls_back_to_its_name(self):
        # Uploaded certificates routinely come back with no domain list at all,
        # and the name is then the only thing left to match on.
        mapping = npm_api.map_certificates(
            [_backup_cert(7, [], nice_name="Internal Wildcard")],
            [_backup_cert(3, ["*.internal.lan"], nice_name="Internal Wildcard")])

        self.assertEqual(mapping, {7: 3})

    def test_a_target_certificate_with_unusable_metadata_is_reachable_by_name(self):
        # The same gap on the other side: the backup knows the domains, the
        # certificate installed here does not record them.
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["*.internal.lan"], nice_name="Internal Wildcard")],
            [_backup_cert(3, [], nice_name="Internal Wildcard")])

        self.assertEqual(mapping, {7: 3})

    def test_the_name_match_is_case_and_whitespace_insensitive(self):
        mapping = npm_api.map_certificates(
            [_backup_cert(7, [], nice_name="  Internal Wildcard  ")],
            [_backup_cert(3, [], nice_name="internal wildcard")])

        self.assertEqual(mapping, {7: 3})

    def test_the_domain_set_wins_over_the_name(self):
        # nice_name is free text a user can edit at any time; the domain set is
        # what actually decides whether a certificate can serve a host, so a
        # name collision must not override a real domain match.
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["app.example.com"], nice_name="shared")],
            [_backup_cert(3, ["other.example.com"], nice_name="shared"),
             _backup_cert(4, ["app.example.com"], nice_name="something else")])

        self.assertEqual(mapping, {7: 4})

    def test_a_nameless_certificate_with_no_domains_matches_nothing(self):
        mapping = npm_api.map_certificates(
            [_backup_cert(7, [], nice_name="")],
            [_backup_cert(3, [], nice_name="")])

        self.assertEqual(mapping, {7: None})

    def test_two_here_with_the_same_domains_resolve_to_the_first(self):
        # A renewed certificate uploaded beside the one it replaces is the
        # ordinary way to end up with two. Either would serve, so the choice
        # only has to be deterministic — an arbitrary one would make a restore
        # unreproducible.
        mapping = npm_api.map_certificates(
            [_backup_cert(7, ["app.example.com"])],
            [_backup_cert(3, ["app.example.com"]),
             _backup_cert(4, ["app.example.com"])])

        self.assertEqual(mapping, {7: 3})

    def test_two_here_with_the_same_name_resolve_to_the_first(self):
        mapping = npm_api.map_certificates(
            [_backup_cert(7, [], nice_name="wildcard")],
            [_backup_cert(3, [], nice_name="wildcard"),
             _backup_cert(4, [], nice_name="wildcard")])

        self.assertEqual(mapping, {7: 3})


# =============================================================================
# restore_host_overrides
# =============================================================================

class TestRestoreHostOverrides(unittest.TestCase):
    """Rewriting a backed-up host's ID references for this instance.

    Anything that cannot be resolved is cleared rather than carried over, and
    says so: a host pointing at an ID this NPM does not have is the exact
    failure the whole command exists to avoid.
    """

    def _overrides(self, host, cert_map=None, acl_map=None):
        return npm_api.restore_host_overrides(host, cert_map or {}, acl_map or {})

    def test_a_resolvable_certificate_is_remapped(self):
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=7), cert_map={7: 3})

        self.assertEqual(overrides["certificate_id"], 3)
        self.assertEqual(notes, [])

    def test_a_resolvable_certificate_leaves_the_ssl_flags_alone(self):
        # The host keeps serving HTTPS, so ssl_forced and hsts_enabled stay out
        # of the overrides entirely and create_host_from carries the backup's
        # own values through.
        overrides, _ = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=7, ssl_forced=True,
                        hsts_enabled=True), cert_map={7: 3})

        self.assertNotIn("ssl_forced", overrides)
        self.assertNotIn("hsts_enabled", overrides)

    def test_an_unresolvable_certificate_clears_all_three_and_says_so(self):
        # Forcing SSL with no certificate is strictly worse than plain HTTP:
        # NPM renders the redirect to https:// but omits the whole
        # `listen 443 ssl` block, so every request bounces to a closed port.
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=7, ssl_forced=True,
                        hsts_enabled=True), cert_map={7: None})

        self.assertIsNone(overrides["certificate_id"])
        self.assertIs(overrides["ssl_forced"], False)
        self.assertIs(overrides["hsts_enabled"], False)
        self.assertEqual(len(notes), 1)
        self.assertIn("certificate 7", notes[0])
        self.assertIn("HTTP-only", notes[0])

    def test_a_certificate_absent_from_the_map_is_also_unresolvable(self):
        # A host referencing a certificate the backup itself never recorded.
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=7), cert_map={})

        self.assertIsNone(overrides["certificate_id"])
        self.assertEqual(len(notes), 1)

    def test_a_host_with_no_certificate_says_nothing(self):
        # Plain HTTP in the backup, plain HTTP here. Noting it would bury the
        # hosts that really did lose something.
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=None))

        self.assertIsNone(overrides["certificate_id"])
        self.assertEqual(notes, [])

    def test_certificate_id_zero_is_no_certificate_not_certificate_zero(self):
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=0), cert_map={0: 3})

        self.assertIsNone(overrides["certificate_id"])
        self.assertEqual(notes, [])

    def test_a_resolvable_access_list_is_remapped(self):
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], access_list_id=4), acl_map={4: 501})

        self.assertEqual(overrides["access_list_id"], 501)
        self.assertEqual(notes, [])

    def test_an_unresolvable_access_list_becomes_zero_and_says_so(self):
        # 0 rather than None: that is NPM's own spelling of "nothing linked",
        # and it is a real loss of access control, so it is named.
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], access_list_id=4), acl_map={4: None})

        self.assertEqual(overrides["access_list_id"], 0)
        self.assertEqual(len(notes), 1)
        self.assertIn("access list 4", notes[0])
        self.assertIn("access control dropped", notes[0])

    def test_access_list_zero_stays_zero_without_a_note(self):
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], access_list_id=0))

        self.assertEqual(overrides["access_list_id"], 0)
        self.assertEqual(notes, [])

    def test_a_null_access_list_stays_zero_without_a_note(self):
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], access_list_id=None))

        self.assertEqual(overrides["access_list_id"], 0)
        self.assertEqual(notes, [])

    def test_both_losses_are_reported_together(self):
        # One host can lose both, and the preview prints the notes joined, so
        # neither may swallow the other.
        overrides, notes = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=7, access_list_id=4),
            cert_map={7: None}, acl_map={4: None})

        self.assertIsNone(overrides["certificate_id"])
        self.assertEqual(overrides["access_list_id"], 0)
        self.assertEqual(len(notes), 2)

    def test_the_backed_up_host_is_not_modified(self):
        # It is read again for the preview and once more when the host is
        # created, so the overrides have to stay a separate dict.
        host = _merge_host(1, ["app.example.com"], certificate_id=7, access_list_id=4)

        self._overrides(host, cert_map={7: None}, acl_map={4: None})

        self.assertEqual(host["certificate_id"], 7)
        self.assertEqual(host["access_list_id"], 4)

    def test_only_the_reference_fields_are_overridden(self):
        # Everything else about the host comes back exactly as backed up, so
        # the overrides must not quietly grow a third key.
        overrides, _ = self._overrides(
            _merge_host(1, ["app.example.com"], certificate_id=7), cert_map={7: 3})

        self.assertEqual(set(overrides), {"certificate_id", "access_list_id"})


# =============================================================================
# restore_acl_payload
# =============================================================================

class TestRestoreAclPayload(unittest.TestCase):
    """A backed-up access list reduced to create_access_list's arguments."""

    def test_the_name_is_carried_through(self):
        payload, _ = npm_api.restore_acl_payload(_backup_acl(4, "internal"))
        self.assertEqual(payload["name"], "internal")

    def test_item_rows_are_rebuilt_without_the_backups_ids(self):
        # `id` and `access_list_id` belong to the instance the backup came from.
        # Echoing them back sends NPM a row claiming to already exist somewhere.
        payload, _ = npm_api.restore_acl_payload(_backup_acl(4, "internal"))

        self.assertEqual(payload["items"],
                         [{"username": "ops", "password": "s3cret"}])

    def test_client_rows_are_rebuilt_without_the_backups_ids(self):
        payload, _ = npm_api.restore_acl_payload(_backup_acl(4, "internal"))

        self.assertEqual(payload["clients"],
                         [{"address": "10.0.0.0/8", "directive": "allow"}])

    def test_a_password_that_is_not_in_the_backup_is_noted(self):
        # NPM's API returns access list items without their password, so this is
        # the ordinary case rather than a corrupt backup: the list comes back
        # with its structure intact and its credentials empty, and saying so is
        # the only way the operator learns to set them again.
        for password in ("", None):
            with self.subTest(password=password):
                acl = _backup_acl(4, "internal",
                                  items=[{"username": "ops", "password": password}])

                payload, notes = npm_api.restore_acl_payload(acl)

                self.assertEqual(payload["items"], [{"username": "ops", "password": ""}])
                self.assertEqual(len(notes), 1)
                self.assertIn("ops", notes[0])

    def test_a_missing_password_key_is_noted_too(self):
        acl = _backup_acl(4, "internal", items=[{"username": "ops"}])

        payload, notes = npm_api.restore_acl_payload(acl)

        self.assertEqual(payload["items"], [{"username": "ops", "password": ""}])
        self.assertEqual(len(notes), 1)

    def test_a_password_that_is_there_is_carried_and_not_noted(self):
        payload, notes = npm_api.restore_acl_payload(_backup_acl(4, "internal"))

        self.assertEqual(payload["items"][0]["password"], "s3cret")
        self.assertEqual(notes, [])

    def test_one_note_per_credentialless_user(self):
        acl = _backup_acl(4, "internal", items=[{"username": "ops"},
                                                {"username": "ci", "password": "x"},
                                                {"username": "audit", "password": ""}])

        _, notes = npm_api.restore_acl_payload(acl)

        self.assertEqual(len(notes), 2)
        self.assertIn("ops", notes[0])
        self.assertIn("audit", notes[1])

    def test_directive_defaults_to_allow(self):
        # An empty directive would render as a bare address in the nginx
        # snippet, which is a config error rather than a permissive default.
        for given in (None, "", "missing"):
            with self.subTest(directive=given):
                client = {"address": "10.0.0.0/8"}
                if given != "missing":
                    client["directive"] = given
                acl = _backup_acl(4, "internal", clients=[client])

                payload, _ = npm_api.restore_acl_payload(acl)

                self.assertEqual(payload["clients"][0]["directive"], "allow")

    def test_an_explicit_deny_is_kept(self):
        acl = _backup_acl(4, "internal",
                          clients=[{"address": "10.0.0.0/8", "directive": "deny"}])

        payload, _ = npm_api.restore_acl_payload(acl)

        self.assertEqual(payload["clients"][0]["directive"], "deny")

    def test_satisfy_any_and_pass_auth_become_real_booleans(self):
        # NPM stores them as 0/1 in some releases and true/false in others, and
        # the create endpoint rejects the integers.
        for given, expected in ((1, True), (0, False), (True, True), (False, False),
                                (None, False)):
            with self.subTest(given=given):
                acl = _backup_acl(4, "internal", satisfy_any=given, pass_auth=given)

                payload, _ = npm_api.restore_acl_payload(acl)

                self.assertIs(payload["satisfy_any"], expected)
                self.assertIs(payload["pass_auth"], expected)

    def test_missing_flags_are_false(self):
        acl = _backup_acl(4, "internal")
        del acl["satisfy_any"]
        del acl["pass_auth"]

        payload, _ = npm_api.restore_acl_payload(acl)

        self.assertIs(payload["satisfy_any"], False)
        self.assertIs(payload["pass_auth"], False)

    def test_no_items_or_clients_yields_empty_lists(self):
        for value in ([], None, "missing"):
            with self.subTest(value=value):
                acl = _backup_acl(4, "internal")
                if value == "missing":
                    del acl["items"]
                    del acl["clients"]
                else:
                    acl["items"] = acl["clients"] = value

                payload, notes = npm_api.restore_acl_payload(acl)

                self.assertEqual(payload["items"], [])
                self.assertEqual(payload["clients"], [])
                self.assertEqual(notes, [])

    def test_the_payload_holds_exactly_what_create_access_list_takes(self):
        # It is splatted straight in as **kwargs, so an extra key is a
        # TypeError at apply time, after the target has already been emptied.
        payload, _ = npm_api.restore_acl_payload(_backup_acl(4, "internal"))

        self.assertEqual(set(payload),
                         {"name", "satisfy_any", "pass_auth", "items", "clients"})


# =============================================================================
# restore
# =============================================================================

class _RestoreCommandTestCase(_WorkdirTestCase, _ConsoleTestCase):
    """Runs the restore command against a stub, off a real backup file on disk.

    The backup is written for real rather than load_backup being patched out:
    the file is the command's whole input, and reading it is the one step no
    stub should stand in for.
    """

    def setUp(self):
        super().setUp()
        self.backup_dir = self.workdir / "backup"
        self.state_dir = self.workdir / "state"
        self.backup_dir.mkdir()

    def _backup_file(self, **sections):
        path = self.backup_dir / "full_config_2026_08_24__11_20_02.json"
        path.write_text(json.dumps(sections, indent=2))
        return path

    def _client(self, **kwargs):
        return _RestoreCommandClient(self.state_dir, **kwargs)

    def _restore(self, client, source, **overrides):
        options = dict(source=str(source), preview=False, yes=True)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.restore(**options)

    def _restore_expecting_exit(self, client, source, **overrides):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._restore(client, source, **overrides)
        return caught.exception


class TestRestoreOrdering(_RestoreCommandTestCase):
    """What has to happen before what.

    NPM will not drop an access list a host still references, and a host cannot
    be created pointing at an access list that does not exist yet, so the four
    phases only work in one order.
    """

    def test_a_fresh_target_deletes_nothing(self):
        # Restoring into a new NPM is the intended use, and it must not look
        # like a destructive operation when there is nothing there to destroy.
        source = self._backup_file(access_lists=[_backup_acl(4, "internal")],
                                   proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client()

        self._restore(client, source)

        self.assertEqual(client.deleted_host_ids, [])
        self.assertEqual(client.deleted_acl_ids, [])
        self.assertEqual(client.kinds, ["create_acl", "create_host"])

    def test_access_lists_are_created_before_any_host(self):
        source = self._backup_file(
            access_lists=[_backup_acl(4, "internal"), _backup_acl(5, "ops")],
            proxy_hosts=[_merge_host(1, ["app.example.com"]),
                         _merge_host(2, ["api.example.com"])])
        client = self._client()

        self._restore(client, source)

        self.assertEqual(client.kinds,
                         ["create_acl", "create_acl", "create_host", "create_host"])

    def test_existing_hosts_are_removed_before_existing_access_lists(self):
        # The other way round, NPM refuses the access list delete while a host
        # still references it, and the restore stalls with the target
        # half-emptied.
        source = self._backup_file(access_lists=[_backup_acl(4, "internal")],
                                   proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}, {"id": 62}], acls=[{"id": 71}])

        self._restore(client, source)

        self.assertEqual(client.kinds, ["delete_host", "delete_host", "delete_acl",
                                        "create_acl", "create_host"])
        self.assertEqual(client.deleted_host_ids, [61, 62])
        self.assertEqual(client.deleted_acl_ids, [71])

    def test_nothing_is_created_until_the_target_is_emptied(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}])

        self._restore(client, source)

        creates = [i for i, kind in enumerate(client.kinds) if kind.startswith("create")]
        deletes = [i for i, kind in enumerate(client.kinds) if kind.startswith("delete")]
        self.assertLess(max(deletes), min(creates))

    def test_settings_are_written_last(self):
        # After the hosts, because default-site decides what nginx answers for
        # an unrecognised Host header and the answer is more useful once the
        # recognised ones are back.
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])],
                                   settings=[_backup_setting("default-site")])
        client = self._client(settings=[_backup_setting("default-site", "404")])

        self._restore(client, source)

        self.assertEqual(client.kinds, ["create_host", "setting"])

    def test_the_snapshot_is_written_before_the_first_delete(self):
        # The snapshot is the only record of the configuration about to be
        # replaced, and the deletes are irreversible, so the ordering is the
        # guarantee.
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}])

        with mock.patch.object(npm_api, "write_state_snapshot",
                               side_effect=OSError("Read-only file system")):
            exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Read-only file system")
        self.assertPrinted("Refusing to delete configuration")

    def test_the_snapshot_records_the_configuration_being_replaced(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}],
                              settings=[_backup_setting("default-site", "404")])

        self._restore(client, source)

        written = list(self.state_dir.glob("pre_restore_*.json"))
        self.assertEqual(len(written), 1, written)
        assert_owner_only(self, written[0])
        recorded = json.loads(written[0].read_text())
        self.assertEqual(recorded["proxy_hosts"], [{"id": 61}])
        self.assertEqual(recorded["access_lists"], [{"id": 71}])
        self.assertEqual(recorded["restored_from"], str(source))


class TestRestoreRemapping(_RestoreCommandTestCase):
    """Every ID in a backup is meaningless here, and has to be looked up again."""

    def test_a_hosts_access_list_id_comes_from_the_create_response(self):
        # NPM assigns the ID on create and offers no way to ask for a
        # particular one, so the backup's 4 cannot be reused even when nothing
        # else in the target is using it.
        source = self._backup_file(
            access_lists=[_backup_acl(4, "internal")],
            proxy_hosts=[_merge_host(1, ["app.example.com"], access_list_id=4)])
        client = self._client()

        self._restore(client, source)

        created_acl_id = client._next_acl_id
        _, overrides = client.created_hosts[0]
        self.assertEqual(overrides["access_list_id"], created_acl_id)
        self.assertNotEqual(overrides["access_list_id"], 4)

    def test_a_backup_certificate_id_is_never_written_to_a_host(self):
        source = self._backup_file(
            certificates=[_backup_cert(7, ["app.example.com"])],
            proxy_hosts=[_merge_host(1, ["app.example.com"], certificate_id=7)])
        client = self._client(certs=[_backup_cert(3, ["app.example.com"])])

        self._restore(client, source)

        _, overrides = client.created_hosts[0]
        self.assertEqual(overrides["certificate_id"], 3)

    def test_a_host_whose_certificate_has_no_match_comes_back_http_only(self):
        source = self._backup_file(
            certificates=[_backup_cert(7, ["gone.example.com"])],
            proxy_hosts=[_merge_host(1, ["app.example.com"], certificate_id=7,
                                     ssl_forced=True, hsts_enabled=True)])
        client = self._client(certs=[_backup_cert(3, ["other.example.com"])])

        self._restore(client, source)

        _, overrides = client.created_hosts[0]
        self.assertIsNone(overrides["certificate_id"])
        self.assertIs(overrides["ssl_forced"], False)
        self.assertIs(overrides["hsts_enabled"], False)
        self.assertPrinted("HTTP-only")

    def test_the_hosts_that_lost_something_are_named_at_the_end(self):
        # The run finishes successfully, so without a closing summary the
        # per-host warnings scroll away and the operator never repoints them.
        source = self._backup_file(
            certificates=[_backup_cert(7, ["gone.example.com"])],
            proxy_hosts=[_merge_host(1, ["app.example.com"], certificate_id=7)])
        client = self._client()

        self._restore(client, source)

        self.assertPrinted("host bulk-update certificate_id")

    def test_a_host_referencing_an_access_list_the_backup_lacks_loses_it(self):
        source = self._backup_file(
            access_lists=[],
            proxy_hosts=[_merge_host(1, ["app.example.com"], access_list_id=4)])
        client = self._client()

        self._restore(client, source)

        _, overrides = client.created_hosts[0]
        self.assertEqual(overrides["access_list_id"], 0)
        self.assertPrinted("access control dropped")

    def test_the_access_list_payload_reaches_create_access_list_rebuilt(self):
        source = self._backup_file(access_lists=[_backup_acl(4, "internal")])
        client = self._client()

        self._restore(client, source)

        self.assertEqual(client.created_acls[0]["name"], "internal")
        self.assertEqual(client.created_acls[0]["items"],
                         [{"username": "ops", "password": "s3cret"}])


class TestRestoreScope(_RestoreCommandTestCase):
    """What restore refuses to touch, whatever the backup holds."""

    def test_users_are_never_created(self):
        # NPM's API never exports password material, so a restored user could
        # only be created with an invented password — an account nobody can log
        # into and nobody knows is there.
        source = self._backup_file(
            proxy_hosts=[_merge_host(1, ["app.example.com"])],
            users=[{"id": 1, "name": "Admin", "email": "admin@example.com"},
                   {"id": 2, "name": "Ops", "email": "ops@example.com"}])
        client = self._client()

        self._restore(client, source)

        self.assertEqual(client.forbidden_calls, [])

    def test_certificates_are_never_created_deleted_or_uploaded(self):
        # Uploading one would mean POSTing a private key to an endpoint that is
        # plain HTTP by default. They are read for matching and nothing else.
        source = self._backup_file(
            certificates=[_backup_cert(7, ["app.example.com"]),
                          _backup_cert(8, ["gone.example.com"])],
            proxy_hosts=[_merge_host(1, ["app.example.com"], certificate_id=7)])
        client = self._client(certs=[_backup_cert(3, ["app.example.com"])])

        self._restore(client, source)

        self.assertEqual(client.forbidden_calls, [])

    def test_certificates_already_here_are_left_alone_even_with_no_match(self):
        source = self._backup_file(
            certificates=[_backup_cert(7, ["gone.example.com"])],
            proxy_hosts=[_merge_host(1, ["app.example.com"], certificate_id=7)])
        client = self._client(certs=[_backup_cert(3, ["unrelated.example.com"])])

        self._restore(client, source)

        self.assertEqual(client.forbidden_calls, [])

    def test_only_settings_this_npm_already_defines_are_written(self):
        # A backup from a later NPM must not introduce a setting this instance
        # has never had; NPM's PUT would create it and nothing here understands
        # what it does.
        source = self._backup_file(
            settings=[_backup_setting("default-site", "congratulations"),
                      _backup_setting("some-future-setting", "on")])
        client = self._client(settings=[_backup_setting("default-site", "404")])

        self._restore(client, source)

        self.assertEqual([setting_id for setting_id, _ in client.written_settings],
                         ["default-site"])

    def test_a_skipped_setting_is_reported_in_the_preview(self):
        source = self._backup_file(
            settings=[_backup_setting("default-site"),
                      _backup_setting("some-future-setting", "on")])
        client = self._client(settings=[_backup_setting("default-site", "404")])

        self._restore(client, source, preview=True)

        self.assertPrinted("some-future-setting")
        self.assertPrinted("not defined on this NPM")

    def test_a_setting_is_written_as_value_and_meta(self):
        source = self._backup_file(
            settings=[{"id": "default-site", "value": "html", "meta": {"html": "<p>hi"}}])
        client = self._client(settings=[_backup_setting("default-site", "404")])

        self._restore(client, source)

        self.assertEqual(client.written_settings,
                         [("default-site", {"value": "html", "meta": {"html": "<p>hi"}})])

    def test_a_setting_with_no_meta_is_written_with_an_empty_one(self):
        source = self._backup_file(settings=[{"id": "default-site", "value": "404"}])
        client = self._client(settings=[_backup_setting("default-site")])

        self._restore(client, source)

        self.assertEqual(client.written_settings[0][1]["meta"], {})


class TestRestoreRefusals(_RestoreCommandTestCase):
    """When restore stops before writing anything."""

    def test_a_backup_holding_none_of_the_restored_sections_exits_1(self):
        # Certificates and users are the two sections restore reads but never
        # writes, so a backup of only those is a valid backup and still nothing
        # this command can do.
        source = self._backup_file(certificates=[_backup_cert(7, ["app.example.com"])],
                                   users=[{"id": 1, "email": "admin@example.com"}])
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}])

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("holds nothing this command restores")

    def test_that_refusal_happens_before_the_target_is_even_read(self):
        # It has to come first: reading the target is harmless, but the message
        # would then arrive after a screen of preview about a restore that is
        # not going to happen.
        source = self._backup_file(users=[{"id": 1}])

        self._restore_expecting_exit(self._client(hosts=[{"id": 61}]), source)

        self.assertNotPrinted("Restore Preview")

    def test_an_unreadable_backup_raises_npmerror_before_anything_is_read(self):
        # NPMError rather than typer.Exit: main() renders it as one line, and
        # the command has done nothing that needs unwinding.
        missing = self.backup_dir / "full_config_nope.json"
        client = self._client(hosts=[{"id": 61}])

        with mock.patch.object(npm_api, "get_client", lambda: client):
            with self.assertRaises(npm_api.NPMError):
                npm_api.restore(source=str(missing), preview=False, yes=True)

        self.assertEqual(client.calls, [])

    def test_declining_the_confirmation_writes_nothing(self):
        source = self._backup_file(access_lists=[_backup_acl(4, "internal")],
                                   proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}])

        with mock.patch.object(npm_api.typer, "confirm", return_value=False):
            exit_exc = self._restore_expecting_exit(client, source, yes=False)

        # 0, not 1: the user got what they asked for.
        self.assertEqual(exit_exc.exit_code, 0)
        self.assertEqual(client.calls, [])
        self.assertFalse(list(self.state_dir.glob("*"))
                         if self.state_dir.exists() else [])

    def test_a_non_empty_target_is_warned_about_before_the_confirmation(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}, {"id": 62}], acls=[{"id": 71}])

        with mock.patch.object(npm_api.typer, "confirm", return_value=False) as confirm:
            self._restore_expecting_exit(client, source, yes=False)

        self.assertPrinted("This NPM is not empty")
        self.assertPrinted("2 proxy host(s) and 1 access list(s) will be DELETED")
        self.assertPrinted("cancel and run `npm-api backup` first")
        confirm.assert_called_once()


class TestRestoreFailures(_RestoreCommandTestCase):
    """One object failing must not abandon the rest, and must not exit 0."""

    def test_a_failed_host_create_does_not_stop_the_others(self):
        source = self._backup_file(
            proxy_hosts=[_merge_host(1, ["a.example.com"]),
                         _merge_host(2, ["b.example.com"]),
                         _merge_host(3, ["c.example.com"])])
        client = self._client(host_create_failures={2})

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual([host_id for host_id, _ in client.created_hosts], [1, 2, 3])
        self.assertPrinted("Successful: 2")
        self.assertPrinted("Failed: 1")

    def test_a_failed_access_list_create_still_lets_the_hosts_through(self):
        # The hosts that referenced it lose their access control and are told
        # so, but a host that never used it has no reason to be held back.
        source = self._backup_file(
            access_lists=[_backup_acl(4, "internal")],
            proxy_hosts=[_merge_host(1, ["app.example.com"], access_list_id=4)])
        client = self._client(acl_create_failures={"internal"})

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        _, overrides = client.created_hosts[0]
        self.assertEqual(overrides["access_list_id"], 0)
        self.assertPrinted("access control dropped")

    def test_a_failed_delete_is_counted_and_the_restore_continues(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}, {"id": 62}],
                              delete_host_error_ids={61})

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.deleted_host_ids, [61, 62])
        self.assertEqual([host_id for host_id, _ in client.created_hosts], [1])

    def test_a_refused_delete_counts_as_a_failure(self):
        # delete_host reports a refusal by returning False rather than raising,
        # so a caller testing only for exceptions would report a clean run over
        # a host that is still there and still holding its domains.
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}], delete_host_refuse_ids={61})

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertPrinted("Could not remove host 61")

    def test_a_failed_setting_is_counted_without_stopping_the_rest(self):
        source = self._backup_file(settings=[_backup_setting("default-site"),
                                             _backup_setting("other")])
        client = self._client(settings=[_backup_setting("default-site"),
                                        _backup_setting("other")],
                              setting_failures={"default-site"})

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual([setting_id for setting_id, _ in client.written_settings],
                         ["default-site", "other"])

    def test_an_npm_error_is_caught_like_an_http_error(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"]),
                                                _merge_host(2, ["b.example.com"])])
        client = self._client(host_create_failures={1},
                              error=npm_api.NPMError("NPM is unreachable"))

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertPrinted("NPM is unreachable")
        self.assertEqual([host_id for host_id, _ in client.created_hosts], [1, 2])


class TestRestoreCleanRun(_RestoreCommandTestCase):
    """The ordinary path: a backup, a fresh NPM, exit 0."""

    def test_a_clean_run_returns_normally(self):
        source = self._backup_file(
            access_lists=[_backup_acl(4, "internal")],
            proxy_hosts=[_merge_host(1, ["app.example.com"], access_list_id=4)],
            settings=[_backup_setting("default-site")])
        client = self._client(settings=[_backup_setting("default-site", "404")])

        self._restore(client, source)

        self.assertPrinted("Successful: 3")
        self.assertNotPrinted("Failed:")

    def test_a_quiet_restore_says_nothing_about_dropped_references(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])

        self._restore(self._client(), source)

        self.assertNotPrinted("HTTP-only")
        self.assertNotPrinted("access control dropped")

    def test_the_preview_names_the_backup_it_read(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])

        self._restore(self._client(), source, preview=True)

        self.assertPrinted("Restore Preview")
        self.assertPrinted(str(source))

    def test_the_preview_reports_certificate_matching_before_anything_is_written(self):
        # The one part of a restore the operator cannot fix afterwards without
        # knowing which hosts are affected, so it has to be visible up front.
        source = self._backup_file(
            certificates=[_backup_cert(7, ["gone.example.com"])],
            proxy_hosts=[_merge_host(1, ["app.example.com"], certificate_id=7)])
        client = self._client()

        self._restore(client, source, preview=True)

        self.assertPrinted("come back with something dropped")
        self.assertPrinted("app.example.com")


class TestRestoreSkipsSectionsTheBackupCannotSpeakFor(_RestoreCommandTestCase):
    """A section the backup says nothing about is left standing.

    full_backup only assigns a section once its fetch has returned, so a
    section whose fetch failed is missing from the file entirely. Read as [],
    it became an instruction to delete every one of them and put none back —
    silently, and with no symptom until someone looked.
    """

    def test_an_absent_proxy_hosts_section_deletes_no_hosts(self):
        source = self._backup_file(access_lists=[_backup_acl(4, "internal")])
        client = self._client(hosts=[{"id": 61}, {"id": 62}], acls=[{"id": 71}])

        self._restore(client, source)

        self.assertEqual(client.deleted_host_ids, [])
        self.assertEqual(client.deleted_acl_ids, [71])

    def test_an_absent_access_lists_section_deletes_no_access_lists(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}])

        self._restore(client, source)

        self.assertEqual(client.deleted_host_ids, [61])
        self.assertEqual(client.deleted_acl_ids, [])

    def test_skipping_certificates_warns_that_hosts_come_back_without_tls(self):
        # Certificates are never deleted or created by restore, so the generic
        # "nothing in that section will be deleted or created" line is true
        # here and useless. Matching is the only thing that maps the backup's
        # certificate IDs onto this NPM's, so skipping it is precisely what
        # strips TLS from every host that comes back.
        source = self._backup_file(
            proxy_hosts=[_merge_host(1, ["app.example.com"], certificate_id=3)],
            access_lists=[], settings=[],
            incomplete_sections={"certificates": "HTTP 500"})
        client = self._client(hosts=[])

        self._restore(client, source)

        self.assertPrinted("without TLS")
        self.assertNotPrinted("deleted or created here")

    def test_an_explicitly_empty_section_still_wipes(self):
        # The other half of the pair. [] says the source instance had none, and
        # replacing the target's with none is then exactly right.
        source = self._backup_file(proxy_hosts=[],
                                   access_lists=[_backup_acl(4, "internal")])
        client = self._client(hosts=[{"id": 61}])

        self._restore(client, source)

        self.assertEqual(client.deleted_host_ids, [61])

    def test_the_skipped_section_is_named_out_loud(self):
        source = self._backup_file(access_lists=[_backup_acl(4, "internal")])

        self._restore(self._client(hosts=[{"id": 61}]), source)

        self.assertPrinted("Skipping proxy hosts")
        self.assertPrinted("no such section")

    def test_a_section_recorded_as_incomplete_is_skipped_though_present(self):
        # The marker full_backup writes when a fetch failed part way. Whatever
        # landed in the section is not the whole of it, so it cannot be used to
        # decide what to delete.
        source = self._backup_file(
            proxy_hosts=[_merge_host(1, ["app.example.com"])],
            access_lists=[_backup_acl(4, "internal")],
            incomplete_sections={"proxy_hosts": "500 Server Error"})
        client = self._client(hosts=[{"id": 61}])

        self._restore(client, source)

        self.assertEqual(client.deleted_host_ids, [])
        self.assertEqual(client.created_hosts, [])
        self.assertPrinted("500 Server Error")

    def test_a_backup_that_can_speak_for_nothing_exits_1(self):
        # Every restorable section skipped. Without this the command would run
        # to the end having done nothing at all and exit 0.
        source = self._backup_file(
            proxy_hosts=[_merge_host(1, ["app.example.com"])],
            incomplete_sections={"proxy_hosts": "500 Server Error"})
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}])

        exit_exc = self._restore_expecting_exit(client, source)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.calls, [])

    def test_the_warning_says_how_many_are_really_deleted(self):
        source = self._backup_file(proxy_hosts=[_merge_host(1, ["app.example.com"])])
        client = self._client(hosts=[{"id": 61}], acls=[{"id": 71}, {"id": 72}])

        with mock.patch.object(npm_api.typer, "confirm", return_value=False):
            self._restore_expecting_exit(client, source, yes=False)

        self.assertPrinted("only 1 proxy host(s) and 0 access list(s)")


class TestRestoreValidatesBeforeItDeletes(_RestoreCommandTestCase):
    """A malformed row must stop the run while the target is still intact.

    restore_acl_payload and host_config_payload both run in the rebuild loop,
    which is reached only after every host and access list on the target has
    been deleted. An AttributeError there leaves a live NPM emptied.
    """

    def _target(self):
        return self._client(hosts=[{"id": 61}], acls=[{"id": 71}])

    def test_a_null_row_stops_the_run_with_nothing_deleted(self):
        source = self._backup_file(
            proxy_hosts=[_merge_host(1, ["app.example.com"]), None])
        client = self._target()

        with self.assertRaises(npm_api.NPMError):
            self._restore(client, source)

        self.assertEqual(client.calls, [])

    def test_a_malformed_nested_row_stops_the_run_with_nothing_deleted(self):
        source = self._backup_file(
            access_lists=[_backup_acl(4, "internal", items=[None])])
        client = self._target()

        with self.assertRaises(npm_api.NPMError):
            self._restore(client, source)

        self.assertEqual(client.calls, [])

    def test_a_section_of_the_wrong_type_stops_the_run(self):
        source = self._backup_file(
            proxy_hosts={"12": _merge_host(12, ["app.example.com"])})
        client = self._target()

        with self.assertRaises(npm_api.NPMError):
            self._restore(client, source)

        self.assertEqual(client.calls, [])

    def test_a_bad_certificate_row_is_caught_though_certificates_are_never_written(self):
        # Matching walks every row of the section, so a null there crashes the
        # run just as readily as one in a section that is restored.
        source = self._backup_file(
            proxy_hosts=[_merge_host(1, ["app.example.com"])],
            certificates=[None])
        client = self._target()

        with self.assertRaises(npm_api.NPMError):
            self._restore(client, source)

        self.assertEqual(client.calls, [])

    def test_the_refusal_names_the_section_and_the_row(self):
        source = self._backup_file(settings=[_backup_setting("default-site"), None])

        with self.assertRaises(npm_api.NPMError) as caught:
            self._restore(self._target(), source)

        self.assertIn("settings", str(caught.exception))
        self.assertIn("1", str(caught.exception))

    def test_validation_happens_before_the_target_is_read(self):
        # Reading the target is harmless, but the message would then arrive
        # after a screen of preview about a restore that cannot happen.
        source = self._backup_file(proxy_hosts=[None])
        client = self._target()

        with self.assertRaises(npm_api.NPMError):
            self._restore(client, source, preview=True)

        self.assertNotPrinted("Restore Preview")


# =============================================================================
# domain_base
# =============================================================================

class TestDomainBase(unittest.TestCase):
    """The registrable base of a name — the complement of domain_prefix.

    Used to notice that one host would answer to two unrelated names, which is
    a question about the names themselves and never about what a certificate
    claims to cover.
    """

    def test_case_is_preserved(self):
        # Pinned deliberately, to say where case folding lives. domain_base is
        # a plain "last two labels" function and keeps whatever NPM stored,
        # exactly as domain_prefix does; callers that compare bases fold case
        # themselves, which is the convention every other domain comparison in
        # the tool follows (dedupe_domains, cert_match_key, _domain_conflicts).
        self.assertEqual(npm_api.domain_base("App.Example.COM"), "Example.COM")

    def test_a_subdomain_reduces_to_its_base(self):
        self.assertEqual(npm_api.domain_base("ex.example.com"), "example.com")

    def test_an_apex_is_its_own_base(self):
        self.assertEqual(npm_api.domain_base("example.com"), "example.com")

    def test_a_deep_subdomain_still_reduces_to_two_labels(self):
        self.assertEqual(npm_api.domain_base("a.b.c.example.com"), "example.com")

    def test_a_single_label_has_no_base(self):
        # "localhost" or a short internal name: there is no base to compare it
        # against, and inventing one would put it beside real domains.
        self.assertIsNone(npm_api.domain_base("localhost"))

    def test_an_empty_string_has_no_base(self):
        self.assertIsNone(npm_api.domain_base(""))

    def test_dots_and_space_alone_have_no_base(self):
        for given in (".", "...", "   ", " . "):
            with self.subTest(given=given):
                self.assertIsNone(npm_api.domain_base(given))

    def test_trailing_dot_and_surrounding_space_ignored(self):
        self.assertEqual(npm_api.domain_base("  ex.example.com.  "), "example.com")

    def test_a_wildcard_label_does_not_change_the_base(self):
        # A certificate's *.example.com and a host's app.example.com belong to
        # the same base, which is the whole point of grouping by it.
        self.assertEqual(npm_api.domain_base("*.example.com"), "example.com")

    def test_empty_labels_are_ignored(self):
        # A doubled dot is a typo NPM will happily store. Counting the empty
        # label would make the base of "example..com" come back as ".com", and
        # every differently mistyped name would then group under that same
        # bogus base instead of being reported as the odd one out.
        for given in ("a..b.example.com", "example..com", "a.example..com"):
            with self.subTest(given=given):
                self.assertEqual(npm_api.domain_base(given), "example.com")

    def test_a_multipart_suffix_reads_as_the_suffix(self):
        # Documented limitation, not a bug, and deliberately the same one
        # domain_prefix carries: the registrable base is assumed to be two
        # labels. Getting .co.uk right needs public-suffix data that neither
        # this tool nor NPM has. Two hosts under .co.uk therefore look like one
        # base to the mixed-base warning, which under-warns rather than
        # over-warns — the safe direction for an advisory message.
        self.assertEqual(npm_api.domain_base("ex.example.co.uk"), "co.uk")

    def test_prefix_and_base_reassemble_the_original(self):
        # The two functions split one name between them, so anything they
        # disagree about is a name one of them is mis-reading.
        for domain in ("ex.example.com", "sub.ex.example.com", "a.b.c.example.com",
                       "*.example.com", "ex.example.co.uk"):
            with self.subTest(domain=domain):
                prefix = npm_api.domain_prefix(domain)
                base = npm_api.domain_base(domain)
                self.assertEqual(f"{prefix}.{base}", domain)

    def test_an_apex_is_all_base_and_no_prefix(self):
        self.assertIsNone(npm_api.domain_prefix("example.com"))
        self.assertEqual(npm_api.domain_base("example.com"), "example.com")

    def test_both_agree_a_single_label_has_neither(self):
        self.assertIsNone(npm_api.domain_prefix("localhost"))
        self.assertIsNone(npm_api.domain_base("localhost"))


# =============================================================================
# warn_on_mixed_bases
# =============================================================================

class TestWarnOnMixedBases(_ConsoleTestCase):
    """Advisory only, and checked against the names rather than the certificate.

    One NPM host is one nginx server block with one ssl_certificate, so every
    name on it has to be covered by that one certificate. NPM keeps a
    certificate's domain_names as free-form metadata it never consults when
    serving, so for uploaded certificates it is routinely unusable and
    cert_covers_domain answers "cannot tell" — which is exactly when this
    warning earns its place, so it must not depend on that metadata.
    """

    def test_one_base_is_returned_quietly(self):
        self.assertEqual(
            npm_api.warn_on_mixed_bases(["app.example.com"], "Host 12"),
            ["example.com"])
        self.assertEqual(self.console.text, "")

    def test_many_domains_on_one_base_stay_quiet(self):
        bases = npm_api.warn_on_mixed_bases(
            ["app.example.com", "api.example.com", "www.example.com", "example.com"],
            "Host 12")

        self.assertEqual(bases, ["example.com"])
        self.assertEqual(self.console.text, "")

    def test_one_base_spelled_two_ways_is_still_one_base(self):
        # Regression. domain_base keeps NPM's spelling, and the set was built
        # from its raw output, so "App.Example.com" and "api.example.com" came
        # out as two entries and the warning fired on a perfectly good merge —
        # reading "2 unrelated base domains: Example.com, example.com".
        #
        # Not contrived: DNS is case-insensitive and NPM stores whatever was
        # typed, dedupe_domains keeps the *first* spelling it sees, and every
        # other domain comparison here already folds case. A warning whose
        # whole value is that it fires rarely cannot afford a false alarm.
        bases = npm_api.warn_on_mixed_bases(
            ["App.Example.com", "api.example.com", "WWW.EXAMPLE.COM"], "Host 12")

        self.assertEqual(bases, ["example.com"])
        self.assertEqual(self.console.text, "")

    def test_case_folding_does_not_hide_a_genuine_second_base(self):
        bases = npm_api.warn_on_mixed_bases(
            ["App.Example.com", "nas.EXAMPLE.org"], "Host 12")

        self.assertEqual(bases, ["example.com", "example.org"])
        self.assertPrinted("2 unrelated base domains")

    def test_two_bases_are_both_returned_and_named(self):
        bases = npm_api.warn_on_mixed_bases(
            ["app.example.com", "nas.example.org"], "Host 12")

        self.assertEqual(bases, ["example.com", "example.org"])
        self.assertPrinted("2 unrelated base domains")
        self.assertPrinted("example.com, example.org")

    def test_the_warning_names_its_subject(self):
        # Called from merge and from clone, which are talking about different
        # things — the surviving host in one case, a host that does not exist
        # yet in the other.
        npm_api.warn_on_mixed_bases(["app.example.com", "nas.example.org"], "The new host")
        self.assertPrinted("The new host will answer to")

    def test_the_warning_explains_the_single_certificate_constraint(self):
        # The count alone is not actionable. What makes it act-on-able is that
        # a host answering to a name its one certificate omits is precisely the
        # fault `host split` exists to undo.
        npm_api.warn_on_mixed_bases(["app.example.com", "nas.example.org"], "Host 12")

        self.assertPrinted("one nginx server block with one certificate")
        self.assertPrinted("host split")

    def test_the_bases_come_back_sorted(self):
        bases = npm_api.warn_on_mixed_bases(
            ["nas.example.org", "app.example.com", "shop.internal.lan"], "Host 12")

        self.assertEqual(bases, ["example.com", "example.org", "internal.lan"])

    def test_every_base_is_named_not_just_the_first_two(self):
        npm_api.warn_on_mixed_bases(
            ["nas.example.org", "app.example.com", "shop.internal.lan"], "Host 12")

        self.assertPrinted("3 unrelated base domains")
        self.assertPrinted("example.com, example.org, internal.lan")

    def test_repeated_bases_collapse_to_one(self):
        bases = npm_api.warn_on_mixed_bases(
            ["app.example.com", "api.example.com", "example.com"], "Host 12")

        self.assertEqual(bases, ["example.com"])

    def test_domains_with_no_usable_base_are_dropped(self):
        # A bare label has no base to compare, and counting it as one of its
        # own would fire the warning on every host that happens to carry an
        # internal short name.
        bases = npm_api.warn_on_mixed_bases(
            ["localhost", "app.example.com", "api.example.com"], "Host 12")

        self.assertEqual(bases, ["example.com"])
        self.assertEqual(self.console.text, "")

    def test_an_unusable_domain_beside_two_real_bases_is_not_named(self):
        bases = npm_api.warn_on_mixed_bases(
            ["localhost", "app.example.com", "nas.example.org"], "Host 12")

        self.assertEqual(bases, ["example.com", "example.org"])
        self.assertPrinted("2 unrelated base domains")
        self.assertNotPrinted("localhost")

    def test_only_unusable_domains_return_nothing_quietly(self):
        self.assertEqual(
            npm_api.warn_on_mixed_bases(["localhost", "intranet"], "Host 12"), [])
        self.assertEqual(self.console.text, "")

    def test_an_empty_list_returns_empty_and_says_nothing(self):
        self.assertEqual(npm_api.warn_on_mixed_bases([], "Host 12"), [])
        self.assertEqual(self.console.text, "")


# =============================================================================
# warn_on_mixed_bases: the merge and clone call sites
# =============================================================================

class _MixedBaseTestCase(_MergeCommandTestCase):
    """Drives merge and clone far enough to see whether the warning fires.

    Both call it only when a certificate is actually being assigned, so each
    case has to be run with a real certificate behind the stub — otherwise the
    absence of a warning proves nothing.
    """

    # Covers both bases used below, so the only warning a mixed-base test can
    # produce is the one under test rather than a coverage complaint.
    _CERT = {"id": 4, "domain_names": ["*.example.com", "*.example.org"],
             "expires_on": _expires_in(timedelta(days=90))}

    _WARNING = "unrelated base domains"

    def _clone(self, client, domains, **overrides):
        options = dict(host_id=12, domains=list(domains), cert=None,
                       forward_host=None, forward_port=None, preview=False, yes=True)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_clone(**options)


class TestHostMergeMixedBases(_MixedBaseTestCase):
    """Merge is where this was found: a host of *.example.org names folded into one
    of *.example.com names, under a single uploaded certificate whose metadata
    was unusable enough that the coverage check could only shrug."""

    def _hosts(self, source_domains):
        return (_merge_host(12, ["app.example.com"], certificate_id=4),
                [_merge_host(13, source_domains)])

    def test_a_union_spanning_two_bases_warns_and_names_both(self):
        target, sources = self._hosts(["nas.example.org"])
        client = self._client(target, sources, certificate=self._CERT)

        self._merge(client, host_ids="13")

        self.assertPrinted(self._WARNING)
        self.assertPrinted("example.com, example.org")

    def test_the_warning_is_about_the_whole_union_not_one_host(self):
        # Neither host on its own spans two bases; only the result does, which
        # is why the check runs on the merged list rather than per source.
        target, sources = self._hosts(["nas.example.org", "printer.example.org"])
        client = self._client(target, sources, certificate=self._CERT)

        self._merge(client, host_ids="13")

        self.assertPrinted("2 unrelated base domains")

    def test_a_union_on_one_base_does_not_warn(self):
        target, sources = self._hosts(["old.example.com"])
        client = self._client(target, sources, certificate=self._CERT)

        self._merge(client, host_ids="13")

        self.assertNotPrinted(self._WARNING)

    def test_the_warning_does_not_block_the_merge(self):
        # Advisory, not a refusal: a multi-SAN certificate covering both bases
        # is a legitimate setup, and only the operator can tell.
        target, sources = self._hosts(["nas.example.org"])
        client = self._client(target, sources, certificate=self._CERT)

        self._merge(client, host_ids="13")

        self.assertEqual(client.kinds, ["delete", "update"])
        self.assertPrinted("Successful: 1")
        self.assertNotPrinted("Failed:")

    def test_cert_none_never_raises_the_question(self):
        # No certificate is being assigned, so there is no single certificate
        # for the names to have to fit inside and nothing to warn about.
        target, sources = self._hosts(["nas.example.org"])
        client = self._client(target, sources)

        self._merge(client, host_ids="13", cert="none")

        self.assertNotPrinted(self._WARNING)
        self.assertEqual(client.kinds, ["delete", "update"])


class TestHostCloneMixedBases(_MixedBaseTestCase):
    """The same question asked of a host that does not exist yet."""

    def _source(self):
        return _merge_host(12, ["app.example.com"], certificate_id=4)

    def test_new_domains_spanning_two_bases_warn(self):
        client = self._client(self._source(), [], certificate=self._CERT)

        self._clone(client, ["shop.example.com", "nas.example.org"])

        self.assertPrinted(self._WARNING)
        self.assertPrinted("example.com, example.org")
        self.assertPrinted("The new host")

    def test_new_domains_on_one_base_do_not_warn(self):
        client = self._client(self._source(), [], certificate=self._CERT)

        self._clone(client, ["shop.example.com", "api.example.com"])

        self.assertNotPrinted(self._WARNING)

    def test_the_warning_is_about_the_new_domains_not_the_sources(self):
        # The clone gets its own server block, so what the host it was copied
        # from answers to has no bearing on whether the new one is coherent.
        client = self._client(_merge_host(12, ["nas.example.org"], certificate_id=4),
                              [], certificate=self._CERT)

        self._clone(client, ["shop.example.com", "api.example.com"])

        self.assertNotPrinted(self._WARNING)

    def test_the_warning_does_not_block_the_clone(self):
        client = self._client(self._source(), [], certificate=self._CERT)

        self._clone(client, ["shop.example.com", "nas.example.org"])

        self.assertPrinted(self._WARNING)
        self.assertEqual(client.recreated_ids, [12])
        self.assertPrinted("Created host")

    def test_cert_none_never_raises_the_question(self):
        client = self._client(self._source(), [])

        self._clone(client, ["shop.example.com", "nas.example.org"], cert="none")

        self.assertNotPrinted(self._WARNING)
        self.assertEqual(client.recreated_ids, [12])


# =============================================================================
# warn_on_idn_domains
# =============================================================================

class _IdnTestCase(_ConsoleTestCase):
    """Resets the once-per-run latch around every test.

    warn_on_idn_domains sets a module global the first time it fires, so
    without this the second test in the file would silently assert against a
    warning the first one had already consumed.
    """

    # münchen.example.com and the punycode spelling of the same name.
    UNICODE = "münchen.example.com"
    PUNYCODE = "xn--mnchen-3ya.example.com"

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, npm_api, "_idn_warning_shown", False)
        npm_api._idn_warning_shown = False


class TestWarnOnIdnDomains(_IdnTestCase):
    """Advisory only, and deliberately not a normalisation.

    Python's built-in idna codec is IDNA 2003 and raises UnicodeError on names
    NPM stores happily — an underscore in a label, a label past 63 characters —
    so encoding on the comparison paths would trade a rare wrong answer for a
    common crash. The warning says plainly that the tool cannot see the
    equivalence, and leaves the check to the operator.
    """

    def test_an_ascii_only_list_says_nothing(self):
        self.assertEqual(
            npm_api.warn_on_idn_domains(["app.example.com", "api.example.com"], "Host 12"),
            [])
        self.assertEqual(self.console.text, "")

    def test_a_non_ascii_domain_warns(self):
        flagged = npm_api.warn_on_idn_domains([self.UNICODE], "Host 12")

        self.assertEqual(flagged, [self.UNICODE])
        self.assertPrinted("internationalised domain name")

    def test_a_punycode_domain_warns(self):
        flagged = npm_api.warn_on_idn_domains([self.PUNYCODE], "Host 12")

        self.assertEqual(flagged, [self.PUNYCODE])
        self.assertPrinted("internationalised domain name")

    def test_a_punycode_label_below_the_first_still_warns(self):
        # app.xn--mnchen-3ya.com has a perfectly ASCII first label, and is
        # exactly the shape the warning exists for.
        self.assertEqual(
            npm_api.warn_on_idn_domains(["app.xn--mnchen-3ya.com"], "Host 12"),
            ["app.xn--mnchen-3ya.com"])
        self.assertPrinted("internationalised domain name")

    def test_a_name_merely_containing_xn_is_not_flagged(self):
        # "xnavier.example.com" and "learn--fast.example.com" are ASCII names
        # that happen to contain the letters; the test is on the label prefix.
        self.assertEqual(
            npm_api.warn_on_idn_domains(
                ["xnavier.example.com", "learn--fast.example.com"], "Host 12"),
            [])
        self.assertEqual(self.console.text, "")

    def test_the_warning_names_its_subject(self):
        npm_api.warn_on_idn_domains([self.UNICODE], "The new host")
        self.assertPrinted("The new host")

    def test_the_warning_says_the_comparison_is_plain_text(self):
        # The count alone is not actionable. What makes it act-on-able is
        # naming the limitation: this tool will not notice the two spellings
        # are one name, so the operator has to.
        npm_api.warn_on_idn_domains([self.UNICODE], "Host 12")

        self.assertPrinted("compares domains as plain text")
        self.assertPrinted("spelled both ways")

    def test_the_warning_names_the_domains_that_triggered_it(self):
        npm_api.warn_on_idn_domains(
            ["app.example.com", self.PUNYCODE], "Host 12")

        self.assertPrinted(self.PUNYCODE)

    def test_only_the_triggering_domains_are_named(self):
        npm_api.warn_on_idn_domains(
            ["app.example.com", self.PUNYCODE], "Host 12")

        # The positive half first: assertNotPrinted alone would pass just as
        # happily against a warning that never fired at all.
        self.assertPrinted(self.PUNYCODE)
        self.assertNotPrinted("app.example.com")

    def test_both_spellings_of_one_name_are_returned_as_two(self):
        # The whole point: this tool sees two unrelated names here, and says so
        # rather than folding them.
        self.assertEqual(
            npm_api.warn_on_idn_domains([self.UNICODE, self.PUNYCODE], "Host 12"),
            [self.UNICODE, self.PUNYCODE])

    def test_it_warns_once_per_run(self):
        npm_api.warn_on_idn_domains([self.UNICODE], "Host 12")
        first = self.console.text

        npm_api.warn_on_idn_domains([self.PUNYCODE], "Host 13")

        # Pinned non-empty: comparing "what was printed" before and after is
        # satisfied by printing nothing both times, which is the one outcome
        # this test must not accept.
        self.assertIn("internationalised domain name", first)
        self.assertEqual(self.console.text, first)

    def test_the_second_call_still_reports_what_it_found(self):
        # Silent, but not a lie: a caller that branches on the return value
        # must not start seeing "no IDNs here" once the latch is set.
        npm_api.warn_on_idn_domains([self.UNICODE], "Host 12")

        self.assertEqual(
            npm_api.warn_on_idn_domains([self.PUNYCODE], "Host 13"),
            [self.PUNYCODE])

    def test_an_ascii_first_call_does_not_spend_the_one_warning(self):
        npm_api.warn_on_idn_domains(["app.example.com"], "Host 12")
        npm_api.warn_on_idn_domains([self.UNICODE], "Host 13")

        self.assertPrinted("internationalised domain name")

    def test_an_empty_list_returns_empty_and_says_nothing(self):
        self.assertEqual(npm_api.warn_on_idn_domains([], "Host 12"), [])
        self.assertEqual(self.console.text, "")

    def test_the_domains_are_rendered_through_the_display_helper(self):
        # A name can be both non-ASCII and invisible; the warning about the
        # first must not itself hide the second.
        npm_api.warn_on_idn_domains(["münchen\u200b.example.com"], "Host 12")

        self.assertPrinted("\\u200b")


class TestIdnWarningCallSites(_MixedBaseTestCase):
    """Wired into the same commands as warn_on_mixed_bases, plus the bulk
    domain commands that rewrite names."""

    _IDN = "xn--mnchen-3ya.example.com"
    _NOTICE = "internationalised domain name"

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, npm_api, "_idn_warning_shown", False)
        npm_api._idn_warning_shown = False

    def test_clone_warns_on_an_internationalised_new_domain(self):
        client = self._client(_merge_host(12, ["app.example.com"], certificate_id=4),
                              [], certificate=self._CERT)

        self._clone(client, [self._IDN])

        self.assertPrinted(self._NOTICE)

    def test_clone_warns_even_with_no_certificate(self):
        # Unlike the mixed-base warning, this one is not about a certificate
        # having to cover every name — it is about the conflict check having
        # compared them as text, which an HTTP-only host needs just as much.
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._clone(client, [self._IDN], cert="none")

        self.assertPrinted(self._NOTICE)

    def test_clone_stays_quiet_on_ascii_names(self):
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._clone(client, ["shop.example.com"], cert="none")

        self.assertNotPrinted(self._NOTICE)

    def test_merge_warns_when_the_union_contains_one(self):
        client = self._client(_merge_host(12, ["app.example.com"], certificate_id=4),
                              [_merge_host(13, [self._IDN])], certificate=self._CERT)

        self._merge(client, host_ids="13")

        self.assertPrinted(self._NOTICE)

    def test_merge_stays_quiet_on_an_ascii_union(self):
        client = self._client(_merge_host(12, ["app.example.com"], certificate_id=4),
                              [_merge_host(13, ["old.example.com"])],
                              certificate=self._CERT)

        self._merge(client, host_ids="13")

        self.assertNotPrinted(self._NOTICE)

    def test_the_warning_does_not_block_the_clone(self):
        # Advisory. Refusing would make the tool unusable against an estate
        # that legitimately carries one.
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._clone(client, [self._IDN], cert="none")

        self.assertPrinted(self._NOTICE)
        self.assertEqual(client.recreated_ids, [12])


class TestIdnWarningInBulkDomainCommands(_ConsoleTestCase):
    """The three bulk commands that rewrite the stored names."""

    _NOTICE = "internationalised domain name"

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, npm_api, "_idn_warning_shown", False)
        npm_api._idn_warning_shown = False

    def _run(self, call, hosts):
        client = _BulkDomainClient(hosts)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            call()
        return client

    def test_bulk_add_domain_warns_when_a_selected_host_carries_one(self):
        self._run(
            lambda: npm_api.host_bulk_add_domain(
                new_domain="example.net", host_ids="12", pattern=None,
                preview=False, yes=True, interactive=False),
            [{"id": 12, "domain_names": ["app.xn--mnchen-3ya.com"]}])

        self.assertPrinted(self._NOTICE)

    def test_bulk_add_domain_warns_when_the_new_base_is_one(self):
        self._run(
            lambda: npm_api.host_bulk_add_domain(
                new_domain="xn--mnchen-3ya.com", host_ids="12", pattern=None,
                preview=False, yes=True, interactive=False),
            [{"id": 12, "domain_names": ["app.example.com"]}])

        self.assertPrinted(self._NOTICE)

    def test_bulk_remove_domain_warns(self):
        self._run(
            lambda: npm_api.host_bulk_remove_domain(
                domain_pattern="example.net", host_ids="12", pattern=None,
                preview=False, yes=True, interactive=False),
            [{"id": 12, "domain_names": ["app.xn--mnchen-3ya.com",
                                         "app.example.net"]}])

        self.assertPrinted(self._NOTICE)

    def test_bulk_replace_domain_warns_on_the_name_it_writes(self):
        self._run(
            lambda: npm_api.host_bulk_replace_domain(
                old_domain="example.com", new_domain="xn--mnchen-3ya.com",
                host_ids="12", pattern=None, preview=False, yes=True,
                interactive=False),
            [{"id": 12, "domain_names": ["app.example.com"]}])

        self.assertPrinted(self._NOTICE)

    def test_an_all_ascii_bulk_run_stays_quiet(self):
        self._run(
            lambda: npm_api.host_bulk_add_domain(
                new_domain="example.net", host_ids="12", pattern=None,
                preview=False, yes=True, interactive=False),
            [{"id": 12, "domain_names": ["app.example.com"]}])

        self.assertNotPrinted(self._NOTICE)


# =============================================================================
# display_domain / display_domains
# =============================================================================

# A zero-width space. Invisible on every terminal, and Rich passes it straight
# through, so app.exam<ZWSP>ple.com and app.example.com look identical while
# being different strings — and only one of them matches the certificate.
#
# Written as an escape rather than pasted in literally, here and everywhere
# below: a test file about invisible characters is the last place that should
# contain any. Python turns "\u200b" into the real character at compile time,
# so what reaches the code under test is the genuine article.
_ZWSP = "\u200b"
_HIDDEN_DOMAIN = f"app.exam{_ZWSP}ple.com"


class TestDisplayDomain(unittest.TestCase):
    """Display only. The stored value is never touched — see
    TestHiddenCharactersAreShownButNotSent below for the half of that
    contract which matters."""

    def test_an_ordinary_domain_is_unchanged(self):
        # The overwhelmingly common case has to stay byte-identical, or every
        # existing assertion about output becomes a lie.
        for domain in ("app.example.com", "example.com", "*.example.com",
                       "xn--mnchen-3ya.example.com", "host_1.example.com"):
            with self.subTest(domain=domain):
                self.assertEqual(npm_api.display_domain(domain), domain)

    def test_a_zero_width_space_is_spelled_out(self):
        self.assertEqual(npm_api.display_domain(_HIDDEN_DOMAIN),
                         "app.exam\\u200bple.com")

    def test_a_plain_ascii_space_is_left_alone(self):
        # Category Zs, like the exotic separators, but the one separator a
        # human can already see. Escaping it would make every "(no domains)"
        # and every joined list unreadable.
        self.assertEqual(npm_api.display_domain("app example.com"),
                         "app example.com")

    def test_bidirectional_overrides_are_spelled_out(self):
        # U+202E flips the rendering of everything after it, so a name can be
        # made to display as a completely different one.
        for char, expected in (("\u202e", "\\u202e"), ("\u202d", "\\u202d"),
                               ("\u2066", "\\u2066"), ("\u200f", "\\u200f")):
            with self.subTest(char=expected):
                self.assertEqual(npm_api.display_domain(f"a{char}b"),
                                 f"a{expected}b")

    def test_control_characters_are_spelled_out(self):
        for char, expected in (("\n", "\\u000a"), ("\r", "\\u000d"),
                               ("\t", "\\u0009"), ("\x00", "\\u0000")):
            with self.subTest(char=expected):
                self.assertEqual(npm_api.display_domain(f"a{char}b"),
                                 f"a{expected}b")

    def test_exotic_separators_are_spelled_out(self):
        for char, expected in (("\u00a0", "\\u00a0"), ("\u2028", "\\u2028"),
                               ("\u2029", "\\u2029"), ("\u3000", "\\u3000")):
            with self.subTest(char=expected):
                self.assertEqual(npm_api.display_domain(f"a{char}b"),
                                 f"a{expected}b")

    def test_a_soft_hyphen_is_spelled_out(self):
        # Cf, like the zero-width space, and just as invisible.
        self.assertEqual(npm_api.display_domain("a\u00adb"), "a\\u00adb")

    def test_visible_non_ascii_is_left_alone(self):
        # münchen.example.com is a real name a human can read. Escaping it
        # would bury the IDN warning's own output in backslashes and make a
        # legitimate estate unreadable; the letters are not the hazard.
        self.assertEqual(npm_api.display_domain("münchen.example.com"),
                         "münchen.example.com")

    def test_square_brackets_cannot_be_read_as_rich_markup(self):
        # Not hypothetical: an unescaped "a[b]c" already renders as "ac", and
        # "[/]" raises MarkupError. The escape is a backslash Rich consumes,
        # so what reaches the terminal is the bracket.
        self.assertEqual(npm_api.display_domain("a[b]c"), "a\\[b]c")

    def test_a_non_string_does_not_raise(self):
        # domain_names comes from NPM's JSON and is not guaranteed to hold
        # strings; a display helper is the wrong place to discover that.
        self.assertEqual(npm_api.display_domain(12), "12")


class TestDisplayDomains(unittest.TestCase):
    """The comma-joining companion, which the display sites call."""

    def test_a_list_is_comma_joined(self):
        self.assertEqual(
            npm_api.display_domains(["app.example.com", "api.example.com"]),
            "app.example.com, api.example.com")

    def test_every_element_is_escaped_not_just_the_first(self):
        self.assertEqual(
            npm_api.display_domains(["app.example.com", _HIDDEN_DOMAIN]),
            "app.example.com, app.exam\\u200bple.com")

    def test_an_empty_list_is_empty_by_default(self):
        self.assertEqual(npm_api.display_domains([]), "")

    def test_none_is_treated_as_empty(self):
        self.assertEqual(npm_api.display_domains(None), "")

    def test_the_empty_placeholder_is_used_when_given(self):
        # The call sites that previously read `", ".join(...) or "(none)"`.
        self.assertEqual(npm_api.display_domains([], "(no domains)"),
                         "(no domains)")

    def test_the_placeholder_is_not_used_for_a_non_empty_list(self):
        self.assertEqual(npm_api.display_domains(["app.example.com"], "(none)"),
                         "app.example.com")


class _RenderedOutputTestCase(_ConsoleTestCase):
    """Renders out_console for real rather than recording its markup.

    _RecordingConsole deliberately keeps the markup string instead of
    rendering it, which is the right trade everywhere else — but the whole
    question here is what survives Rich, so an assertion against unrendered
    markup would pass whether or not Rich then swallowed the text.
    """

    def setUp(self):
        super().setUp()
        self.stdout = io.StringIO()
        # Wide enough that the table cannot wrap a domain mid-name and turn a
        # content assertion into a layout assertion.
        rendered = npm_api.Console(file=self.stdout, width=200,
                                   force_terminal=False, no_color=True)
        patcher = mock.patch.object(npm_api, "out_console", rendered)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def rendered(self):
        return self.stdout.getvalue()

    def run_command(self, client, call):
        with mock.patch.object(npm_api, "get_client", lambda: client):
            call()
        return self.rendered


class TestHiddenCharactersAreShownButNotSent(_RenderedOutputTestCase):
    """The point of the whole change, stated as one property.

    A domain pasted out of a ticket carries a stray zero-width space. `host
    list` has to show it, or the operator spends an afternoon on why a
    certificate that plainly covers app.example.com does not match. NPM has to
    receive the name exactly as stored, or the display helper has quietly
    become a data migration.
    """

    def _host(self):
        return {"id": 12, "domain_names": [_HIDDEN_DOMAIN],
                "enabled": True, "forward_host": "10.0.0.5",
                "forward_port": 8080, "forward_scheme": "http"}

    def test_host_list_shows_the_hidden_character(self):
        text = self.run_command(_HostsClient([self._host()]),
                                lambda: npm_api.host_list(as_json=False))

        self.assertIn("\\u200b", text)

    def test_host_list_does_not_pass_the_raw_character_through(self):
        # The failure this replaces: Rich rendered the zero-width space
        # untouched, so the terminal showed "app.example.com" and the operator
        # had no way to see the difference.
        text = self.run_command(_HostsClient([self._host()]),
                                lambda: npm_api.host_list(as_json=False))

        self.assertNotIn(_ZWSP, text)

    def test_the_stored_name_reaches_npm_unescaped(self):
        # Same host, same character, through a command that writes. What goes
        # on the wire must be the byte NPM already holds.
        client = _BulkDomainClient([self._host()])

        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_add_domain(
                new_domain="example.net", host_ids="12", pattern=None,
                preview=True, yes=True, interactive=False)

        written = client.written_domains[0]
        self.assertIn(_HIDDEN_DOMAIN, written)
        self.assertNotIn("app.exam\\u200bple.com", written)

    def test_the_escaped_spelling_is_never_written(self):
        # Belt and braces on the line above: no name in the payload may carry
        # a literal backslash that the operator would then have to unpick.
        #
        # Asserted on the strings rather than on json.dumps(...), which
        # escapes non-ASCII to "\\u200b" itself under its default
        # ensure_ascii=True and would fail this whatever the code did.
        client = _BulkDomainClient([self._host()])

        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_add_domain(
                new_domain="example.net", host_ids="12", pattern=None,
                preview=True, yes=True, interactive=False)

        for domain in client.written_domains[0]:
            with self.subTest(domain=domain):
                self.assertNotIn("\\", domain)

    def test_json_output_is_not_escaped_either(self):
        # `host list --json | jq` is a data path, not a display one. Escaping
        # here would corrupt every scripted round trip.
        client = _HostsClient([self._host()])
        buffer = io.StringIO()

        with mock.patch.object(npm_api, "get_client", lambda: client), \
                mock.patch.object(sys, "stdout", buffer):
            npm_api.host_list(as_json=True)

        self.assertEqual(json.loads(buffer.getvalue())[0]["domain_names"],
                         [_HIDDEN_DOMAIN])


class TestRenderedDomainsAtTheDisplaySites(_RenderedOutputTestCase):
    """The stdout sites. The stderr ones are covered against the recording
    console, which keeps the markup display_domain produced."""

    def _host(self, domains):
        return {"id": 12, "domain_names": domains, "enabled": True,
                "forward_host": "10.0.0.5", "forward_port": 8080,
                "forward_scheme": "http"}

    def test_host_show_escapes(self):
        client = _HostsClient([self._host([_HIDDEN_DOMAIN])])
        client.get_host = lambda host_id: client.hosts[0]

        text = self.run_command(
            client, lambda: npm_api.host_show(host_id=12, as_json=False))

        self.assertIn("\\u200b", text)

    def test_host_search_escapes(self):
        client = _HostsClient([self._host([_HIDDEN_DOMAIN])])
        client.search_hosts = lambda search: client.hosts

        text = self.run_command(
            client, lambda: npm_api.host_search(search="app", as_json=False))

        self.assertIn("\\u200b", text)

    def test_host_list_does_not_swallow_a_bracketed_name(self):
        # Rich reads "[b]" as a bold tag. Before the escaping, a name
        # containing one rendered with the tag removed — the display site
        # inventing a domain that does not exist.
        client = _HostsClient([self._host(["a[b]c.example.com"])])

        text = self.run_command(client, lambda: npm_api.host_list(as_json=False))

        self.assertIn("a[b]c.example.com", text)

    def test_host_list_survives_a_name_that_is_a_closing_tag(self):
        # "[/]" with nothing open raises MarkupError, which would take out
        # `host list` for the whole estate over one bad name.
        client = _HostsClient([self._host(["[/].example.com"])])

        text = self.run_command(client, lambda: npm_api.host_list(as_json=False))

        self.assertIn("[/].example.com", text)

    def test_an_ordinary_estate_renders_exactly_as_before(self):
        client = _HostsClient([self._host(["app.example.com", "api.example.com"])])

        text = self.run_command(client, lambda: npm_api.host_list(as_json=False))

        self.assertIn("app.example.com, api.example.com", text)
        self.assertNotIn("\\", text)


class TestDomainEchoesOnTheDiagnosticConsole(_ConsoleTestCase):
    """The stderr sites: previews and the per-host success lines."""

    def test_a_bulk_add_success_line_escapes_the_new_name(self):
        client = _BulkDomainClient(
            [{"id": 12, "domain_names": [f"app{_ZWSP}.example.com"]}])

        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_add_domain(
                new_domain="example.net", host_ids="12", pattern=None,
                preview=True, yes=True, interactive=False)

        self.assertPrinted("\\u200b")

    def test_the_delete_confirmation_escapes(self):
        client = _HostsClient([{"id": 12, "domain_names": [_HIDDEN_DOMAIN]}])
        client.get_host = lambda host_id: client.hosts[0]
        client.delete_host = lambda host_id: True

        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_delete(host_id=12, yes=True)

        self.assertPrinted("\\u200b")


# =============================================================================
# validate_certificate_assignment: labelling
# =============================================================================

class TestCertificateStatusLabelling(_ConsoleTestCase):
    """What the status word is claiming to be about."""

    def test_the_status_label_is_scoped_to_the_expiry(self):
        # A bare green "✅ VALID" sits directly above the coverage warnings and
        # reads as an endorsement of the whole assignment, when all it means is
        # that the certificate has not expired. It said nothing about whether
        # the certificate covers the domains it is being pointed at.
        client = _CertLookupClient({"id": 4, "domain_names": ["*.example.com"],
                                    "expires_on": _expires_in(timedelta(days=90))})

        npm_api.validate_certificate_assignment(
            client, 4, [{"id": 12, "domain_names": ["nas.example.org"]}])

        # The markup is kept rather than rendered here, so the assertion pins
        # the two as adjacent: the word has to qualify the label, not sit
        # somewhere else on the line.
        self.assertPrinted("expiry: [green]✅ VALID[/green]")
        self.assertPrinted("not covered")


# =============================================================================
# host split & host clone: shared doubles
# =============================================================================

def _live_cert(cert_id=7, domains=("*.internal.lan",)):
    """A certificate that exists and is nowhere near expiry.

    Given a domain list that covers the names it is handed below, so that the
    only thing a coverage warning can mean in these tests is a real one.
    """
    return {"id": cert_id, "domain_names": list(domains),
            "expires_on": _expires_in(timedelta(days=90))}


class _SplitClient(_MergeClient):
    """_MergeClient, plus the overrides each create carried.

    Merge calls create_host_from only to undo a delete, so it passes no
    overrides and the base class records the source ID alone. Split's create is
    the point of the command — the domains being moved and the certificate the
    new host is given arrive in that argument and nowhere else — so they are
    recorded too, on the same ordered list as everything else.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created_overrides = []

    def create_host_from(self, source, overrides):
        # Recorded before delegating, because super() raises for the rollback
        # tests and "the create was attempted, carrying this" is the fact those
        # tests are about.
        self.created_overrides.append(overrides)
        return super().create_host_from(source, overrides)


class _SplitCommandTestCase(_MergeCommandTestCase):
    """Runs host_split end to end against a stub.

    Every argument is passed explicitly for the same reason merge's driver does
    it: the defaults on a Typer command's signature are typer.OptionInfo
    objects, so an omitted argument arrives truthy and the run under test would
    not be the one the test describes. --cert is a required option on split, so
    it has no default to get wrong; it is spelled out here all the same.
    """

    def _client(self, target, sources, **kwargs):
        return _SplitClient(target, sources, self.workdir, **kwargs)

    def _split(self, client, match, **overrides):
        options = dict(match=match, cert="none", host_ids=None, pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_split(**options)

    def _split_expecting_exit(self, client, match, **overrides):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._split(client, match, **overrides)
        return caught.exception


class _CloneCommandTestCase(_MixedBaseTestCase):
    """host_clone's driver already exists on _MixedBaseTestCase.

    Inherited rather than repeated; this subclass exists to add the
    expecting-an-exit variant and to give the classes below a name that says
    what they are driving.
    """

    def _clone_expecting_exit(self, client, domains, **overrides):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._clone(client, domains, **overrides)
        return caught.exception


# =============================================================================
# host split: the certificate that is not there
# =============================================================================

class TestHostSplitMissingCertificate(_SplitCommandTestCase):
    """Split must not create hosts pointed at a certificate NPM does not have.

    NPM wraps the whole `listen 443 ssl` block in a conditional on the linked
    certificate, so such a host is rendered with no TLS listener at all and
    reports nothing wrong — the domains simply stop answering on 443. All three
    of merge, clone and split ignored validate_certificate_assignment's return
    value until 86380ad; only merge got a test out of it.
    """

    def _source(self):
        return _merge_host(12, ["app.example.com", "api.internal.lan"])

    def test_a_certificate_that_does_not_exist_stops_the_split(self):
        client = self._client(self._source(), [])

        exit_exception = self._split_expecting_exit(client, "*.internal.lan",
                                                    cert="99", host_ids="12")

        self.assertEqual(exit_exception.exit_code, 1)
        # Nothing trimmed and nothing created. The refusal has to land before
        # the first write, because the first write is the one that takes the
        # moving domains off the source: stopping after it and before the
        # create would leave them served by nothing at all.
        self.assertEqual(client.calls, [])
        self.assertEqual(client.created_overrides, [])
        self.assertPrinted("Certificate 99 does not exist")

    def test_the_refusal_explains_what_would_have_happened(self):
        client = self._client(self._source(), [])

        self._split_expecting_exit(client, "*.internal.lan", cert="99", host_ids="12")

        self.assertPrinted("Refusing to split")
        self.assertPrinted("no TLS listener")
        # Both ways out are named: a real certificate, or an explicit decision
        # to create the new hosts HTTP-only.
        self.assertPrinted("--cert none")

    def test_the_lookup_happens_once_however_many_hosts_are_being_split(self):
        client = self._client(_merge_host(12, ["app.example.com", "api.internal.lan"]),
                              [_merge_host(13, ["shop.example.com", "db.internal.lan"])])

        self._split_expecting_exit(client, "*.internal.lan", cert="99",
                                   host_ids="12,13")

        self.assertEqual(client.cert_lookups, [99])
        self.assertEqual(client.calls, [])

    def test_cert_none_needs_no_certificate_lookup(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", cert="none", host_ids="12")

        self.assertEqual(client.cert_lookups, [])
        self.assertEqual(client.kinds, ["update", "create"])

    def test_a_certificate_that_exists_lets_the_split_through(self):
        client = self._client(self._source(), [], certificate=_live_cert())

        self._split(client, "*.internal.lan", cert="7", host_ids="12")

        self.assertEqual(client.cert_lookups, [7])
        self.assertEqual(client.kinds, ["update", "create"])


# =============================================================================
# host clone: the certificate that is not there
# =============================================================================

class TestHostCloneMissingCertificate(_CloneCommandTestCase):
    """The same refusal on the command that creates a host from scratch.

    Clone has two ways to arrive at a certificate ID — named with --cert, or
    inherited from the host being copied — and both have to be checked. The
    inherited one is the likelier failure in practice: deleting a certificate
    in NPM's UI leaves every host that referenced it pointing at the dead ID,
    and cloning one of those hosts propagates the fault to a second host.
    """

    def test_an_explicit_certificate_that_does_not_exist_stops_the_clone(self):
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        exit_exception = self._clone_expecting_exit(
            client, ["shop.example.com"], cert="99")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Certificate 99 does not exist")

    def test_an_inherited_certificate_that_is_gone_also_stops_the_clone(self):
        # No --cert at all: the ID comes off the source host, which NPM is
        # perfectly happy to keep serving a stale reference on.
        client = self._client(_merge_host(12, ["app.example.com"], certificate_id=4), [])

        exit_exception = self._clone_expecting_exit(client, ["shop.example.com"])

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.cert_lookups, [4])
        self.assertPrinted("Certificate 4 does not exist")

    def test_the_refusal_explains_what_would_have_happened(self):
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._clone_expecting_exit(client, ["shop.example.com"], cert="99")

        self.assertPrinted("Refusing to clone")
        self.assertPrinted("no TLS listener")
        self.assertPrinted("--cert none")

    def test_cert_none_needs_no_certificate_lookup(self):
        # Even when the source carries one: --cert none is an override, so the
        # source's dead reference is never followed.
        client = self._client(_merge_host(12, ["app.example.com"], certificate_id=4), [])

        self._clone(client, ["shop.example.com"], cert="none")

        self.assertEqual(client.cert_lookups, [])
        self.assertEqual(client.recreated_ids, [12])

    def test_a_source_with_no_certificate_needs_no_lookup_either(self):
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._clone(client, ["shop.example.com"])

        self.assertEqual(client.cert_lookups, [])
        self.assertEqual(client.recreated_ids, [12])

    def test_an_inherited_certificate_that_exists_lets_the_clone_through(self):
        client = self._client(_merge_host(12, ["app.example.com"], certificate_id=7),
                              [], certificate=_live_cert(domains=("*.example.com",)))

        self._clone(client, ["shop.example.com"])

        self.assertEqual(client.cert_lookups, [7])
        self.assertEqual(client.recreated_ids, [12])
        self.assertPrinted("Created host")


# =============================================================================
# host split: rollback
# =============================================================================

class TestHostSplitRollback(_SplitCommandTestCase):
    """What becomes of the source when the new host cannot be created.

    Split frees the moving domains off the source before creating the host that
    is to take them, because NPM refuses to let two hosts hold the same name.
    That leaves a window in which those domains belong to nobody, and a failed
    create has to close it by writing the original list back. The pre-split
    snapshot (TestHostSplitSnapshot) is the floor under a rollback that cannot
    run at all; this is the ordinary case, where it can.
    """

    def _source(self):
        return _merge_host(12, ["app.example.com", "api.internal.lan"])

    _ORIGINAL = ["app.example.com", "api.internal.lan"]
    _TRIMMED = ["app.example.com"]

    def test_a_failed_create_puts_the_original_domains_back(self):
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"))

        exit_exception = self._split_expecting_exit(client, "*.internal.lan",
                                                    host_ids="12")

        self.assertEqual(exit_exception.exit_code, 1)
        # Read as a sequence: trim, attempt, undo. The third call is the whole
        # point — without it the host stays trimmed and api.internal.lan is
        # served by nothing, which is worse than never having run the command.
        self.assertEqual(client.calls, [
            ("update", 12, {"domain_names": self._TRIMMED}),
            ("create", 12),
            ("update", 12, {"domain_names": self._ORIGINAL}),
        ])
        self.assertPrinted("Host 12 restored")

    def test_the_failure_is_attributed_to_the_host_it_happened_on(self):
        # A batch split reports per host, so the ID has to be on the line: the
        # summary at the end gives a count and nothing else.
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"))

        self._split_expecting_exit(client, "*.internal.lan", host_ids="12")

        self.assertPrinted("Host 12: create failed")

    def test_the_failure_carries_the_reason_npm_gave(self):
        # Regression. Four HTTPError reports in split and clone interpolated
        # the exception directly, so NPM's "Domain already in use" was replaced
        # by requests' "400 Client Error: Bad Request for url: http://.../api/
        # nginx/proxy-hosts" — the URL the operator already knows, and none of
        # the reason. Every other HTTPError report in the tool goes through
        # format_http_error; these two commands were added together and missed
        # the convention.
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"))

        self._split_expecting_exit(client, "*.internal.lan", host_ids="12")

        self.assertPrinted("Create refused")
        self.assertNotPrinted("Client Error")

    def test_a_failed_trim_never_reaches_the_create(self):
        # Nothing has moved yet, so there is nothing to undo: the host still
        # holds every domain it started with.
        client = self._client(self._source(), [], update_fail_calls={1})

        exit_exception = self._split_expecting_exit(client, "*.internal.lan",
                                                    host_ids="12")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.kinds, ["update"])
        self.assertEqual(client.created_overrides, [])
        self.assertPrinted("could not trim source")

    def test_a_failed_rollback_is_announced_as_one(self):
        # The trim succeeded (update 1) and the restoring write did not
        # (update 2), so the host is now short the domains that were to move
        # and nothing was created to serve them.
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"),
                              update_fail_calls={2})

        self._split_expecting_exit(client, "*.internal.lan", host_ids="12")

        self.assertPrinted("ROLLBACK FAILED")
        self.assertNotPrinted("Host 12 restored")

    def test_a_failed_rollback_carries_the_reason_npm_gave(self):
        # The most important of the four lines that were interpolating the
        # exception directly. By this point the rollback has already failed and
        # the domain list has to be repaired by hand, so why NPM refused the
        # restoring write is the single most useful thing on screen — and a
        # bare repr replaces it with the URL the operator already knows.
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"),
                              error=_npm_http_error(message="Restore refused"),
                              update_fail_calls={2})

        self._split_expecting_exit(client, "*.internal.lan", host_ids="12")

        self.assertPrinted("ROLLBACK FAILED: HTTP 400: Restore refused")
        self.assertNotPrinted("Client Error")

    def test_a_failed_rollback_prints_the_original_domain_list(self):
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"),
                              update_fail_calls={2})

        self._split_expecting_exit(client, "*.internal.lan", host_ids="12")

        # It has to be printed in full because NPM has already been told the
        # host holds the shorter list. The pre-split snapshot has the same
        # record, but an operator repairing this by hand is looking at the
        # terminal, not hunting for a file.
        self.assertPrinted(f"it originally held {self._ORIGINAL}")

    def test_a_failed_rollback_also_prints_what_the_host_holds_now(self):
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"),
                              update_fail_calls={2})

        self._split_expecting_exit(client, "*.internal.lan", host_ids="12")

        # Both halves, so the difference between them is the repair.
        self.assertPrinted(f"Host 12 now holds {self._TRIMMED}")

    def test_a_failed_host_does_not_abandon_the_ones_after_it(self):
        # The first host's trim is rejected; the second must still be split.
        # A bulk command that stopped at the first failure would leave a batch
        # half-done with no indication of where it got to.
        client = self._client(self._source(),
                              [_merge_host(13, ["shop.example.com", "db.internal.lan"])],
                              update_fail_calls={1})

        exit_exception = self._split_expecting_exit(client, "*.internal.lan",
                                                    host_ids="12,13")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.kinds, ["update", "update", "create"])
        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")

    def test_a_rolled_back_host_is_not_counted_as_a_success(self):
        client = self._client(self._source(), [],
                              restore_error=_npm_http_error(message="Create refused"))

        self._split_expecting_exit(client, "*.internal.lan", host_ids="12")

        self.assertPrinted("Successful: 0")
        self.assertPrinted("Failed: 1")


# =============================================================================
# host split: the hosts it steps over
# =============================================================================

class TestHostSplitSkips(_SplitCommandTestCase):
    """Which hosts a split declines to touch, and why that is not a failure.

    A split is normally aimed at a batch picked out by --pattern, so a host the
    glob cannot usefully split is reported and stepped over rather than taking
    the run down with it. Every skip is decided before the first write, so a
    skipped host is never left half-processed.
    """

    def test_a_host_with_one_domain_is_skipped(self):
        # There is no split to make: the moved half and the kept half cannot
        # both be non-empty.
        client = self._client(_merge_host(12, ["api.internal.lan"]), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.calls, [])
        self.assertPrinted("needs at least two")

    def test_a_host_with_no_domains_at_all_is_skipped(self):
        client = self._client(_merge_host(12, []), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.calls, [])
        self.assertPrinted("(no domains)")

    def test_a_glob_matching_nothing_is_skipped(self):
        client = self._client(_merge_host(12, ["app.example.com", "www.example.com"]), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.calls, [])
        self.assertPrinted("nothing matches")

    def test_a_glob_matching_every_domain_is_skipped(self):
        # Moving all of them would leave the source holding an empty domain
        # list — a host NPM keeps and nginx can route nothing to. Splitting a
        # whole host onto a new one is a certificate change, not a split.
        client = self._client(_merge_host(12, ["api.internal.lan", "db.internal.lan"]), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.calls, [])
        self.assertPrinted("would leave the source empty")

    def test_a_moving_domain_already_on_another_host_is_skipped(self):
        # NPM rejects duplicates, so the create would fail and the trim would
        # have to be rolled back. Cheaper and safer to notice beforehand.
        client = self._client(_merge_host(12, ["app.example.com", "api.internal.lan"]),
                              [_merge_host(13, ["api.internal.lan"])])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.calls, [])
        self.assertPrinted("already on host 13")

    def test_every_host_skipped_writes_nothing_and_says_so(self):
        client = self._client(_merge_host(12, ["api.internal.lan"]), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.calls, [])
        self.assertPrinted("Nothing to split")

    def test_no_hosts_selected_writes_nothing_and_exits_1(self):
        # Distinct from "Nothing to split" above, which is a real host that had
        # nothing to move — a decision the command made about a host it saw.
        # This one is the operator naming a host that is not there, and it now
        # fails the run rather than reporting a clean pass over zero hosts.
        client = self._client(_merge_host(12, ["app.example.com", "api.internal.lan"]), [])

        exit_exc = self._split_expecting_exit(client, "*.internal.lan", host_ids="99")

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("matched no hosts")

    def test_a_skip_beside_a_success_is_reported_without_failing_the_run(self):
        # The property that matters: a skipped host is a decision the command
        # made about an input it was given, not an operation that went wrong,
        # so it must not turn a scheduled batch red.
        client = self._client(_merge_host(12, ["app.example.com", "api.internal.lan"]),
                              [_merge_host(13, ["only.example.com"])])

        self._split(client, "*.internal.lan", host_ids="12,13")

        self.assertEqual(client.kinds, ["update", "create"])
        self.assertPrinted("Skipped: 1")
        self.assertPrinted("Successful: 1")
        self.assertNotPrinted("Failed:")

    def test_a_skip_beside_a_failure_still_exits_1(self):
        client = self._client(_merge_host(12, ["app.example.com", "api.internal.lan"]),
                              [_merge_host(13, ["only.example.com"])],
                              update_fail_calls={1})

        exit_exception = self._split_expecting_exit(client, "*.internal.lan",
                                                    host_ids="12,13")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertPrinted("Skipped: 1")
        self.assertPrinted("Failed: 1")


# =============================================================================
# host split: a clean run
# =============================================================================

class TestHostSplitCleanRun(_SplitCommandTestCase):
    """What a successful split actually writes, and in what order."""

    def _source(self, **overrides):
        return _merge_host(12, ["app.example.com", "api.internal.lan"], **overrides)

    def test_the_source_is_trimmed_before_the_new_host_is_created(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.kinds, ["update", "create"])

    def test_the_source_keeps_exactly_the_domains_that_did_not_match(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.updates, [(12, {"domain_names": ["app.example.com"]})])

    def test_only_the_domain_list_is_written_to_the_source(self):
        # update_host carries every other field over untouched, so naming one
        # key here is what keeps the source's own certificate, its advanced
        # config and its custom locations where they were.
        client = self._client(self._source(certificate_id=7), [],
                              certificate=_live_cert())

        self._split(client, "*.internal.lan", cert="7", host_ids="12")

        (_, updates), = client.updates
        self.assertEqual(list(updates), ["domain_names"])

    def test_the_new_host_takes_the_moved_domains_and_the_named_certificate(self):
        client = self._client(self._source(), [], certificate=_live_cert())

        self._split(client, "*.internal.lan", cert="7", host_ids="12")

        self.assertEqual(client.created_overrides,
                         [{"domain_names": ["api.internal.lan"], "certificate_id": 7}])

    def test_cert_none_creates_the_new_host_without_one(self):
        # Spelled as an explicit null rather than left out: the new host is
        # built from the source's configuration, which may well carry a
        # certificate that has no business covering the moved names.
        client = self._client(self._source(certificate_id=4), [])

        self._split(client, "*.internal.lan", cert="none", host_ids="12")

        self.assertEqual(client.created_overrides,
                         [{"domain_names": ["api.internal.lan"], "certificate_id": None}])

    def test_the_glob_is_matched_case_insensitively(self):
        # NPM preserves whatever case a domain was typed in, and DNS does not
        # care, so a glob that only matched lowercase would silently skip hosts.
        client = self._client(_merge_host(12, ["app.example.com", "API.Internal.LAN"]), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.created_overrides,
                         [{"domain_names": ["API.Internal.LAN"], "certificate_id": None}])

    def test_each_host_is_trimmed_immediately_before_its_own_create(self):
        # Not "all the trims, then all the creates": a host must not be left
        # open while an unrelated host's create is in flight.
        client = self._client(self._source(),
                              [_merge_host(13, ["shop.example.com", "db.internal.lan"])])

        self._split(client, "*.internal.lan", host_ids="12,13")

        self.assertEqual(client.kinds, ["update", "create", "update", "create"])

    def test_a_clean_run_returns_normally(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertPrinted("Successful: 1")
        self.assertNotPrinted("Failed:")

    def test_the_new_host_id_is_reported(self):
        # NPM assigns it on create, so the terminal is the only place the
        # operator learns what it is.
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertPrinted("Host 12 → new host 101")

    def test_the_preview_names_the_two_halves_before_anything_is_written(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12", preview=True)

        self.assertPrinted("Host Split Preview")
        self.assertPrinted("Total hosts to split")


# =============================================================================
# host split: the snapshot under the trim
# =============================================================================

class TestHostSplitSnapshot(_SplitCommandTestCase):
    """The same floor merge and restore stand on.

    Split's first write frees the moving domains off the source before the host
    that is to take them exists. The in-process rollback closes that window in
    the ordinary case, but it cannot run at all if the process is killed
    between the two calls — and then the original domain list survives nowhere
    but the operator's scrollback. One file, written once for the whole run,
    before the first trim.
    """

    def _source(self, host_id=12, **overrides):
        return _merge_host(host_id, [f"app{host_id}.example.com",
                                     f"api{host_id}.internal.lan"], **overrides)

    def test_a_run_leaves_a_snapshot_on_disk(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        written = list(self.workdir.glob("pre_split_*.json"))
        self.assertEqual(len(written), 1, written)

    def test_the_snapshot_is_owner_readable_only(self):
        # It holds whole host records, and advanced_config routinely carries
        # auth headers and internal hostnames.
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        (written,) = self.workdir.glob("pre_split_*.json")
        assert_owner_only(self, written)

    def test_it_holds_every_source_whole_not_just_its_names(self):
        # Enough to rebuild each source from the file alone: a list of domains
        # would not say where they forwarded or what certificate they carried.
        source = self._source(certificate_id=7, advanced_config="add_header X-A a;")
        client = self._client(source, [], certificate=_live_cert())

        self._split(client, "*.internal.lan", cert="7", host_ids="12")

        (written,) = self.workdir.glob("pre_split_*.json")
        self.assertEqual(json.loads(written.read_text())["sources"], [source])

    def test_one_snapshot_covers_a_whole_batch(self):
        # Not one per host: the point is a single record of the estate as it
        # stood before the run, and twenty files named a second apart would
        # make reconstructing it harder rather than easier.
        client = self._client(self._source(12), [self._source(13), self._source(14)])

        self._split(client, "*.internal.lan", host_ids="12,13,14")

        (written,) = self.workdir.glob("pre_split_*.json")
        self.assertEqual([s["id"] for s in json.loads(written.read_text())["sources"]],
                         [12, 13, 14])

    def test_it_records_only_the_hosts_the_run_will_touch(self):
        # A host skipped for having nothing to move is not being modified, so
        # putting it in the file would misreport what the run was about to do.
        client = self._client(self._source(12), [_merge_host(13, ["shop.example.com",
                                                                  "www.example.com"])])

        self._split(client, "*.internal.lan", host_ids="12,13")

        (written,) = self.workdir.glob("pre_split_*.json")
        self.assertEqual([s["id"] for s in json.loads(written.read_text())["sources"]],
                         [12])

    def test_it_is_written_before_the_first_trim(self):
        # The ordering is the whole guarantee: a snapshot taken after the trim
        # would record the damage rather than the thing being damaged.
        client = self._client(self._source(), [])
        order = []

        def record(config, label, payload):
            order.append(("snapshot", label))
            return Path("/tmp/unused.json")

        with mock.patch.object(npm_api, "write_state_snapshot", side_effect=record):
            with mock.patch.object(client, "update_host",
                                   side_effect=lambda h, u: order.append(("update", h))):
                self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(order[0], ("snapshot", "pre_split"))
        self.assertIn(("update", 12), order)

    def test_nothing_is_written_when_the_snapshot_cannot_be(self):
        # Merge's exact behaviour: no host is trimmed until its configuration
        # is on disk, so a read-only backup directory stops the run rather than
        # quietly removing the safety net.
        client = self._client(self._source(), [])

        with mock.patch.object(npm_api, "write_state_snapshot",
                               side_effect=OSError("Read-only file system")):
            exit_exception = self._split_expecting_exit(client, "*.internal.lan",
                                                        host_ids="12")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Read-only file system")
        self.assertPrinted("Refusing to trim hosts")

    def test_the_path_is_printed_so_the_operator_can_find_it(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        (written,) = self.workdir.glob("pre_split_*.json")
        self.assertPrinted(f"Pre-split snapshot: {written}")

    def test_a_run_with_nothing_to_split_writes_no_snapshot(self):
        # Nothing is about to be destroyed, so there is nothing to record — and
        # a file per no-op run would bury the ones that matter.
        client = self._client(self._source(), [])

        self._split(client, "*.nothing.lan", host_ids="12")

        self.assertEqual(list(self.workdir.glob("pre_split_*.json")), [])

    def test_the_snapshot_is_taken_after_the_confirmation_not_before(self):
        # A cancelled run must leave no trace: confirm_bulk raises Exit(0), and
        # anything written ahead of it would accumulate on every abandoned
        # attempt.
        client = self._client(self._source(), [])

        with mock.patch.object(npm_api.typer, "confirm", return_value=False):
            with self.assertRaises(npm_api.typer.Exit):
                self._split(client, "*.internal.lan", host_ids="12", yes=False)

        self.assertEqual(list(self.workdir.glob("pre_split_*.json")), [])
        self.assertEqual(client.calls, [])


# =============================================================================
# host split: the certificate the source keeps
# =============================================================================

class TestHostSplitDanglingSourceCertificate(_SplitCommandTestCase):
    """A split only fixes the half that moves.

    The domains left behind keep the source's existing certificate, and if that
    certificate has already been deleted they stay HTTP-only afterwards. The
    command cannot repair it — the operator has to choose a replacement — so it
    says so rather than reporting an unqualified success.
    """

    def _source(self, **overrides):
        return _merge_host(12, ["app.example.com", "api.internal.lan"], **overrides)

    def test_a_source_keeping_a_certificate_that_is_gone_is_warned_about(self):
        client = self._client(self._source(certificate_id=4), [])

        self._split(client, "*.internal.lan", cert="none", host_ids="12")

        self.assertPrinted("keep certificate 4, which no longer exists")

    def test_the_warning_names_the_command_that_repairs_it(self):
        client = self._client(self._source(certificate_id=4), [])

        self._split(client, "*.internal.lan", cert="none", host_ids="12")

        self.assertPrinted("host bulk-update certificate_id <cert> --ids 12")

    def test_the_warning_does_not_block_the_split(self):
        # Advisory: the moved half is what the command was asked to fix, and
        # refusing would leave the operator with no way to run it at all.
        client = self._client(self._source(certificate_id=4), [])

        self._split(client, "*.internal.lan", cert="none", host_ids="12")

        self.assertEqual(client.kinds, ["update", "create"])
        self.assertPrinted("Successful: 1")

    def test_a_source_with_no_certificate_says_nothing(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", cert="none", host_ids="12")

        self.assertEqual(client.cert_lookups, [])
        self.assertNotPrinted("no longer exists")

    def test_a_source_certificate_that_is_still_there_says_nothing(self):
        client = self._client(self._source(certificate_id=7), [],
                              certificate=_live_cert())

        self._split(client, "*.internal.lan", cert="none", host_ids="12")

        self.assertEqual(client.cert_lookups, [7])
        self.assertNotPrinted("no longer exists")

    def test_hosts_sharing_a_dead_certificate_are_named_together(self):
        # One lookup and one warning per certificate rather than per host: a
        # batch split after a certificate was deleted in the UI would otherwise
        # print the same paragraph twenty times.
        client = self._client(self._source(certificate_id=4),
                              [_merge_host(13, ["shop.example.com", "db.internal.lan"],
                                           certificate_id=4)])

        self._split(client, "*.internal.lan", cert="none", host_ids="12,13")

        self.assertEqual(client.cert_lookups, [4])
        self.assertPrinted("Host(s) 12, 13 keep certificate 4")


# =============================================================================
# host split: the world moving while the confirmation prompt is up
# =============================================================================

class TestHostSplitConcurrentChange(_SplitCommandTestCase):
    """select_hosts() runs once, at the very top of host_split, before the
    preview and before confirm_bulk(). Every plan's "staying" and "moving"
    lists are computed from that one read and never revisited, and confirm_bulk's
    prompt is the only synchronous pause in the whole command.

    Worse here than in merge: the trim's payload is {"domain_names": staying} —
    a full-field overwrite, per host_config_payload's `payload.update(overrides)`
    — so a domain added during the prompt would be written out of the source
    completely, and it was never part of "moving" either, so it would not land
    on the new host. It would end up served by nothing, with no error and no log
    line naming it. The source is checked before its trim and skipped instead.

    Note that the change is made here by MUTATING the host dict in place, which
    is what a second run of this same tool against the stub does. select_hosts
    hands back the stub's live objects, so the plan must compare against a
    snapshot taken at plan time rather than against plan["source"] — that dict
    moves along with the host and would show no difference at all.
    """

    def _source(self, domains=("app.example.com", "api.internal.lan")):
        return _merge_host(12, list(domains))

    def _split_with_a_changing_source(self, client, **overrides):
        def confirm_and_mutate(*args, **kwargs):
            # A concurrent session adds a domain to host 12 while the operator
            # is looking at the confirmation prompt, before answering "y".
            client.hosts[0]["domain_names"].append("new.example.com")
            return True

        with mock.patch.object(npm_api.typer, "confirm",
                               side_effect=confirm_and_mutate):
            with self.assertRaises(npm_api.typer.Exit) as caught:
                self._split(client, "*.internal.lan", host_ids="12", yes=False,
                            **overrides)
        return caught.exception

    def test_a_source_changed_during_the_prompt_is_not_split(self):
        client = self._client(self._source(), [])

        self._split_with_a_changing_source(client)

        # Sanity: the addition really did land on the host split is operating
        # on — the plan is stale, not a broken mutation.
        self.assertIn("new.example.com", client.hosts[0]["domain_names"])
        self.assertEqual(client.updates, [],
                         msg="the source must not be trimmed from a stale plan")
        self.assertEqual(client.created_overrides, [],
                         msg="no new host, since nothing was freed off the source")

    def test_the_added_domain_is_still_on_the_host_afterwards(self):
        # The point of the guard, stated as the property it protects: the domain
        # added during the prompt is served by exactly the host it was added to,
        # rather than by nothing at all.
        client = self._client(self._source(), [])

        self._split_with_a_changing_source(client)

        self.assertEqual(client.calls, [])
        self.assertIn("new.example.com", client.hosts[0]["domain_names"])
        self.assertIn("api.internal.lan", client.hosts[0]["domain_names"])

    def test_the_message_names_the_host_and_what_changed(self):
        client = self._client(self._source(), [])

        self._split_with_a_changing_source(client)

        self.assertPrinted("Host 12")
        self.assertPrinted("new.example.com")
        self.assertPrinted("since this split was planned")

    def test_a_changed_source_counts_as_a_failure(self):
        client = self._client(self._source(), [])

        exit_exc = self._split_with_a_changing_source(client)

        self.assertEqual(exit_exc.exit_code, 1)
        self.assertPrinted("Failed: 1")

    def test_one_changed_source_does_not_abandon_the_others(self):
        # Unlike merge's shared target, each source is independent, so a stale
        # one is skipped rather than failing the batch.
        client = self._client(self._source(),
                              [_merge_host(13, ["shop.example.com", "db.internal.lan"])])

        def confirm_and_mutate(*args, **kwargs):
            client.hosts[0]["domain_names"].append("new.example.com")
            return True

        with mock.patch.object(npm_api.typer, "confirm",
                               side_effect=confirm_and_mutate):
            with self.assertRaises(npm_api.typer.Exit):
                self._split(client, "*.internal.lan", host_ids="12,13", yes=False)

        self.assertEqual([host_id for host_id, _ in client.updates], [13])
        self.assertEqual(client.created_overrides,
                         [{"domain_names": ["db.internal.lan"], "certificate_id": None}])
        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")

    def test_an_unchanged_source_splits_as_before(self):
        client = self._client(self._source(), [])

        self._split(client, "*.internal.lan", host_ids="12")

        self.assertEqual(client.updates, [(12, {"domain_names": ["app.example.com"]})])
        self.assertEqual(client.created_overrides,
                         [{"domain_names": ["api.internal.lan"], "certificate_id": None}])
        self.assertPrinted("Successful: 1")

    def test_a_source_that_vanished_is_reported_as_gone(self):
        client = self._client(self._source(), [])

        def delete_the_source(*args, **kwargs):
            client.hosts = []
            return True

        with mock.patch.object(npm_api.typer, "confirm",
                               side_effect=delete_the_source):
            with self.assertRaises(npm_api.typer.Exit):
                self._split(client, "*.internal.lan", host_ids="12", yes=False)

        self.assertEqual(client.updates, [])
        self.assertPrinted("no longer exists")

    def test_the_snapshot_still_records_the_pre_split_state(self):
        # The snapshot is written before the loop, so it exists even for a run
        # every host of which turns out to be stale. Harmless, and the
        # alternative — deciding staleness before recording anything — would
        # move the read back to where the race is.
        client = self._client(self._source(), [])

        self._split_with_a_changing_source(client)

        written = list(self.workdir.glob("pre_split_*.json"))
        self.assertEqual(len(written), 1, written)


# =============================================================================
# Config.load
# =============================================================================

class _ConfigTestCase(_WorkdirTestCase):
    """Loads Config against a search path and an environment the test owns.

    Both halves have to be isolated or these tests read the machine they run
    on. Config._get_config_search_paths() looks in the working directory, the
    user's ~/.config, /etc/npm-api and the directory npm_api.py sits in — and
    under `unittest discover` the repo root is both the working directory and
    the script directory, so an operator's own npm-api.conf beside the script
    would supply their server address and password to every assertion below.
    The list is therefore replaced outright rather than merely pointed
    elsewhere, and the environment is cleared rather than merely added to,
    since a real NPM_API_PASS exported in the shell would override whatever a
    test wrote to a file.
    """

    def _write_conf(self, name, text):
        path = self.workdir / name
        path.write_text(text)
        return path

    def _load(self, search=(), env=None, config_path=None):
        # os.environ is reached through npm_api rather than imported again at
        # the top of this file: it is the same mapping object, and it is
        # specifically the one Config.load reads.
        with mock.patch.object(npm_api.Config, "_get_config_search_paths",
                               return_value=[Path(p) for p in search]), \
                mock.patch.dict(npm_api.os.environ, env or {}, clear=True):
            return npm_api.Config.load(config_path)


class TestConfigDefaults(_ConfigTestCase):
    """What Config is before anything has configured it."""

    def test_nothing_anywhere_leaves_every_default(self):
        config = self._load()

        self.assertEqual(config.nginx_ip, "127.0.0.1")
        self.assertEqual(config.nginx_port, "81")
        self.assertEqual(config.api_user, "admin@example.com")
        self.assertEqual(config.api_pass, "changeme")

    def test_the_source_is_reported_as_unconfigured(self):
        # get_client keys the whole setup banner off this, so a default config
        # that claimed to come from somewhere would suppress the one message
        # telling a new user what to do.
        config = self._load()

        self.assertEqual(config._config_source, "defaults")
        self.assertEqual(config.get_config_info(), "defaults (not configured)")

    def test_the_base_url_is_assembled_from_host_and_port(self):
        config = self._load(env={"NPM_API_HOST": "10.0.0.1", "NPM_API_PORT": "8181"})

        self.assertEqual(config.base_url, "http://10.0.0.1:8181/api")

    def test_the_data_directory_is_keyed_by_server_address(self):
        # One tree per NPM instance: the token cached for one server is not a
        # token for another, and restoring a backup into the wrong instance is
        # not a mistake this tool should make easy.
        config = self._load(env={"NPM_API_HOST": "10.0.0.1", "NPM_API_PORT": "8181",
                                 "NPM_API_DATA_DIR": str(self.workdir)})

        self.assertEqual(Path(config.data_dir_id), self.workdir / "10_0_0_1_8181")
        self.assertEqual(Path(config.token_file).parent, Path(config.token_dir))
        self.assertEqual(Path(config.token_dir).parent, Path(config.data_dir_id))
        self.assertEqual(Path(config.backup_dir).parent, Path(config.data_dir_id))

    def test_a_default_data_directory_sits_beside_the_script(self):
        # npm-api is deployed by copying one file, so its state lands next to
        # that file unless told otherwise.
        config = self._load()

        self.assertEqual(Path(config.data_dir),
                         Path(npm_api.__file__).parent / "data")


class TestConfigFile(_ConfigTestCase):
    """Reading a config file: which one, and how its lines are parsed."""

    def test_a_config_file_supplies_every_field(self):
        path = self._write_conf("npm-api.conf", (
            "NGINX_IP=10.0.0.1\n"
            "NGINX_PORT=8181\n"
            "API_USER=ops@example.com\n"
            "API_PASS=filepass\n"
            f"DATA_DIR={self.workdir}\n"
        ))

        config = self._load(search=[path])

        self.assertEqual(config.nginx_ip, "10.0.0.1")
        self.assertEqual(config.nginx_port, "8181")
        self.assertEqual(config.api_user, "ops@example.com")
        self.assertEqual(config.api_pass, "filepass")
        self.assertEqual(config.data_dir, str(self.workdir))

    def test_the_source_names_the_file_it_read(self):
        path = self._write_conf("npm-api.conf", "API_USER=ops@example.com\n")

        config = self._load(search=[path])

        self.assertEqual(config._config_source, "file")
        self.assertEqual(config._config_file_path, str(path))
        self.assertIn(str(path), config.get_config_info())

    def test_keys_are_case_insensitive(self):
        # The file format is inherited from the bash original, which spelled
        # its keys in upper case; the dataclass fields are lower case.
        for spelling in ("NGINX_IP", "nginx_ip", "Nginx_Ip"):
            with self.subTest(spelling=spelling):
                path = self._write_conf("npm-api.conf", f"{spelling}=10.0.0.1\n")

                self.assertEqual(self._load(search=[path]).nginx_ip, "10.0.0.1")

    def test_quotes_around_a_value_are_stripped(self):
        for quoted in ('"10.0.0.1"', "'10.0.0.1'"):
            with self.subTest(quoted=quoted):
                path = self._write_conf("npm-api.conf", f"NGINX_IP={quoted}\n")

                self.assertEqual(self._load(search=[path]).nginx_ip, "10.0.0.1")

    def test_surrounding_whitespace_is_stripped(self):
        path = self._write_conf("npm-api.conf", "  NGINX_IP  =  10.0.0.1  \n")

        self.assertEqual(self._load(search=[path]).nginx_ip, "10.0.0.1")

    def test_comments_and_blank_lines_are_ignored(self):
        path = self._write_conf("npm-api.conf", (
            "# NGINX_IP=192.0.2.1\n"
            "\n"
            "   \n"
            "NGINX_IP=10.0.0.1\n"
        ))

        self.assertEqual(self._load(search=[path]).nginx_ip, "10.0.0.1")

    def test_a_line_with_no_equals_sign_is_ignored(self):
        path = self._write_conf("npm-api.conf", "garbage\nNGINX_IP=10.0.0.1\n")

        self.assertEqual(self._load(search=[path]).nginx_ip, "10.0.0.1")

    def test_a_value_may_itself_contain_an_equals_sign(self):
        # Passwords routinely do, and splitting on every "=" would silently
        # truncate one.
        path = self._write_conf("npm-api.conf", "API_PASS=a=b=c\n")

        self.assertEqual(self._load(search=[path]).api_pass, "a=b=c")

    def test_an_unknown_key_is_ignored_rather_than_fatal(self):
        path = self._write_conf("npm-api.conf", "SOMETHING_ELSE=x\nNGINX_IP=10.0.0.1\n")

        self.assertEqual(self._load(search=[path]).nginx_ip, "10.0.0.1")

    def test_the_first_existing_file_wins(self):
        first = self._write_conf("first.conf", "NGINX_IP=10.0.0.1\n")
        second = self._write_conf("second.conf", "NGINX_IP=10.0.0.2\n")

        config = self._load(search=[first, second])

        self.assertEqual(config.nginx_ip, "10.0.0.1")
        self.assertEqual(config._config_file_path, str(first))

    def test_a_path_that_is_not_there_is_skipped(self):
        real = self._write_conf("real.conf", "NGINX_IP=10.0.0.1\n")

        config = self._load(search=[self.workdir / "absent.conf", real])

        self.assertEqual(config.nginx_ip, "10.0.0.1")

    def test_a_directory_of_that_name_is_not_a_config_file(self):
        # exists() alone would accept it and open() would then raise IsADirectory,
        # which _load_from_file swallows — leaving the run silently unconfigured
        # while claiming a file had been found.
        directory = self.workdir / "npm-api.conf"
        directory.mkdir()
        real = self._write_conf("real.conf", "NGINX_IP=10.0.0.1\n")

        config = self._load(search=[directory, real])

        self.assertEqual(config.nginx_ip, "10.0.0.1")
        self.assertEqual(config._config_file_path, str(real))

    def test_every_path_looked_at_is_recorded(self):
        # get_client prints this list with a tick beside each, so a user who
        # has put the file somewhere unsearched can see that.
        missing = self.workdir / "absent.conf"
        real = self._write_conf("real.conf", "NGINX_IP=10.0.0.1\n")

        config = self._load(search=[missing, real])

        self.assertEqual(config._searched_paths, [str(missing), str(real)])

    def test_the_search_stops_at_the_file_it_found(self):
        first = self._write_conf("first.conf", "NGINX_IP=10.0.0.1\n")
        second = self._write_conf("second.conf", "NGINX_IP=10.0.0.2\n")

        config = self._load(search=[first, second])

        self.assertEqual(config._searched_paths, [str(first)])

    def test_an_explicit_path_is_searched_before_the_standard_ones(self):
        standard = self._write_conf("standard.conf", "NGINX_IP=10.0.0.2\n")
        named = self._write_conf("named.conf", "NGINX_IP=10.0.0.1\n")

        config = self._load(search=[standard], config_path=str(named))

        self.assertEqual(config.nginx_ip, "10.0.0.1")

    def test_an_explicit_path_that_is_not_there_falls_back(self):
        standard = self._write_conf("standard.conf", "NGINX_IP=10.0.0.2\n")

        config = self._load(search=[standard],
                            config_path=str(self.workdir / "absent.conf"))

        self.assertEqual(config.nginx_ip, "10.0.0.2")


class TestConfigPrecedence(_ConfigTestCase):
    """Environment over file over defaults, one variable at a time."""

    def _conf(self):
        return self._write_conf("npm-api.conf", (
            "NGINX_IP=10.0.0.1\n"
            "NGINX_PORT=8181\n"
            "API_USER=file@example.com\n"
            "API_PASS=filepass\n"
            "DATA_DIR=/from/file\n"
        ))

    def test_each_environment_variable_beats_the_file(self):
        cases = (
            ("NPM_API_HOST", "nginx_ip", "192.0.2.10"),
            ("NPM_API_PORT", "nginx_port", "9191"),
            ("NPM_API_USER", "api_user", "env@example.com"),
            ("NPM_API_PASS", "api_pass", "envpass"),
            ("NPM_API_DATA_DIR", "data_dir", "/from/env"),
        )
        for env_var, attribute, value in cases:
            with self.subTest(env_var=env_var):
                config = self._load(search=[self._conf()], env={env_var: value})

                self.assertEqual(getattr(config, attribute), value)

    def test_the_fields_the_environment_did_not_name_still_come_from_the_file(self):
        # The override is per field, not wholesale: exporting NPM_API_PASS for
        # one command must not discard the server address in the file.
        config = self._load(search=[self._conf()], env={"NPM_API_PASS": "envpass"})

        self.assertEqual(config.api_pass, "envpass")
        self.assertEqual(config.nginx_ip, "10.0.0.1")
        self.assertEqual(config.api_user, "file@example.com")

    def test_the_environment_beats_the_defaults_with_no_file_at_all(self):
        config = self._load(env={"NPM_API_USER": "env@example.com"})

        self.assertEqual(config.api_user, "env@example.com")
        self.assertEqual(config.api_pass, "changeme")
        self.assertEqual(config._config_source, "env")
        self.assertEqual(config.get_config_info(), "environment variables")

    def test_file_and_environment_together_are_reported_as_both(self):
        # Worth distinguishing in the info output: "the password came from
        # somewhere other than the file you are looking at" is exactly the
        # thing that is hard to work out otherwise.
        path = self._conf()

        config = self._load(search=[path], env={"NPM_API_PASS": "envpass"})

        self.assertEqual(config._config_source, "file+env")
        self.assertIn(str(path), config.get_config_info())
        self.assertIn("environment variables", config.get_config_info())

    def test_an_empty_environment_variable_does_not_override(self):
        # `NPM_API_PASS= npm-api host list` reads as "unset for this command",
        # not as "the password is the empty string".
        config = self._load(search=[self._conf()], env={"NPM_API_PASS": ""})

        self.assertEqual(config.api_pass, "filepass")
        self.assertEqual(config._config_source, "file")

    def test_an_unrelated_environment_variable_is_not_a_source(self):
        config = self._load(search=[self._conf()], env={"NPM_API_SOMETHING": "x"})

        self.assertEqual(config._config_source, "file")


class TestConfigIsUsingDefaults(unittest.TestCase):
    """The check that stops the tool talking to NPM with its shipped credentials.

    Either half being untouched is enough. NPM's own factory login is
    admin@example.com / changeme, so a half-configured install is not a
    harmless one — it is a live guess at a real account.
    """

    def test_untouched_credentials_are_defaults(self):
        self.assertTrue(npm_api.Config().is_using_defaults())

    def test_a_real_user_beside_the_default_password_is_still_defaults(self):
        config = npm_api.Config(api_user="ops@example.com")

        self.assertTrue(config.is_using_defaults())

    def test_a_real_password_beside_the_default_user_is_still_defaults(self):
        config = npm_api.Config(api_pass="s3cret")

        self.assertTrue(config.is_using_defaults())

    def test_both_replaced_counts_as_configured(self):
        config = npm_api.Config(api_user="ops@example.com", api_pass="s3cret")

        self.assertFalse(config.is_using_defaults())

    def test_the_server_address_has_no_bearing_on_it(self):
        # A real address with the shipped credentials is the dangerous case,
        # not a safe one: 127.0.0.1 is a perfectly ordinary place to run NPM.
        config = npm_api.Config(nginx_ip="10.0.0.1", nginx_port="8181")

        self.assertTrue(config.is_using_defaults())


# =============================================================================
# --json: shared doubles
# =============================================================================

def _stats_fixture(**overrides):
    """A dashboard reading with every section answered."""
    stats = {
        "proxy_hosts": {"total": 3, "enabled": 2, "disabled": 1},
        "certificates": {"total": 2, "valid": 2, "expired": 0},
        "redirections": 0,
        "streams": 1,
        "users": 1,
        "access_lists": 0,
        "failures": [],
    }
    stats.update(overrides)
    return stats


class _JsonClient(_StubClient):
    """Serves a fixed inventory to every read-only command at once.

    Carries a real Config rather than a stand-in: `info` reads six separate
    attributes off it, several of them derived properties, and a Config built
    with no arguments touches nothing on disk — the directory creation lives in
    NPMClient.__init__, which _StubClient skips.
    """

    def __init__(self, *, hosts=None, certs=None, acls=None, users=None,
                 stats=None, token=True, auth_error=None, read_error=None):
        self.config = npm_api.Config()
        self.auth_error = auth_error
        self.hosts = [_merge_host(12, ["app.example.com"])] if hosts is None else hosts
        self.certs = [_live_cert(7, ("*.example.com",))] if certs is None else certs
        self.acls = [{"id": 5, "name": "ops", "satisfy_any": False,
                      "items": [], "clients": []}] if acls is None else acls
        self.users = [{"id": 1, "name": "Admin", "email": "admin@example.com",
                       "roles": ["admin"]}] if users is None else users
        self.stats = _stats_fixture() if stats is None else stats
        self._token = token
        self._read_error = read_error

    # --- authentication ------------------------------------------------------

    def ensure_token(self):
        return self._token

    # --- reads ---------------------------------------------------------------

    def _maybe_fail(self):
        if self._read_error is not None:
            raise self._read_error

    def list_hosts(self):
        self._maybe_fail()
        return self.hosts

    def get_host(self, host_id):
        for host in self.hosts:
            if host.get("id") == host_id:
                return host
        raise requests.HTTPError("404 Not Found", response=_FakeResponse(404))

    def search_hosts(self, term):
        return [h for h in self.hosts
                if any(term.lower() in d.lower() for d in h.get("domain_names", []))]

    def list_certificates(self):
        self._maybe_fail()
        return self.certs

    def get_certificate(self, cert_id):
        for cert in self.certs:
            if cert.get("id") == cert_id:
                return cert
        raise requests.HTTPError("404 Not Found", response=_FakeResponse(404))

    def list_access_lists(self):
        return self.acls

    def get_access_list(self, list_id):
        for acl in self.acls:
            if acl.get("id") == list_id:
                return acl
        raise requests.HTTPError("404 Not Found", response=_FakeResponse(404))

    def list_users(self):
        return self.users

    def get_dashboard_stats(self):
        return self.stats


class _JsonCommandTestCase(_ConsoleTestCase):
    """Captures the real stdout while the diagnostics console is recorded away.

    The split between the two streams is the property under test, so unlike
    every other command test here this one cannot patch both: `console` is
    replaced as usual by _ConsoleTestCase, and sys.stdout is captured for real
    so that anything reaching it is visible. Rich resolves sys.stdout on each
    write rather than at construction, so out_console is captured too and a
    table printed by mistake would show up.
    """

    def stdout_of(self, client, call):
        """Run `call` against `client`; return (stdout, exit code)."""
        buffer = io.StringIO()
        exit_code = 0
        with mock.patch.object(npm_api, "get_client", lambda: client), \
                mock.patch.object(sys, "stdout", buffer):
            try:
                call()
            except npm_api.typer.Exit as exc:
                exit_code = exc.exit_code
        return buffer.getvalue(), exit_code


# =============================================================================
# --json: stdout under success
# =============================================================================

class TestJsonOutputOnSuccess(_JsonCommandTestCase):
    """`--json` output has to survive being piped into jq.

    print_json bypasses both consoles and writes plain stdout for exactly this
    reason: everything else the tool says goes to stderr, so the document on
    stdout is the whole of stdout.
    """

    def test_every_json_command_writes_one_parseable_document(self):
        client = _JsonClient()
        cases = (
            ("host list", lambda: npm_api.host_list(as_json=True), client.hosts),
            ("host show", lambda: npm_api.host_show(host_id=12, as_json=True),
             client.hosts[0]),
            ("host search", lambda: npm_api.host_search(search="app", as_json=True),
             client.hosts),
            ("cert list", lambda: npm_api.cert_list(as_json=True), client.certs),
            ("cert show", lambda: npm_api.cert_show(identifier="7", as_json=True),
             client.certs[0]),
            ("acl list", lambda: npm_api.acl_list(as_json=True), client.acls),
            ("acl show", lambda: npm_api.acl_show(list_id=5, as_json=True),
             client.acls[0]),
            ("user list", lambda: npm_api.user_list(as_json=True), client.users),
        )
        for label, call, expected in cases:
            with self.subTest(command=label):
                text, exit_code = self.stdout_of(client, call)

                self.assertEqual(exit_code, 0)
                self.assertEqual(json.loads(text), expected)

    def test_cert_show_by_domain_returns_a_list_not_a_bare_object(self):
        # A domain can match several certificates, so the shape has to be the
        # same whether it matched one or three — a consumer cannot branch on
        # something it has not parsed yet.
        client = _JsonClient()

        text, exit_code = self.stdout_of(
            client, lambda: npm_api.cert_show(identifier="example.com", as_json=True))

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(text), client.certs)

    def test_info_reports_the_dashboard_and_the_configuration_together(self):
        client = _JsonClient()

        text, exit_code = self.stdout_of(client, lambda: npm_api.info(as_json=True))

        payload = json.loads(text)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["stats"], client.stats)
        self.assertEqual(payload["version"], npm_api.VERSION)
        self.assertEqual(payload["base_url"], client.config.base_url)

    def test_info_json_carries_no_password_or_token(self):
        # It is meant to be pasteable into an issue. The API user is in there
        # on purpose; nothing that authenticates as them may be.
        client = _JsonClient()
        client.config.api_pass = "s3cret-should-not-appear"

        text, _ = self.stdout_of(client, lambda: npm_api.info(as_json=True))

        self.assertNotIn("s3cret-should-not-appear", text)
        self.assertNotIn("token", text.lower())

    def test_an_empty_inventory_is_an_empty_array_not_no_output(self):
        # `host list --json | jq length` on a fresh NPM has to answer 0 rather
        # than failing to parse.
        client = _JsonClient(hosts=[], certs=[], acls=[], users=[])
        cases = (
            ("host list", lambda: npm_api.host_list(as_json=True)),
            ("cert list", lambda: npm_api.cert_list(as_json=True)),
            ("acl list", lambda: npm_api.acl_list(as_json=True)),
            ("user list", lambda: npm_api.user_list(as_json=True)),
        )
        for label, call in cases:
            with self.subTest(command=label):
                text, exit_code = self.stdout_of(client, call)

                self.assertEqual(exit_code, 0)
                self.assertEqual(json.loads(text), [])

    def test_nothing_but_the_document_reaches_stdout(self):
        # No banner, no spinner, no "Backed up N things" line. json.loads would
        # catch most of that; the equality below catches a trailing blank line
        # or a second document too.
        client = _JsonClient()

        text, _ = self.stdout_of(client, lambda: npm_api.host_list(as_json=True))

        self.assertEqual(text, json.dumps(client.hosts, indent=2, sort_keys=True) + "\n")


# =============================================================================
# --json: stdout under failure
# =============================================================================

class TestJsonOutputOnFailure(_JsonCommandTestCase):
    """A failing --json run must leave stdout empty, not half a document.

    Half a document is worse than none: jq fails either way, but a partial one
    can also parse by accident when the failure happened after a complete
    top-level value was written.
    """

    def test_a_missing_object_writes_nothing_at_all_to_stdout(self):
        client = _JsonClient()
        cases = (
            ("host show", lambda: npm_api.host_show(host_id=99, as_json=True)),
            ("cert show", lambda: npm_api.cert_show(identifier="99", as_json=True)),
            ("acl show", lambda: npm_api.acl_show(list_id=99, as_json=True)),
        )
        for label, call in cases:
            with self.subTest(command=label):
                text, exit_code = self.stdout_of(client, call)

                self.assertEqual(text, "")
                self.assertEqual(exit_code, 1)

    def test_the_diagnostic_goes_to_the_other_console(self):
        client = _JsonClient()

        self.stdout_of(client, lambda: npm_api.host_show(host_id=99, as_json=True))

        self.assertPrinted("Host ID 99 not found")

    def test_a_domain_matching_no_certificate_is_not_an_empty_document(self):
        # This one exits 0, because "no certificate is called that" is an
        # answer rather than a failure. It still may not print half of one.
        client = _JsonClient()

        text, exit_code = self.stdout_of(
            client, lambda: npm_api.cert_show(identifier="nowhere.example.net",
                                              as_json=True))

        self.assertEqual(text, "")
        self.assertEqual(exit_code, 0)
        self.assertPrinted("No certificates found")

    def test_a_read_that_fails_outright_writes_nothing(self):
        # The HTTPError escapes to main(), which reports it on stderr and
        # exits 1. What matters here is that print_json was never reached.
        client = _JsonClient(read_error=_npm_http_error(500, "upstream exploded"))
        cases = (
            ("host list", lambda: npm_api.host_list(as_json=True)),
            ("cert list", lambda: npm_api.cert_list(as_json=True)),
        )
        for label, call in cases:
            with self.subTest(command=label):
                buffer = io.StringIO()
                with mock.patch.object(npm_api, "get_client", lambda: client), \
                        mock.patch.object(sys, "stdout", buffer):
                    with self.assertRaises(requests.HTTPError):
                        call()

                self.assertEqual(buffer.getvalue(), "")

    def test_info_that_cannot_authenticate_writes_nothing(self):
        client = _JsonClient(token=False, auth_error="rejected the credentials")

        text, exit_code = self.stdout_of(client, lambda: npm_api.info(as_json=True))

        self.assertEqual(text, "")
        self.assertEqual(exit_code, 1)
        self.assertPrinted("rejected the credentials")

    def test_info_with_an_unreadable_section_prints_the_whole_document_and_exits_1(self):
        # The one place both happen at once, and deliberately so: the counts a
        # section could not supply are null rather than 0, so the document is
        # worth having, while the shell still needs to see that the run did not
        # come back clean.
        client = _JsonClient(stats=_stats_fixture(
            users=None, failures=["users: HTTP 403: Permission denied"]))

        text, exit_code = self.stdout_of(client, lambda: npm_api.info(as_json=True))

        payload = json.loads(text)
        self.assertEqual(exit_code, 1)
        self.assertIsNone(payload["stats"]["users"])
        self.assertEqual(payload["stats"]["failures"],
                         ["users: HTTP 403: Permission denied"])


# =============================================================================
# info: the human-readable dashboard
# =============================================================================

class TestInfoTable(_JsonCommandTestCase):
    """Without --json the data goes to stdout and the complaints do not."""

    def test_the_dashboard_lands_on_stdout(self):
        client = _JsonClient()

        text, exit_code = self.stdout_of(client, lambda: npm_api.info(as_json=False))

        self.assertEqual(exit_code, 0)
        self.assertIn("Proxy Hosts", text)
        self.assertIn("NGINX Proxy Manager Dashboard", text)

    def test_an_unreadable_section_renders_as_a_question_mark_not_a_zero(self):
        # A sick NPM used to render identically to an empty one, and "0 proxy
        # hosts" reads as a fact.
        client = _JsonClient(stats=_stats_fixture(
            proxy_hosts={"total": None, "enabled": None, "disabled": None},
            failures=["proxy hosts: HTTP 500"]))

        text, exit_code = self.stdout_of(client, lambda: npm_api.info(as_json=False))

        # Read the row as the operator sees it. With the styling left in, the
        # "0" of a \x1b[0m reset counts as a digit and the row reads as a
        # count of zero to this assertion even while rendering a "?".
        plain = strip_ansi(text)
        self.assertEqual(exit_code, 1)
        self.assertIn("?", plain)
        self.assertNotIn("0", plain.split("Proxy Hosts")[1].splitlines()[0])
        self.assertPrinted("section(s) could not be read")

    def test_a_failure_to_authenticate_still_exits_non_zero(self):
        # The configuration lines are printed before the token is needed, so a
        # bare return would let an unusable setup look like a clean run.
        client = _JsonClient(token=False, auth_error="rejected the credentials")

        _, exit_code = self.stdout_of(client, lambda: npm_api.info(as_json=False))

        self.assertEqual(exit_code, 1)
        self.assertPrinted("rejected the credentials")


# =============================================================================
# cert generate
# =============================================================================

class _CertGenClient(_StubClient):
    """Certificate lookup and issuance, with every request recorded.

    Carries a real Config because cert_generate falls back to the configured
    API user as the Let's Encrypt contact address when --email is left out.
    """

    def __init__(self, existing=None, error=None):
        self.config = npm_api.Config(api_user="ops@example.com")
        self.existing = existing
        self.error = error
        self.requests = []

    def find_certificate(self, domain):
        return self.existing

    def generate_certificate(self, domain, email, dns_provider=None,
                             dns_credentials=None):
        self.requests.append((domain, email, dns_provider, dns_credentials))
        if self.error is not None:
            raise self.error
        return {"id": 42}


class TestCertGenerate(_ConsoleTestCase):
    """Issuing a certificate: what it refuses, and what it sends."""

    _CREDENTIALS = '{"dns_cloudflare_api_token": "tok"}'

    def _generate(self, client, domain, **overrides):
        options = dict(domain=domain, email=None, dns_provider=None,
                       dns_credentials=None, yes=True)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.cert_generate(**options)

    def _generate_expecting_exit(self, client, domain, **overrides):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._generate(client, domain, **overrides)
        return caught.exception

    def test_a_wildcard_without_dns_details_is_refused(self):
        # Let's Encrypt will only issue a wildcard against a DNS-01 challenge,
        # so NPM cannot even start without a provider and its credentials.
        for missing, options in (
            ("both", {}),
            ("credentials", {"dns_provider": "cloudflare"}),
            ("provider", {"dns_credentials": self._CREDENTIALS}),
        ):
            with self.subTest(missing=missing):
                client = _CertGenClient()

                exit_exception = self._generate_expecting_exit(
                    client, "*.example.com", **options)

                self.assertEqual(exit_exception.exit_code, 1)
                self.assertEqual(client.requests, [])

    def test_a_wildcard_with_both_goes_through(self):
        client = _CertGenClient()

        self._generate(client, "*.example.com", dns_provider="cloudflare",
                       dns_credentials=self._CREDENTIALS)

        (domain, _, provider, credentials), = client.requests
        self.assertEqual(domain, "*.example.com")
        self.assertEqual(provider, "cloudflare")
        # Parsed here rather than passed through as text: the client builds the
        # meta block from it and re-serialises it itself.
        self.assertEqual(credentials, {"dns_cloudflare_api_token": "tok"})

    def test_malformed_dns_credentials_are_refused_as_a_value_error(self):
        client = _CertGenClient()

        exit_exception = self._generate_expecting_exit(
            client, "*.example.com", dns_provider="cloudflare",
            dns_credentials="{not json")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.requests, [])

    def test_a_certificate_that_is_still_valid_is_not_reissued(self):
        # Let's Encrypt rate-limits duplicate certificates, and NPM keeps the
        # old one alongside the new, so a needless reissue costs twice.
        client = _CertGenClient(existing={"id": 7, "domain_names": ["example.com"],
                                          "expires_on": _expires_in(timedelta(days=60))})

        self._generate(client, "example.com")

        self.assertEqual(client.requests, [])
        self.assertPrinted("already exists")
        self.assertPrinted("Certificate ID: 7")

    def test_an_expired_certificate_is_replaced(self):
        client = _CertGenClient(existing={"id": 7, "domain_names": ["example.com"],
                                          "expires_on": _expires_in(timedelta(days=-1))})

        self._generate(client, "example.com")

        self.assertPrinted("Replacing certificate 7")
        self.assertEqual(len(client.requests), 1)

    def test_an_unreadable_expiry_falls_through_and_reissues(self):
        # "I cannot tell whether this is still good" must not become "it is
        # still good": the failure mode of not reissuing is an outage.
        client = _CertGenClient(existing={"id": 7, "domain_names": ["example.com"],
                                          "expires_on": "not a date"})

        self._generate(client, "example.com")

        self.assertEqual(len(client.requests), 1)

    def test_the_contact_address_defaults_to_the_configured_user(self):
        client = _CertGenClient()

        self._generate(client, "example.com")

        (_, email, _, _), = client.requests
        self.assertEqual(email, "ops@example.com")
        self.assertPrinted("Using default email")

    def test_an_explicit_email_wins(self):
        client = _CertGenClient()

        self._generate(client, "example.com", email="certs@example.com")

        (_, email, _, _), = client.requests
        self.assertEqual(email, "certs@example.com")

    def test_a_rejected_request_exits_1_carrying_npms_message(self):
        client = _CertGenClient(error=_npm_http_error(
            400, "Cannot request certificate for example.com"))

        exit_exception = self._generate_expecting_exit(client, "example.com")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertPrinted("Cannot request certificate for example.com")

    def test_a_value_error_from_the_client_exits_1(self):
        # The client raises this for a wildcard with no DNS provider, which the
        # command already guards against — but the guard and the client's own
        # check can drift, and the second one must not surface as a traceback.
        client = _CertGenClient(error=ValueError("Wildcard certificates require DNS"))

        exit_exception = self._generate_expecting_exit(client, "example.com")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertPrinted("Wildcard certificates require DNS")

    def test_the_new_certificate_id_is_reported(self):
        client = _CertGenClient()

        self._generate(client, "example.com")

        self.assertPrinted("Certificate ID: 42")


# =============================================================================
# host bulk-update
# =============================================================================

class TestHostBulkUpdate(_MergeCommandTestCase):
    """One field written across a selection, and the guard on certificate_id.

    Reuses the merge doubles: _MergeClient already serves an inventory, records
    every update in order and can answer or refuse a certificate lookup, which
    is exactly the surface bulk-update touches.
    """

    def _bulk_update(self, client, field, value, **overrides):
        options = dict(field=field, value=value, host_ids=None, pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_update(**options)

    def _bulk_update_expecting_exit(self, client, field, value, **overrides):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._bulk_update(client, field, value, **overrides)
        return caught.exception

    def _client_with_two_hosts(self, **kwargs):
        return self._client(_merge_host(12, ["app.example.com"]),
                            [_merge_host(13, ["api.example.com"])], **kwargs)

    def test_the_value_is_coerced_before_it_is_written(self):
        # NPM's schema is typed: "true" as a string where a boolean belongs is
        # a 400, and "8080" where an integer belongs is silently wrong.
        cases = (
            ("block_exploits", "true", True),
            ("forward_port", "8080", 8080),
            ("forward_host", "10.0.0.9", "10.0.0.9"),
            ("domain_names", "a.example.com,b.example.com",
             ["a.example.com", "b.example.com"]),
        )
        for field, raw, expected in cases:
            with self.subTest(field=field):
                client = self._client(_merge_host(12, ["app.example.com"]), [])

                self._bulk_update(client, field, raw, host_ids="12")

                self.assertEqual(client.updates, [(12, {field: expected})])

    def test_every_selected_host_is_written_once(self):
        client = self._client_with_two_hosts()

        self._bulk_update(client, "forward_port", "8080", host_ids="12,13")

        self.assertEqual(client.updates, [(12, {"forward_port": 8080}),
                                          (13, {"forward_port": 8080})])

    def test_a_certificate_that_does_not_exist_refuses_and_writes_nothing(self):
        # Same guard as split, clone and merge, at the one call site where the
        # certificate is being assigned to hosts chosen by pattern — so the
        # blast radius is however many hosts matched.
        client = self._client_with_two_hosts()

        exit_exception = self._bulk_update_expecting_exit(
            client, "certificate_id", "99", host_ids="12,13")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Certificate 99 does not exist")

    def test_a_certificate_that_exists_is_written(self):
        client = self._client_with_two_hosts(
            certificate=_live_cert(7, ("*.example.com",)))

        self._bulk_update(client, "certificate_id", "7", host_ids="12")

        self.assertEqual(client.cert_lookups, [7])
        self.assertEqual(client.updates, [(12, {"certificate_id": 7})])

    def test_clearing_the_certificate_needs_no_lookup(self):
        # 0 means "nothing linked" to NPM, and coerce_field_value turns it into
        # a null for the link fields, so there is no certificate to check.
        client = self._client_with_two_hosts()

        self._bulk_update(client, "certificate_id", "0", host_ids="12")

        self.assertEqual(client.cert_lookups, [])
        self.assertEqual(client.updates, [(12, {"certificate_id": None})])

    def test_a_malformed_json_value_exits_1_before_any_write(self):
        client = self._client_with_two_hosts()

        exit_exception = self._bulk_update_expecting_exit(
            client, "locations", "[{broken", host_ids="12")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("locations: invalid JSON")

    def test_no_selector_writes_nothing(self):
        # The regression this guard exists for: a bulk command with no --ids
        # and no --pattern once fell through to every host in the estate. It is
        # a refusal rather than a no-op, so that a script which forgot to build
        # its --ids string fails instead of quietly doing nothing.
        client = self._client_with_two_hosts()

        exit_exception = self._bulk_update_expecting_exit(client, "forward_port", "8080")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.calls, [])
        self.assertPrinted("Please specify --ids, --pattern, or --interactive")

    def test_a_rejected_write_is_counted_and_the_run_exits_1(self):
        client = self._client_with_two_hosts(update_fail_calls={1})

        exit_exception = self._bulk_update_expecting_exit(
            client, "forward_port", "8080", host_ids="12,13")

        self.assertEqual(exit_exception.exit_code, 1)
        # The second host was still attempted: one rejection does not abandon
        # the batch.
        self.assertEqual([host_id for host_id, _ in client.updates], [12, 13])
        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")

    def test_the_preview_names_the_field_and_the_new_value(self):
        client = self._client_with_two_hosts()

        self._bulk_update(client, "forward_port", "8080", host_ids="12", preview=True)

        self.assertPrinted("Bulk Update Preview")
        self.assertPrinted("Total hosts to update")

    def test_an_empty_domain_names_refuses_and_writes_nothing(self):
        # coerce_field_value folds every one of these into [], and a host whose
        # domain_names is [] is an nginx server block with no server_name: NPM
        # keeps it and nothing can reach it. A batch selected by --pattern would
        # take that many hosts off the air in one command, reported as success.
        for value in ("", " ", ",", " , ", "[]", "null"):
            with self.subTest(value=value):
                client = self._client_with_two_hosts()

                exit_exception = self._bulk_update_expecting_exit(
                    client, "domain_names", value, host_ids="12,13")

                self.assertEqual(exit_exception.exit_code, 1)
                self.assertEqual(client.calls, [])
                self.assertPrinted("domain_names is empty")

    def test_a_domain_names_with_one_name_left_is_still_written(self):
        # The refusal is about emptiness, not about shrinking: cutting a host
        # down to a single name is a legitimate edit.
        client = self._client_with_two_hosts()

        self._bulk_update(client, "domain_names", "only.example.com", host_ids="12")

        self.assertEqual(client.updates, [(12, {"domain_names": ["only.example.com"]})])

    # --- the host moved while the confirmation prompt was up ----------------

    def test_a_host_changed_since_the_preview_is_skipped(self):
        # bulk-update names one field, so update_host's own read supplies
        # domain_names and no domain is lost — but the preview the operator
        # approved described this host as it was read before the prompt, and
        # writing the field onto a host that has since moved applies a change
        # they never saw.
        client = self._client_with_two_hosts()

        def change_host_13(*args, **kwargs):
            client.hosts[1] = _merge_host(13, ["api.example.com", "late.example.com"])

        with mock.patch.object(npm_api, "confirm_bulk", change_host_13):
            exit_exception = self._bulk_update_expecting_exit(
                client, "forward_port", "9090", host_ids="12,13")

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.updates, [(12, {"forward_port": 9090})])
        self.assertPrinted("Host 13")
        self.assertPrinted("late.example.com")
        self.assertPrinted("Successful: 1")
        self.assertPrinted("Failed: 1")

    def test_an_unchanged_selection_is_written_as_before(self):
        client = self._client_with_two_hosts()

        self._bulk_update(client, "forward_port", "9090", host_ids="12,13")

        self.assertEqual(client.updates,
                         [(12, {"forward_port": 9090}), (13, {"forward_port": 9090})])
        self.assertNotPrinted("Failed:")

    def test_a_host_deleted_since_the_preview_is_reported_as_gone(self):
        client = self._client_with_two_hosts()

        def delete_host_13(*args, **kwargs):
            client.hosts = [h for h in client.hosts if h.get("id") != 13]

        with mock.patch.object(npm_api, "confirm_bulk", delete_host_13):
            self._bulk_update_expecting_exit(
                client, "forward_port", "9090", host_ids="12,13")

        self.assertEqual(client.updates, [(12, {"forward_port": 9090})])
        self.assertPrinted("no longer exists")


# =============================================================================
# host update
# =============================================================================

class TestHostUpdate(_MergeCommandTestCase):
    """The single-host spelling of bulk-update, and the same emptiness rule.

    Reuses the merge double for the same reason bulk-update's tests do: it
    records every update in order, which is the only place the outcome shows.
    """

    def _update(self, client, host_id, field_value):
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_update(host_id=host_id, field_value=field_value)

    def _update_expecting_exit(self, client, host_id, field_value):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._update(client, host_id, field_value)
        return caught.exception

    def test_a_field_is_coerced_and_written(self):
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._update(client, 12, "forward_port=8080")

        self.assertEqual(client.updates, [(12, {"forward_port": 8080})])

    def test_an_empty_domain_names_refuses_and_writes_nothing(self):
        # `host update 12 domain_names=` is what a script produces when the
        # variable holding the list came back empty.
        for spelling in ("domain_names=", "domain_names= ", "domain_names=,",
                         "domain_names=[]", "domain_names=null"):
            with self.subTest(spelling=spelling):
                client = self._client(_merge_host(12, ["app.example.com"]), [])

                exit_exception = self._update_expecting_exit(client, 12, spelling)

                self.assertEqual(exit_exception.exit_code, 1)
                self.assertEqual(client.calls, [])
                self.assertPrinted("domain_names is empty")

    def test_a_non_empty_domain_names_is_written(self):
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._update(client, 12, "domain_names=a.example.com,b.example.com")

        self.assertEqual(
            client.updates,
            [(12, {"domain_names": ["a.example.com", "b.example.com"]})])

    def test_another_field_may_still_be_emptied(self):
        # Only domain_names leaves a host unreachable; clearing advanced_config
        # is an ordinary edit and must not be caught by the same guard.
        client = self._client(_merge_host(12, ["app.example.com"]), [])

        self._update(client, 12, "advanced_config=")

        self.assertEqual(client.updates, [(12, {"advanced_config": ""})])


# =============================================================================
# the permission assertion's own escape hatch
# =============================================================================

class TestAssertOwnerOnly(_WorkdirTestCase):
    """The helper must skip on Windows and assert everywhere else.

    Worth its own tests because the failure mode is silence: a version that
    returned early on every platform, or that compared against whatever mode
    it found, would leave ten call sites green while checking nothing. The
    permission on a private key is the thing those ten sites exist to prove.
    """

    def _owner_only_file(self, mode=0o600):
        path = self.workdir / "secret"
        if path.exists():
            path.unlink()
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, mode))
        return path

    def test_a_correctly_moded_file_passes(self):
        assert_owner_only(self, self._owner_only_file())

    def test_a_world_readable_file_fails(self):
        with self.assertRaises(AssertionError):
            assert_owner_only(self, self._owner_only_file(0o644))

    def test_windows_skips_rather_than_passing(self):
        # The distinction that matters: not asserted, and *known* not to be
        # asserted. A silent pass would report coverage that does not exist.
        path = self._owner_only_file(0o644)
        with mock.patch.object(os, "name", "nt"):
            with self.assertRaises(unittest.SkipTest):
                assert_owner_only(self, path)

    def test_the_skip_names_where_the_caveat_is_written_down(self):
        with mock.patch.object(os, "name", "nt"):
            with self.assertRaises(unittest.SkipTest) as caught:
                assert_owner_only(self, self._owner_only_file())
        self.assertIn("SECURITY.md", str(caught.exception))


# =============================================================================
# rebasing a domain onto another base, by label rather than by character
# =============================================================================

class TestDomainIsUnder(unittest.TestCase):
    """Whether one name sits at or beneath a base, compared label by label."""

    def test_the_base_itself_is_under_it(self):
        self.assertTrue(npm_api.domain_is_under("example.com", "example.com"))

    def test_a_subdomain_is_under_it(self):
        self.assertTrue(npm_api.domain_is_under("ex.example.com", "example.com"))

    def test_a_deep_subdomain_is_under_it(self):
        self.assertTrue(npm_api.domain_is_under("a.b.example.com", "example.com"))

    def test_a_name_merely_ending_in_those_characters_is_not(self):
        # The whole point: 'myexample.com' shares a suffix with 'example.com'
        # as text and shares no label with it as a name.
        self.assertFalse(npm_api.domain_is_under("myexample.com", "example.com"))

    def test_a_partial_first_label_is_not(self):
        self.assertFalse(npm_api.domain_is_under("example.com", "e.com"))

    def test_a_one_label_base_is_never_a_base(self):
        for domain in ("example.com", "ex.example.com", "com"):
            with self.subTest(domain=domain):
                self.assertFalse(npm_api.domain_is_under(domain, "com"))

    def test_case_and_a_trailing_dot_do_not_matter(self):
        self.assertTrue(npm_api.domain_is_under("EX.Example.COM", "example.com."))

    def test_a_shorter_name_is_not_under_a_longer_base(self):
        self.assertFalse(npm_api.domain_is_under("example.com", "a.example.com"))


class TestReplaceDomainBase(unittest.TestCase):
    """Moving a name from one base onto another.

    Replaced `old in domain` plus str.replace, which matched characters: it
    rewrote 'myexample.com' while renaming 'example.com', and let a one-label
    argument match most of an estate.
    """

    def test_a_subdomain_is_rebased(self):
        self.assertEqual(
            npm_api.replace_domain_base("ex.example.com", "example.com", "example.net"),
            "ex.example.net")

    def test_the_apex_is_rebased_to_the_bare_new_base(self):
        self.assertEqual(
            npm_api.replace_domain_base("example.com", "example.com", "example.net"),
            "example.net")

    def test_every_subdomain_label_survives(self):
        self.assertEqual(
            npm_api.replace_domain_base("a.b.example.com", "example.com", "example.net"),
            "a.b.example.net")

    def test_a_name_merely_containing_the_base_is_untouched(self):
        self.assertIsNone(
            npm_api.replace_domain_base("myexample.com", "example.com", "example.net"))

    def test_an_unrelated_name_is_untouched(self):
        self.assertIsNone(
            npm_api.replace_domain_base("other.internal.lan", "example.com", "example.net"))

    def test_a_one_label_old_base_rebases_nothing(self):
        self.assertIsNone(
            npm_api.replace_domain_base("example.com", "com", "net"))

    def test_the_subdomain_keeps_the_spelling_npm_holds(self):
        # Only the base was asked to change. Lowercasing the rest is an
        # unrequested write to a field the operator can see in the UI.
        self.assertEqual(
            npm_api.replace_domain_base("Shop.Example.COM", "example.com", "example.net"),
            "Shop.example.net")

    def test_the_new_base_lands_spelled_as_given(self):
        self.assertEqual(
            npm_api.replace_domain_base("ex.example.com", "example.com", "Example.NET"),
            "ex.Example.NET")

    def test_a_trailing_dot_on_either_side_is_tolerated(self):
        self.assertEqual(
            npm_api.replace_domain_base("ex.example.com.", "example.com", "example.net."),
            "ex.example.net")

    def test_rebasing_onto_the_same_base_returns_the_same_name(self):
        self.assertEqual(
            npm_api.replace_domain_base("ex.example.com", "example.com", "example.com"),
            "ex.example.com")


# =============================================================================
# bulk-replace-domain: the old base names the hosts, so a selector is optional
# =============================================================================

class TestBulkReplaceDomainSelector(_ConsoleTestCase):
    """The old base already says which hosts are meant.

    Requiring --pattern as well was ceremony that guarded nothing: the reason
    given for it was that a short argument like 'com' would match the estate,
    but typing '-p com' reproduced that exactly. The argument is now required
    to be a real base and matched by label, which closes the hole for real,
    and the selector is free to be omitted.
    """

    def _hosts(self):
        return [
            _merge_host(1, ["ex.example.com"]),
            _merge_host(2, ["shop.example.com", "www.example.com"]),
            _merge_host(3, ["other.internal.lan"]),
            _merge_host(4, ["myexample.com"]),
        ]

    def _replace(self, client, old, new, **overrides):
        options = dict(old_domain=old, new_domain=new, host_ids=None,
                       pattern=None, preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_replace_domain(**options)

    def test_with_no_selector_every_host_on_that_base_is_rebased(self):
        client = _BulkDomainClient(self._hosts())

        self._replace(client, "example.com", "example.net")

        self.assertEqual(sorted(host_id for host_id, _ in client.calls), [1, 2])

    def test_a_host_on_another_base_is_left_alone(self):
        client = _BulkDomainClient(self._hosts())

        self._replace(client, "example.com", "example.net")

        self.assertNotIn(3, [host_id for host_id, _ in client.calls])

    def test_a_name_merely_containing_the_base_is_never_rewritten(self):
        # host 4 is 'myexample.com'. The old rule turned it into
        # 'myexample.net' while the operator was renaming a different base.
        client = _BulkDomainClient(self._hosts())

        self._replace(client, "example.com", "example.net")

        self.assertNotIn(4, [host_id for host_id, _ in client.calls])

    def test_both_names_on_one_host_are_rebased_together(self):
        client = _BulkDomainClient(self._hosts())

        self._replace(client, "example.com", "example.net")

        written = dict(client.calls)
        self.assertEqual(written[2]["domain_names"],
                         ["shop.example.net", "www.example.net"])

    def test_an_explicit_ids_selector_still_narrows(self):
        client = _BulkDomainClient(self._hosts())

        self._replace(client, "example.com", "example.net", host_ids="1")

        self.assertEqual([host_id for host_id, _ in client.calls], [1])

    def test_an_explicit_pattern_still_narrows(self):
        client = _BulkDomainClient(self._hosts())

        self._replace(client, "example.com", "example.net", pattern="shop.*")

        self.assertEqual([host_id for host_id, _ in client.calls], [2])

    def test_a_one_label_old_base_is_refused_before_the_client_is_touched(self):
        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._replace(_ExplodingClient(), "com", "net")

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertPrinted("must be a full domain")

    def test_no_host_on_that_base_exits_1_rather_than_reporting_success(self):
        client = _BulkDomainClient(self._hosts())

        with self.assertRaises(npm_api.typer.Exit) as caught:
            self._replace(client, "absent.example.org", "example.net")

        self.assertEqual(caught.exception.exit_code, 1)
        self.assertEqual(client.calls, [])

    def test_the_refusal_names_the_base_that_matched_nothing(self):
        client = _BulkDomainClient(self._hosts())

        with self.assertRaises(npm_api.typer.Exit):
            self._replace(client, "absent.example.org", "example.net")

        self.assertPrinted("absent.example.org")

    def test_the_stored_case_of_a_subdomain_survives_the_rewrite(self):
        client = _BulkDomainClient([_merge_host(1, ["Shop.Example.COM"])])

        self._replace(client, "example.com", "example.net")

        self.assertEqual(dict(client.calls)[1]["domain_names"],
                         ["Shop.example.net"])

    def test_rebasing_onto_the_same_base_writes_nothing(self):
        client = _BulkDomainClient([_merge_host(1, ["ex.example.com"])])

        self._replace(client, "example.com", "example.com")

        self.assertEqual(client.calls, [])


# =============================================================================
# host cert-assign: the findable name for bulk certificate assignment
# =============================================================================

class TestCertAssignAlias(_ConsoleTestCase):
    """`ssl-enable` is named after a toggle, so nobody finds it.

    cert-assign is the same command under the name people reach for. It must
    stay a delegation rather than a second copy, or the certificate validation
    that lives in bulk-update applies to only one of the two.
    """

    def _cert_assign(self, cert_id, **overrides):
        options = dict(cert_id=cert_id, host_ids="1", pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        npm_api.host_cert_assign(**options)

    def test_it_writes_the_certificate_through_ssl_enable(self):
        seen = {}

        def record(**kwargs):
            seen.update(kwargs)

        with mock.patch.object(npm_api, "host_ssl_enable", record):
            self._cert_assign(14)

        self.assertEqual(seen["cert_id"], 14)

    def test_every_selector_is_forwarded_unchanged(self):
        seen = {}

        def record(**kwargs):
            seen.update(kwargs)

        with mock.patch.object(npm_api, "host_ssl_enable", record):
            self._cert_assign(14, host_ids="3,7", pattern="example.com",
                              preview=True, yes=False, interactive=True)

        self.assertEqual(
            (seen["host_ids"], seen["pattern"], seen["preview"],
             seen["yes"], seen["interactive"]),
            ("3,7", "example.com", True, False, True))

    def test_it_is_registered_as_its_own_command(self):
        names = {c.name for c in npm_api.host_app.registered_commands}
        self.assertIn("cert-assign", names)
        self.assertIn("ssl-enable", names)


# =============================================================================
# host bulk-add-domain / bulk-replace-domain: a blank domain argument
# =============================================================================

class _ExplodingClient:
    """A client that fails on first contact.

    Lets a test say "the guard ran before the inventory was read" rather than
    only "before the write": any command that reaches select_hosts against this
    raises instead of quietly returning a plausible result.
    """

    def __getattr__(self, name):
        raise AssertionError(f"the client was used before the guard: {name}")


class TestBlankDomainArguments(_MergeCommandTestCase):
    """`bulk-replace-domain "$OLD" "$NEW"` with the variable unset.

    "" is a substring of every name and str.replace("", new) inserts between
    every character, so an empty --old rewrote app.example.com into
    example.netaexample.netpexample.netp… on every selected host, wrote it,
    reported Successful: 1 and exited 0. An empty --new is the mirror image
    ("ex.example.com" -> "ex."), and bulk-add-domain's f"{prefix}.{base}" turns
    an empty base into "ex." the same way.
    """

    def _bulk_add(self, new_domain, client=None, **overrides):
        options = dict(new_domain=new_domain, host_ids="12", pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client",
                               lambda: client or _ExplodingClient()):
            npm_api.host_bulk_add_domain(**options)

    def _bulk_replace(self, old_domain, new_domain, client=None, **overrides):
        options = dict(old_domain=old_domain, new_domain=new_domain,
                       host_ids="12", pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client",
                               lambda: client or _ExplodingClient()):
            npm_api.host_bulk_replace_domain(**options)

    _BLANK = ("", " ", "\t", "   ")

    def test_bulk_add_refuses_a_blank_base_before_reading_anything(self):
        for value in self._BLANK:
            with self.subTest(value=value):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    self._bulk_add(value)
                self.assertEqual(caught.exception.exit_code, 1)
                self.assertPrinted("The new base domain is blank")

    def test_bulk_replace_refuses_a_blank_old_domain(self):
        for value in self._BLANK:
            with self.subTest(value=value):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    self._bulk_replace(value, "example.net")
                self.assertEqual(caught.exception.exit_code, 1)
                self.assertPrinted("The old base domain is blank")

    def test_bulk_replace_refuses_a_blank_new_domain(self):
        # Checked as well as --old, not instead of it: a script can lose either
        # variable, and only one of the two is guarded by the other's message.
        for value in self._BLANK:
            with self.subTest(value=value):
                with self.assertRaises(npm_api.typer.Exit) as caught:
                    self._bulk_replace("example.com", value)
                self.assertEqual(caught.exception.exit_code, 1)
                self.assertPrinted("The new base domain is blank")

    def test_the_refusal_does_not_depend_on_the_selector(self):
        # --pattern '*' is the widest selection the tool offers and the one an
        # unset OLD would most plausibly appear beside.
        with self.assertRaises(npm_api.typer.Exit):
            self._bulk_replace("", "example.net", host_ids=None, pattern="*")

    def test_a_real_pair_of_domains_still_runs(self):
        # The guard must not have made the command unusable: the ordinary
        # rewrite still reaches the API.
        client = self._client(_merge_host(12, ["ex.example.com"]), [])

        self._bulk_replace("example.com", "example.net", client=client)

        self.assertEqual(client.updates, [(12, {"domain_names": ["ex.example.net"]})])

    def test_a_real_base_still_adds(self):
        client = self._client(_merge_host(12, ["ex.example.com"]), [])

        self._bulk_add("example.net", client=client)

        self.assertEqual(
            client.updates,
            [(12, {"domain_names": ["ex.example.com", "ex.example.net"]})])


# =============================================================================
# the bulk domain commands: the world moving while the prompt is up
# =============================================================================

class TestBulkDomainCommandsConcurrentChange(_MergeCommandTestCase):
    """End to end for the three commands that route through apply_domain_changes.

    All three hand it a resulting_domains worked out before the confirmation
    prompt, and it is written as a full replacement — so before the guard, a
    domain added to the host while the prompt was up was erased on every one of
    them. Verified against each command rather than against the shared loop
    alone, because the loop can only check what the command chose to record: a
    command that forgot to carry its pre-prompt copy of the host would be
    guarded by a comparison against the host's own current state, which can
    never differ.
    """

    def _bulk_add(self, client, new_domain, **overrides):
        options = dict(new_domain=new_domain, host_ids="12", pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_add_domain(**options)

    def _bulk_remove(self, client, domain_pattern, **overrides):
        options = dict(domain_pattern=domain_pattern, host_ids="12", pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_remove_domain(**options)

    def _bulk_replace(self, client, old_domain, new_domain, **overrides):
        options = dict(old_domain=old_domain, new_domain=new_domain,
                       host_ids="12", pattern=None,
                       preview=False, yes=True, interactive=False)
        options.update(overrides)
        with mock.patch.object(npm_api, "get_client", lambda: client):
            npm_api.host_bulk_replace_domain(**options)

    def _changing_host(self, client, domains):
        """Replace host 12 with a copy carrying an extra domain.

        REPLACES the dict in the stub's host list rather than mutating it, for
        the same reason the merge tests do: the real get_host parses fresh JSON
        on every call, so a caller holding an earlier result holds an
        independent copy. Mutating the shared dict in place would hand the
        command the new value through a reference the real client never gives
        it, and the test would pass without the code being right.
        """
        def add_a_domain_while_the_prompt_is_up(*args, **kwargs):
            client.hosts[0] = _merge_host(
                12, list(domains) + ["added-during-prompt.example.com"])
        return add_a_domain_while_the_prompt_is_up

    def _run_expecting_exit(self, client, domains, run):
        with mock.patch.object(npm_api, "confirm_bulk",
                               self._changing_host(client, domains)):
            with self.assertRaises(npm_api.typer.Exit) as caught:
                run()
        return caught.exception

    def test_bulk_add_domain_does_not_overwrite_a_domain_added_meanwhile(self):
        domains = ["ex.example.com"]
        client = self._client(_merge_host(12, domains), [])

        exit_exception = self._run_expecting_exit(
            client, domains, lambda: self._bulk_add(client, "example.net"))

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.updates, [])
        self.assertPrinted("added-during-prompt.example.com")
        self.assertPrinted("Failed: 1")

    def test_bulk_remove_domain_does_not_overwrite_a_domain_added_meanwhile(self):
        domains = ["ex.example.com", "ex.example.net"]
        client = self._client(_merge_host(12, domains), [])

        exit_exception = self._run_expecting_exit(
            client, domains, lambda: self._bulk_remove(client, "example.net"))

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.updates, [])
        self.assertPrinted("added-during-prompt.example.com")

    def test_bulk_replace_domain_does_not_overwrite_a_domain_added_meanwhile(self):
        domains = ["ex.example.com"]
        client = self._client(_merge_host(12, domains), [])

        exit_exception = self._run_expecting_exit(
            client, domains,
            lambda: self._bulk_replace(client, "example.com", "example.net"))

        self.assertEqual(exit_exception.exit_code, 1)
        self.assertEqual(client.updates, [])
        self.assertPrinted("added-during-prompt.example.com")

    def test_an_unchanged_host_is_written_by_every_one_of_them(self):
        # The guard has to be invisible on the ordinary path.
        cases = (
            ("bulk-add-domain", ["ex.example.com"],
             lambda c: self._bulk_add(c, "example.net"),
             ["ex.example.com", "ex.example.net"]),
            ("bulk-remove-domain", ["ex.example.com", "ex.example.net"],
             lambda c: self._bulk_remove(c, "example.net"),
             ["ex.example.com"]),
            ("bulk-replace-domain", ["ex.example.com"],
             lambda c: self._bulk_replace(c, "example.com", "example.net"),
             ["ex.example.net"]),
        )
        for label, domains, run, expected in cases:
            with self.subTest(command=label):
                client = self._client(_merge_host(12, domains), [])

                run(client)

                self.assertEqual(client.updates, [(12, {"domain_names": expected})])

    def test_a_host_deleted_during_the_prompt_is_reported_as_gone(self):
        client = self._client(_merge_host(12, ["ex.example.com"]), [])

        def delete_the_host(*args, **kwargs):
            client.hosts = []

        with mock.patch.object(npm_api, "confirm_bulk", delete_the_host):
            with self.assertRaises(npm_api.typer.Exit):
                self._bulk_add(client, "example.net")

        self.assertEqual(client.updates, [])
        self.assertPrinted("no longer exists")


# =============================================================================
# get_client
# =============================================================================

class TestGetClient(_ConsoleTestCase):
    """The gate between an unconfigured install and the API.

    NPM ships with admin@example.com / changeme, so a config that still holds
    either is not merely incomplete — running against it is a login attempt on
    a real account with a published password.
    """

    def _get_client(self, config):
        """Call get_client with the module global reset and NPMClient stubbed.

        The global is patched rather than assigned so the caching this function
        does is undone when the test ends; NPMClient is stubbed because its
        real __init__ creates the token and backup directories on disk.
        """
        factory = mock.Mock(return_value=mock.sentinel.client)
        with mock.patch.object(npm_api, "_client", None), \
                mock.patch.object(npm_api.Config, "load", return_value=config), \
                mock.patch.object(npm_api, "NPMClient", factory):
            try:
                return npm_api.get_client(), factory, None
            except npm_api.typer.Exit as exc:
                return None, factory, exc

    def _configured(self):
        return npm_api.Config(api_user="ops@example.com", api_pass="s3cret")

    def test_default_credentials_refuse_before_a_client_is_built(self):
        _, factory, exit_exception = self._get_client(npm_api.Config())

        self.assertEqual(exit_exception.exit_code, 1)
        factory.assert_not_called()

    def test_the_refusal_lists_every_path_it_looked_in(self):
        # Without this the user has no way to tell that the file they wrote is
        # somewhere the tool never looks.
        config = npm_api.Config()
        config._searched_paths = ["/nowhere/npm-api.conf", "/elsewhere/npm-api.conf"]

        self._get_client(config)

        self.assertPrinted("/nowhere/npm-api.conf")
        self.assertPrinted("/elsewhere/npm-api.conf")

    def test_the_refusal_names_both_ways_to_configure_it(self):
        self._get_client(npm_api.Config())

        self.assertPrinted("Configuration Required")
        self.assertPrinted("NPM_API_USER")
        self.assertPrinted("npm-api.conf")

    def test_a_configured_install_builds_a_client_from_that_config(self):
        config = self._configured()

        client, factory, exit_exception = self._get_client(config)

        self.assertIsNone(exit_exception)
        self.assertIs(client, mock.sentinel.client)
        factory.assert_called_once_with(config)

    def test_the_client_is_built_once_and_reused(self):
        # Every command calls get_client(), and NPMClient.__init__ creates
        # directories and may fetch a token; doing that per call would turn one
        # bulk command into one authentication round trip per host.
        config = self._configured()
        factory = mock.Mock(return_value=mock.sentinel.client)

        with mock.patch.object(npm_api, "_client", None), \
                mock.patch.object(npm_api.Config, "load", return_value=config), \
                mock.patch.object(npm_api, "NPMClient", factory):
            first = npm_api.get_client()
            second = npm_api.get_client()

        self.assertIs(first, second)
        factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
