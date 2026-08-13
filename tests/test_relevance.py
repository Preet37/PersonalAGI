"""Stage A/B relevance, and the three invariants that keep it honest.

1. Stage A is asymmetric: any single signal proves automated, but proving
   human requires having looked at headers.
2. A message must never be retrieved as context for itself.
3. A commitment quote must actually appear in the message.
"""

from datetime import datetime

import pytest

from personalagi.identity import automated_by_header, classify_sender
from personalagi.llm.relevance import _quote_is_grounded, direction_for, what_hash
from personalagi.llm.schemas import CommitmentOut, RelevanceOut

CLEAN = {"to": "preet@example.com", "message-id": "<a@b>"}


class TestStageAHeaders:
    def test_list_unsubscribe_beats_a_clean_address(self):
        """The signal the local-part regex structurally cannot reach.

        uber@uber.com has no robot token in it — the brand IS the local part —
        so the regex called it human. On the real corpus List-Unsubscribe
        caught 2,827 messages against the regex's 705.
        """
        verdict = classify_sender("uber@uber.com", {"list-unsubscribe": "<mailto:x>"})

        assert verdict.is_human is False
        assert "list-unsubscribe" in verdict.reason

    def test_the_same_address_without_the_header_reads_as_human(self):
        assert classify_sender("uber@uber.com", CLEAN).is_human is True

    def test_precedence_bulk_is_machine_mail(self):
        assert classify_sender("news@x.com", {"precedence": "bulk"}).is_human is False

    def test_auto_submitted_no_means_a_human_sent_it(self):
        """RFC 3834: 'no' is the explicit human marker, not a bulk one."""
        assert automated_by_header({"auto-submitted": "no"}) is None
        assert automated_by_header({"auto-submitted": "auto-generated"}) is not None

    def test_header_names_are_matched_case_insensitively(self):
        assert automated_by_header({"List-Unsubscribe": "<mailto:x>"}) == "list-unsubscribe"

    def test_empty_header_values_do_not_count(self):
        assert automated_by_header({"list-unsubscribe": ""}) is None


class TestStageAIsAsymmetric:
    def test_missing_headers_never_prove_human(self):
        """The trap this exists to avoid.

        Adding headers_json defaulted 4,000 existing rows to NULL. If absence
        of a bulk marker counted as evidence of a person, the whole corpus
        would have silently promoted itself to human the day the column landed.
        """
        verdict = classify_sender("dana@example.com", {})

        assert verdict.is_human is True
        assert verdict.confident is False  # <- the part that matters
        assert verdict.needs_llm is True

    def test_headers_present_makes_the_human_verdict_confident(self):
        assert classify_sender("dana@example.com", CLEAN).confident is True

    def test_a_robot_address_is_confident_without_any_headers(self):
        verdict = classify_sender("no-reply@x.com", {})

        assert verdict.is_human is False
        assert verdict.confident is True  # negative evidence needs no headers

    def test_headers_outrank_a_clean_address_and_a_clean_shared_set(self):
        verdict = classify_sender(
            "ceo@realstartup.com", {"list-id": "<news.realstartup.com>"}, set()
        )
        assert verdict.is_human is False

    def test_shared_envelope_catches_what_the_regex_misses(self):
        """`connect@brand.com` has no robot token and no bulk header, so only
        the many-display-names rule can catch it. This is the shape of
        LinkedIn's invitations address: 215 messages, 215 different names."""
        verdict = classify_sender("connect@brand.com", CLEAN, {"connect@brand.com"})

        assert classify_sender("connect@brand.com", CLEAN).is_human is True  # alone
        assert verdict.is_human is False
        assert "shared" in verdict.reason


