"""
Direct unit tests for core/dlp.py's SECOND, independent detection layer -
closes a gap found during the 2026-09-29 security-claims audit. The
module's docstring claims two layers: gitleaks-style regex patterns for
common secret SHAPES (already covered by
tests/unit/test_core_execute.py::test_dlp_refuses_outgoing_content_with_a_hidden_api_key,
an sk-... pattern) AND a direct substring check against every currently-
configured secret VALUE (core.dlp._configured_secret_values/_SECRET_ENV_VARS)
- so a literal leak of this deployment's OWN SMTP_PASSWORD or
GATEWAY_INTERNAL_TOKEN is caught even though it matches no generic shape.
That second layer had zero test coverage anywhere before this file.
"""

from __future__ import annotations

from core import dlp


def test_configured_secret_value_is_caught_even_with_no_recognizable_shape(monkeypatch):
    # A value with none of the generic shapes (sk-, AKIA, xox, ghp_, PEM,
    # nvapi-) - the ONLY way this gets caught is the configured-value
    # substring check, not the regex-pattern layer.
    monkeypatch.setenv("SMTP_PASSWORD", "correct-horse-battery-staple-9x2")
    findings = dlp.scan_text("oh btw the password is correct-horse-battery-staple-9x2, don't tell anyone")
    assert any("SMTP_PASSWORD" in f.label for f in findings)


def test_configured_secret_value_not_flagged_when_absent_from_text(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "correct-horse-battery-staple-9x2")
    findings = dlp.scan_text("nothing sensitive in here at all")
    assert findings == []


def test_short_configured_value_is_never_substring_matched(monkeypatch):
    """_MIN_CONFIGURED_SECRET_LEN guards against a short, common value (a
    short OWNER_NAME-like string) causing false positives via bare
    substring matching - e.g. a 3-character value would match almost
    anything."""
    monkeypatch.setenv("SMTP_PASSWORD", "ab")  # shorter than _MIN_CONFIGURED_SECRET_LEN
    findings = dlp.scan_text("I saw this ab over there")
    assert findings == []


def test_scan_params_only_scans_string_values_not_numbers_or_symbols(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "correct-horse-battery-staple-9x2")
    findings = dlp.scan_params({"amount": 123.45, "symbol": "EURUSD", "note": "clean text"})
    assert findings == []
