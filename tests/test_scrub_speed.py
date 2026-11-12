"""The scrubber stays linear on adversarial 32 KB inputs (an agent's put runs it on whatever it sends)."""

import time

import pytest

from gieok import scrub

CASES = {
    "phone-like": "1-" * 16000, "ip-like": "1." * 16000, "digits-spaces": "1 " * 16000, "phone-groups": "12 123 " * 4700,
    "email-domain": "a@" + "1." * 16000, "email-dots": ".@" * 16000, "email-local": "a." * 16000 + "@x",
    "card-groups": "4111 " * 6600, "stripe": "cus_" * 8200, "pii-field": '"email":"' * 3600, "jwt": "eyJ" * 11000,
    "bearer": "bearer " * 4700, "cookie": "cookie: " * 4100, "assigned": "password=" * 3600, "token": "token:" * 5500,
    "discord": "M" + "a" * 32767, "pem-begins": "-----BEGIN PRIVATE KEY-----\n" * 1200,
    "pem-unterminated": "-----BEGIN RSA PRIVATE KEY----- " + "a" * 32000,
    "pem-begins-then-end": "-----BEGIN PRIVATE KEY-----" * 1200 + "-----END PRIVATE KEY-----", "dashes": "-" * 32768,
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_scrub_is_linear(name):
    text = CASES[name][:32 * 1024]
    t = time.perf_counter()
    scrub.scrub(text)
    assert time.perf_counter() - t < 0.2


def test_bounded_patterns_still_catch_real_things():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n" + ("A+/b" * 16 + "\n") * 100 + \
          "-----END RSA PRIVATE KEY-----"
    assert scrub.scrub(f"key {pem} end").text == "key [secret:private-key] end"
    assert scrub.scrub("mail john.smith@example.com, x.y+z@ex.co").text == "mail [redacted], [redacted]"
    assert scrub.scrub("call +44 20 7946 0958 or 10.1.2.3").text == "call [redacted] or [redacted]"