class TestCommitmentDirection:
    """A truth table, which is exactly why the model is not asked for it."""

    @pytest.mark.parametrize(
        "promiser,owner_wrote_it,expected",
        [
            ("author", True, "i_owe"),
            ("author", False, "they_owe"),
            ("recipient", True, "they_owe"),
            ("recipient", False, "i_owe"),
        ],
    )
    def test_all_four_cases(self, promiser, owner_wrote_it, expected):
        assert direction_for(promiser, owner_wrote_it) == expected

    def test_the_case_that_motivated_it(self):
        """Owner's own sent mail.

        Here "the sender promised it" and "the owner promised it" are the same
        sentence, so a model asked for i_owe/they_owe directly has no way to be
        consistently right. Asked who wrote it, it cannot go wrong.
        """
        assert direction_for("author", True) == "i_owe"
        assert direction_for("recipient", True) == "they_owe"


class TestCounterparty:
    """Who the commitment is WITH — not who sent the message."""

    OWNER = {"preet@example.com"}

    def view(self, sender: str, to=(), cc=()):
        from tests.factories import make_view

        return make_view(
            sender="Someone",
            email=sender,
            to=list(to),
            cc=list(cc),
            owner_addresses=self.OWNER,
        )

    @staticmethod
    def resolve(view):
        other = view.counterparty
        return (other.display_name if other else "", other.address if other else "")

    def test_on_received_mail_it_is_the_sender(self):
        view = self.view("karan@example.com", to=[("", "preet@example.com")])

        assert self.resolve(view)[1] == "karan@example.com"

    def test_on_sent_mail_it_is_the_recipient_not_the_owner(self):
        """The bug the first real `owed` run exposed.

        Every commitment came back attributed to "Preet Karia owes Preet
        Karia", because the extractor took sender_email unconditionally and on
        sent mail the sender is the owner.
        """
        view = self.view("preet@example.com", to=[("Karan G", "karan@example.com")])
        name, email = self.resolve(view)

        assert email == "karan@example.com"
        assert name == "Karan G"

    def test_the_owner_is_skipped_when_they_are_also_a_recipient(self):
        view = self.view(
            "preet@example.com",
            to=[("", "preet@example.com"), ("", "daniel@example.com")],
        )

        assert self.resolve(view)[1] == "daniel@example.com"

    def test_it_falls_back_to_cc(self):
        view = self.view(
            "preet@example.com",
            to=[("", "preet@example.com")],
            cc=[("", "sisi@x.com")],
        )

        assert self.resolve(view)[1] == "sisi@x.com"

    def test_a_note_to_self_yields_nobody_rather_than_the_owner(self):
        """Blank is honest; attributing it to the owner is a false claim."""
        view = self.view("preet@example.com", to=[("", "preet@example.com")])

        assert self.resolve(view) == ("", "")


class TestQuoteGrounding:
    BODY = "Hi Preet,\n\nI'll send you the sponsor deck by Friday.\n\nThanks,\nKaran"

    def test_a_real_quote_survives(self):
        assert _quote_is_grounded("I'll send you the sponsor deck by Friday.", self.BODY)

    def test_whitespace_and_case_differences_are_tolerated(self):
        """The body has been through HTML stripping; exact matching would
        reject valid quotes for cosmetic reasons."""
        assert _quote_is_grounded("i'll send you   the SPONSOR deck by friday.", self.BODY)

    def test_a_paraphrase_is_rejected(self):
        """The failure that would make this feature worse than useless.

        Showing someone a sentence they never wrote destroys the one thing
        commitment tracking sells: that you can prove it.
        """
        assert not _quote_is_grounded("I will send the deck on Friday.", self.BODY)

    def test_a_fabricated_quote_is_rejected(self):
        assert not _quote_is_grounded("I promise to wire you $5000 tomorrow.", self.BODY)

    def test_an_empty_quote_is_rejected(self):
        assert not _quote_is_grounded("", self.BODY)

    def test_a_trivially_short_quote_is_rejected(self):
        """"ok" appears in almost any body and evidences nothing."""
        assert not _quote_is_grounded("deck", self.BODY)


