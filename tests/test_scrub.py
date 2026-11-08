"""Guardrail 3: credentials and customer PII never reach the store. The samples are fake."""

import json

from gieok import scrub
from gieok.ingest import Context
from gieok.ingest.transcripts import ingest_claude

PLANTED = {
    "openai-key": "sk-proj-" + "A1b2C3d4" * 6,
    "anthropic-key": "sk-ant-api03-" + "x9Y8z7W6" * 4,
    "stripe-key": "sk_live_" + "51Hq" * 6,
    "github-token": "ghp_" + "a1B2" * 9,
    "aws-key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "jwt": ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNry" * 3]),
    "private-key": "\n".join(["-----BEGIN OPENSSH " + "PRIVATE KEY-----", "b3BlbnNzaC1rZXktdjEAAAAA" + "fake" * 4,
                              "-----END OPENSSH " + "PRIVATE KEY-----"]),
}
PII = {"email": "jane.customer@example.org", "phone": "+1 415-555-0134", "card": "4242 4242 4242 4242",
       "ip": "203.0.113.42", "payment-id": "cus_NffrFeUfNV2Hib"}


def test_each_secret_kind_is_redacted_and_live_ones_flagged():
    for kind, value in PLANTED.items():
        r = scrub.scrub(f"here: {value} end")
        assert value not in r.text, kind
        assert f"[secret:{kind}]" in r.text, (kind, r.text)
    live = scrub.scrub(PLANTED["stripe-key"]).live
    assert live == ["stripe-key"]
    assert scrub.scrub(PLANTED["jwt"]).live == []


def test_assigned_secrets_keep_their_name():
    r = scrub.scrub('PASSWORD="hunter2hunter2" and api_key: zz99zz99zz99')
    assert "hunter2hunter2" not in r.text and "zz99zz99zz99" not in r.text
    assert 'PASSWORD="[secret:assigned-secret]' in r.text


def test_known_settings_values_are_caught_in_any_context(tmp_path):
    settings = tmp_path / "settings"
    settings.write_text("DISCORD_TOKEN=plain-looking-value-123\nGUILD_ID=12345\n")
    known = scrub.known_values([settings])
    assert known == ["plain-looking-value-123"]
    r = scrub.scrub("the bot token is plain-looking-value-123, guild 12345", known)
    assert "plain-looking-value-123" not in r.text and "12345" in r.text and r.live == ["known-value"]


def test_customer_pii_is_redacted_without_a_mapping():
    text = " ".join(PII.values()) + ' {"user_id": "u-8f2a", "first_name": "Jane", "name": "Bash"}'
    r = scrub.scrub(text)
    for value in [*PII.values(), "u-8f2a", "Jane"]:
        assert value not in r.text, value
    assert '"name": "Bash"' in r.text  # tool names survive
    assert set(r.pii) >= {"email", "phone", "card", "ip", "payment-id", "field"}


def test_transcript_structure_survives_scrubbing():
    numbers = '{"costUSD":0.4242424242424242,"uuid":"42424242-4242-4242-4242-424242424242"}'
    assert scrub.scrub(numbers).text == numbers
    line = json.dumps({"type": "assistant", "timestamp": "2026-11-08T20:42:29.801Z", "startedAtMs": 1794169224344,
                       "uuid": "01a1bce5-f628-7f40-b154-7a71834c333c", "version": "0.166.0",
                       "message": {"content": [{"type": "text", "text": "mail jane@example.org"}]}})
    out = scrub.scrub(line).text
    parsed = json.loads(out)
    assert parsed["startedAtMs"] == 1794169224344 and parsed["uuid"].startswith("01a1bce5")
    assert "jane@example.org" not in out


def test_planted_secrets_never_reach_a_pack(store, tmp_path):
    project = tmp_path / "projects" / "-home-monty-oscar"
    project.mkdir(parents=True)
    lines = [{"type": "user", "message": {"content": f"use {v} please"}} for v in [*PLANTED.values(), *PII.values()]]
    (project / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    report = ingest_claude(store, Context(host="h", home=tmp_path), projects=tmp_path / "projects")
    assert report.stored == 1 and report.redactions >= len(PLANTED) + len(PII)
    store.seal(force=True)
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    on_disk = b"".join(p.read_bytes() for p in store.root.rglob("*") if p.is_file())
    from gieok import codec
    packs = b"".join(codec.decode(p.read_bytes(), "pack") for p in (store.root / "packs").glob("*.pack"))
    for value in [*PLANTED.values(), *PII.values()]:
        token = value.split("\n")[1] if "\n" in value else value
        assert token.encode() not in on_disk and token.encode() not in packs, value
