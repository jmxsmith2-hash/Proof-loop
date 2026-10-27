import logging

import pytest

from synthux.core.config import AllowedHost, SafetyConfig
from synthux.safety.allowlist import SafetyError, check_url, match_host
from synthux.safety.credentials import CredentialVault, RedactingFilter
from synthux.safety.guard import ActionGuard
from synthux.safety.network import RequestPolicy

ALLOW = [AllowedHost(host="127.0.0.1", ports=[8765], sandbox=True),
         AllowedHost(host="*.staging.example.com", sandbox=False)]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/signup",
    "https://app.staging.example.com/x",
])
def test_allowed_urls(url):
    assert check_url(url, ALLOW)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9999/",                 # wrong port
    "https://example.com/",                   # not listed
    "https://staging.example.com.evil.io/",   # suffix trick
    "http://127.0.0.1:8765@evil.io/",         # userinfo trick
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://127.0.0.1:8765/",
])
def test_refused_urls(url):
    with pytest.raises(SafetyError):
        check_url(url, ALLOW)


def test_sandbox_flag_comes_from_allowlist():
    assert match_host("http://127.0.0.1:8765/", ALLOW).sandbox is True
    assert match_host("https://a.staging.example.com/", ALLOW).sandbox is False


def _safety():
    return SafetyConfig(allowlist=ALLOW, blocked_request_patterns=[r"stripe\.com", r"/charge"],
                        payment_patterns=[r"place order", r"\bpay\b"], email_patterns=[r"\bsend\b"],
                        destructive_patterns=[r"delete"])


def test_request_policy_blocks_navigation_and_offsite_writes():
    policy = RequestPolicy(_safety(), sandbox=True)
    assert not policy.decide("https://evil.io/", is_navigation=True).allow
    assert not policy.decide("https://cdn.evil.io/beacon", "POST").allow
    assert policy.decide("https://fonts.example.org/font.woff2").allow  # off-site GET subresource ok
    assert policy.decide("about:blank", is_navigation=True).allow


def test_request_policy_blocks_payment_providers_unless_sandbox():
    strict = RequestPolicy(_safety(), sandbox=False)
    assert not strict.decide("https://js.stripe.com/v3").allow
    assert not strict.decide("https://a.staging.example.com/api/charge", "POST").allow
    relaxed = RequestPolicy(_safety(), sandbox=True)
    assert relaxed.decide("https://js.stripe.com/v3").allow
    assert len(strict.blocked_log) == 2


def test_action_guard():
    strict = ActionGuard(_safety(), sandbox=False)
    assert not strict.check("click", {"tag": "button", "text": "Place order"}).allowed
    assert not strict.check("click", {"tag": "button", "text": "Delete account"}).allowed
    assert not strict.check("press_enter", {"tag": "input", "submitTexts": ["Send"]}).allowed
    assert strict.check("click", {"tag": "button", "text": "Continue"}).allowed
    assert strict.check("type", {"tag": "input", "text": "Pay"}).allowed  # typing is not a submission
    sandbox = ActionGuard(_safety(), sandbox=True)
    res = sandbox.check("click", {"tag": "button", "text": "Pay now"})
    assert res.allowed and res.category == "payment"


def test_credentials_are_redacted_in_logs(caplog):
    vault = CredentialVault({"password": "s3cr3t-pw"})
    assert vault.resolve("{{cred:password}}") == "s3cr3t-pw"
    with pytest.raises(KeyError):
        vault.resolve("{{cred:missing}}")
    logger = logging.getLogger("sx-test-redact")
    logger.addFilter(RedactingFilter(vault))
    with caplog.at_level(logging.INFO, logger="sx-test-redact"):
        logger.info("typed %s into field", "s3cr3t-pw")
    assert "s3cr3t-pw" not in caplog.text and "[secret]" in caplog.text