class TestSchemas:
    def test_relevance_accepts_a_word_where_an_integer_was_asked_for(self):
        assert RelevanceOut.model_validate({"relevance": "high", "why": "x"}).relevance == 3

    def test_relevance_is_clamped_to_the_declared_range(self):
        with pytest.raises(ValueError):
            RelevanceOut.model_validate({"relevance": 9, "why": "x"})

    def test_a_malformed_commitment_does_not_cost_the_relevance_score(self):
        """Two independent findings share one call for cost reasons; one being
        malformed must not tombstone the other."""
        out = RelevanceOut.model_validate(
            {
                "relevance": 3,
                "why": "x",
                "commitments": [
                    {"promiser": "author", "what": "send deck", "quote": "q"},
                    {"promiser": "author"},  # no `what` -> dropped
                    "not even an object",
                ],
            }
        )

        assert out.relevance == 3
        assert len(out.commitments) == 1

    def test_promiser_aliases_are_normalised(self):
        assert CommitmentOut.model_validate(
            {"promiser": "sender", "what": "x"}
        ).promiser == "author"

    def test_what_hash_is_stable_across_whitespace_and_case(self):
        assert what_hash("Send  the DECK") == what_hash("send the deck")

    def test_what_hash_separates_different_obligations(self):
        assert what_hash("send the deck") != what_hash("send the invoice")


class TestSelfCitation:
    """A message must not be retrievable as evidence about itself."""

    def test_excluding_the_source_message_empties_a_single_entry_log(self, tmp_path):
        from personalagi.config import Settings
        from personalagi.context.people import LogEntry, PersonFile, save_person
        from personalagi.context.retrieve import get_context
        from personalagi.db import init_db

        settings = Settings(database_url=f"sqlite:///{tmp_path / 'x.db'}")
        context_dir = tmp_path / "context"
        person = PersonFile(slug="groq", name="Groq")
        person.log.append(
            LogEntry(datetime(2026, 8, 13).date(), "Groq warns of decommission", "abc123")
        )
        save_person(context_dir, person)
        init_db(settings)

        with_self = get_context(
            "groq", "decommission", settings, context_dir=context_dir
        )
        without_self = get_context(
            "groq",
            "decommission",
            settings,
            context_dir=context_dir,
            exclude_source_ids={"abc123"},
        )

        assert with_self.total_log_lines == 1
        assert without_self.total_log_lines == 0
        assert without_self.log_lines == []


class TestContextIsAboutTheCounterparty:
    """On the owner's own outgoing messages, the sender IS the owner.

    Retrieving context for the sender there retrieves context about the owner,
    and the model justified maximum relevance with "SENDER block shows sender
    is Preet Karia, the owner himself" — circular reasoning that inflated every
    sent message to a 3. The person whose history explains an outgoing message
    is the person it was sent TO.
    """

    def test_an_owner_sent_message_does_not_retrieve_the_owner(self, tmp_path):
        from personalagi.config import Settings
        from personalagi.db import init_db
        from personalagi.llm.relevance import NO_CONTEXT, render_context
        from tests.factories import make_view

        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'r.db'}", context_dir=tmp_path / "ctx"
        )
        init_db(settings)
        view = make_view(
            email="preet@example.com",
            to=[("", "preet@example.com")],  # note to self: no counterparty
            owner_addresses={"preet@example.com"},
        )

        text, slug, tokens = render_context(view, settings)

        assert text == NO_CONTEXT
        assert slug == ""

    def test_it_looks_up_the_recipient_on_sent_mail(self, tmp_path):
        from personalagi.config import Settings
        from personalagi.context.people import LogEntry, PersonFile, save_person
        from personalagi.db import init_db
        from personalagi.llm.relevance import render_context
        from tests.factories import make_view

        context_dir = tmp_path / "ctx"
        person = PersonFile(slug="karan-g", name="Karan G", profile="Works at Deepgram.")
        person.log.append(LogEntry(datetime(2026, 8, 1).date(), "talked sponsorship", "x1"))
        save_person(context_dir, person)

        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'r.db'}", context_dir=context_dir
        )
        init_db(settings)
        view = make_view(
            email="preet@example.com",
            to=[("Karan G", "karan@example.com")],
            owner_addresses={"preet@example.com"},
        )

        text, slug, _ = render_context(view, settings)

        assert slug == "karan-g"
        assert "Deepgram" in text
