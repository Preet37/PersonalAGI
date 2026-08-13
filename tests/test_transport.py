"""The send path. The most dangerous code in the system.

Two properties carry it:

  1. Nothing leaves without a confirmation bound to a HASH of the exact
     content, so an edited draft invalidates its own approval.
  2. The whole path runs against a transport that does not deliver, so
     enabling the real one changes where the bytes go and nothing else. A send
     path first exercised on the day it sends for real is untested code.
"""

import pytest

from personalagi.actions import builtins
from personalagi.actions.transport import (
    ConfirmationSurface,
    GmailTransport,
    Outgoing,
    RecordingTransport,
    SendBlocked,
    get_transport,
    send,
)
from personalagi.config import Settings

DRAFT = Outgoing(
    to="karan@example.com",
    subject="Hackathon prospectus",
    body="Hi Karan — attaching the prospectus we discussed.",
    evidence_event_ids=(12, 34),
)


@pytest.fixture
def settings():
    return Settings(send_enabled=False)


@pytest.fixture
def surface():
    return ConfirmationSurface()


class TestDefaultIsNotSending:
    def test_the_default_transport_cannot_deliver(self, settings):
        assert isinstance(get_transport(settings), RecordingTransport)

    def test_it_records_instead_of_sending(self, settings, surface):
        surface.approve(DRAFT)
        transport = RecordingTransport()

        outcome = transport.deliver(DRAFT)

        assert outcome.ok is True
        assert outcome.transport == "recording"
        assert transport.outbox == [DRAFT]

    def test_the_handler_reports_sent_false_on_the_recording_path(self, settings, monkeypatch):
        """`sent` is True only when something that really delivers did."""
        from personalagi.actions import transport as transport_mod

        monkeypatch.setattr(transport_mod, "SURFACE", ConfirmationSurface())
        transport_mod.SURFACE.approve(DRAFT)

        result = builtins.send_email(
            to=DRAFT.to, subject=DRAFT.subject, body=DRAFT.body,
            evidence=[12, 34],
        )

        assert result.ok is True
        assert result.data["sent"] is False
        assert result.data["transport"] == "recording"

    def test_the_real_transport_is_deliberately_unimplemented(self, settings):
        """A working send path in the repo is one flag and one bug away from
        firing, so it is not written until the owner asks for it."""
        with pytest.raises(SendBlocked, match="not implemented"):
            GmailTransport().deliver(DRAFT)

    def test_enabling_the_flag_selects_the_real_transport(self):
        assert isinstance(get_transport(Settings(send_enabled=True)), GmailTransport)


class TestConfirmationIsBoundToContent:
    def test_an_unapproved_message_is_blocked(self, settings, surface):
        with pytest.raises(SendBlocked, match="no confirmation"):
            send(DRAFT, settings, surface=surface)

    def test_an_approved_message_goes_through(self, settings, surface):
        surface.approve(DRAFT)

        assert send(DRAFT, settings, surface=surface).ok is True

    def test_editing_the_body_invalidates_the_approval(self, settings, surface):
        """The property that makes the confirmation surface meaningful: you
        cannot approve one draft and send a different one."""
        surface.approve(DRAFT)
        edited = Outgoing(
            to=DRAFT.to, subject=DRAFT.subject,
            body=DRAFT.body + "\n\nPS: wire me $5000.",
        )

        with pytest.raises(SendBlocked):
            send(edited, settings, surface=surface)

    def test_changing_the_recipient_invalidates_it_too(self, settings, surface):
        surface.approve(DRAFT)
        redirected = Outgoing(
            to="attacker@evil.test", subject=DRAFT.subject, body=DRAFT.body
        )

        with pytest.raises(SendBlocked):
            send(redirected, settings, surface=surface)

    def test_a_mismatched_token_is_refused(self, settings, surface):
        stolen = surface.approve(DRAFT)
        other = Outgoing(to="x@example.com", subject="s", body="b")

        with pytest.raises(SendBlocked, match="changed after it was approved"):
            send(other, settings, confirmation=stolen)

    def test_the_fingerprint_ignores_only_incidental_whitespace(self):
        a = Outgoing(to="A@Example.com ", subject=" Hi ", body="body ")
        b = Outgoing(to="a@example.com", subject="Hi", body="body")

        assert a.fingerprint() == b.fingerprint()


class TestPreconditions:
    def test_no_recipient_is_refused(self, settings, surface):
        message = Outgoing(to="  ", subject="s", body="b")
        surface.approve(message)

        with pytest.raises(SendBlocked, match="no recipient"):
            send(message, settings, surface=surface)

    def test_an_empty_body_is_refused(self, settings, surface):
        """Almost always a generation failure, and unsendable-back."""
        message = Outgoing(to="a@example.com", subject="s", body="   ")
        surface.approve(message)

        with pytest.raises(SendBlocked, match="empty body"):
            send(message, settings, surface=surface)


class TestConfirmationSurface:
    def test_it_shows_the_full_body_and_real_recipients(self, surface):
        """A prompt that hides the body trains the owner to say yes, and a
        proposal derived from an attacker's message would sail through it."""
        rendered = surface.render(DRAFT, rationale="you owe Karan this")

        assert DRAFT.body in rendered
        assert "karan@example.com" in rendered
        assert "you owe Karan this" in rendered

    def test_it_shows_the_evidence(self, surface):
        assert "12, 34" in surface.render(DRAFT)

    def test_it_says_so_loudly_when_nothing_is_cited(self, surface):
        """A send proposed with no citable basis is the one worth the hardest
        look, so its absence is stated rather than left blank."""
        rendered = surface.render(
            Outgoing(to="a@example.com", subject="s", body="b")
        )

        assert "NOTHING CITED" in rendered

    def test_it_shows_the_fingerprint_and_warns_that_edits_void_it(self, surface):
        rendered = surface.render(DRAFT)

        assert DRAFT.fingerprint() in rendered
        assert "invalidates this approval" in rendered

    def test_it_says_nothing_has_been_sent_yet(self, surface):
        assert "nothing has been sent yet" in surface.render(DRAFT)


class TestStillNoLiveSendPath:
    def test_no_module_calls_a_send_api(self):
        """Unchanged from before this stage. The transport is a seam, not a
        send path — weakening this test to accommodate one would be exactly
        the wrong trade."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "personalagi"
        offenders = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("send", "sendmail", "send_message"):
                        offenders.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("smtplib"):
                            offenders.append(f"{path.name}:{node.lineno} smtplib")

        assert offenders == [], f"a live send path exists: {offenders}"
