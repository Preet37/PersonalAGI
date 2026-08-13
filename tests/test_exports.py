"""Export adapters, and the provenance rule they exist to enforce.

An assistant transcript is the first source where ONE FILE contains both real
records and generated text. What the owner typed is evidence; what the model
replied is not, and if it were citable the system could ground a confident
claim in an answer some model invented — the self-citation bug arriving
through a new door.
"""

import json

import pytest

from personalagi.adapters.exports import (
    ExportError,
    detect_format,
    parse_export,
    parse_linkedin_messages,
    parse_whatsapp_export,
)
from personalagi.records import Provenance

OWNER = {"preet@example.com"}


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(
        content if isinstance(content, str) else json.dumps(content),
        encoding="utf-8",
    )
    return path


CLAUDE = [
    {
        "uuid": "conv-1",
        "name": "DJ and the programme",
        "created_at": "2026-08-10T09:00:00Z",
        "chat_messages": [
            {"sender": "human", "text": "What did DJ say about Anthropic?"},
            {"sender": "assistant", "text": "DJ is the connection to Anthropic."},
        ],
    }
]

CHATGPT = [
    {
        "id": "c2",
        "title": "Sponsors",
        "create_time": 1786000000,
        "mapping": {
            "n1": {"message": {"author": {"role": "user"},
                               "content": {"parts": ["Who should I ask?"]}}},
            "n2": {"message": {"author": {"role": "assistant"},
                               "content": {"parts": ["Try Deepgram."]}}},
        },
    }
]


class TestProvenanceRule:
    """The reason these adapters were worth building carefully."""

    def test_owner_turns_are_external_and_citable(self, tmp_path):
        path = write(tmp_path, "claude_export.json", CLAUDE)

        result = parse_export(path, owner_addresses=OWNER)
        owner_turn = next(r for r in result.records if "What did DJ" in r.text)

        assert owner_turn.provenance == Provenance.EXTERNAL

    def test_assistant_turns_are_generated_and_never_citable(self, tmp_path):
        """If these were citable the system could cite an answer a model
        invented as evidence for a claim it then makes."""
        path = write(tmp_path, "claude_export.json", CLAUDE)

        result = parse_export(path, owner_addresses=OWNER)
        reply = next(r for r in result.records if "DJ is the connection" in r.text)

        assert reply.provenance == Provenance.GENERATED

    def test_the_split_is_counted_and_reported(self, tmp_path):
        path = write(tmp_path, "claude_export.json", CLAUDE)

        result = parse_export(path, owner_addresses=OWNER)

        assert result.owner_turns == 1
        assert result.assistant_turns == 1
        assert "never citable" in result.summary()

    def test_provenance_survives_into_the_stored_row(self, tmp_path):
        path = write(tmp_path, "claude_export.json", CLAUDE)
        result = parse_export(path, owner_addresses=OWNER)
        reply = next(r for r in result.records if r.provenance == Provenance.GENERATED)

        from datetime import datetime

        row = reply.as_row(datetime(2026, 8, 13))

        assert row["provenance"] == Provenance.GENERATED

    def test_a_non_assistant_source_stays_external(self, tmp_path):
        path = write(tmp_path, "chat.txt", "13/08/2026, 14:32 - Karan: hey there\n")

        result = parse_export(path, owner_addresses=OWNER)

        assert result.records[0].provenance == Provenance.EXTERNAL


class TestAssistantFormats:
    def test_claude_shape(self, tmp_path):
        path = write(tmp_path, "claude_export.json", CLAUDE)

        result = parse_export(path, owner_addresses=OWNER)

        assert result.conversations == 1
        assert result.events == 2
        assert result.records[0].thread_key == "conv-1"

    def test_chatgpt_mapping_shape(self, tmp_path):
        """ChatGPT ships a dict of nodes keyed by id, not a list."""
        path = write(tmp_path, "chatgpt_export.json", CHATGPT)

        result = parse_export(path, owner_addresses=OWNER)

        assert result.events == 2
        assert any("Deepgram" in r.text for r in result.records)

    def test_content_as_a_list_of_parts_is_flattened(self, tmp_path):
        path = write(tmp_path, "gemini_export.json", [
            {"id": "g1", "messages": [{"role": "user", "content": ["a", "b"]}]}
        ])

        result = parse_export(path, owner_addresses=OWNER)

        assert result.records[0].text == "a\nb"

    def test_empty_turns_are_skipped_not_stored(self, tmp_path):
        path = write(tmp_path, "claude_export.json", [
            {"uuid": "c", "chat_messages": [
                {"sender": "human", "text": ""},
                {"sender": "human", "text": "real"},
            ]}
        ])

        result = parse_export(path, owner_addresses=OWNER)

        assert result.events == 1
        assert result.skipped_empty == 1

    def test_turns_within_a_conversation_share_a_thread(self, tmp_path):
        path = write(tmp_path, "claude_export.json", CLAUDE)

        result = parse_export(path, owner_addresses=OWNER)

        assert len({r.thread_key for r in result.records}) == 1

    def test_source_ids_are_unique_per_turn(self, tmp_path):
        path = write(tmp_path, "claude_export.json", CLAUDE)

        result = parse_export(path, owner_addresses=OWNER)

        assert len({r.source_id for r in result.records}) == 2


class TestWhatsApp:
    def test_a_basic_line_parses(self, tmp_path):
        path = write(
            tmp_path, "WhatsApp Chat with Karan.txt",
            "13/08/2026, 14:32 - Karan: did you send the deck?\n",
        )

        result = parse_whatsapp_export(path, owner_addresses=OWNER)

        assert result.events == 1
        assert "did you send the deck?" in result.records[0].text

    def test_a_multiline_message_keeps_its_later_paragraphs(self, tmp_path):
        """Continuation lines do not start with a timestamp. Dropping them
        loses every paragraph after the first."""
        path = write(
            tmp_path, "chat.txt",
            "13/08/2026, 14:32 - Karan: first line\nsecond line\nthird line\n",
        )

        result = parse_whatsapp_export(path, owner_addresses=OWNER)

        assert result.records[0].text == "first line\nsecond line\nthird line"

    def test_system_lines_do_not_become_messages(self, tmp_path):
        path = write(
            tmp_path, "chat.txt",
            "13/08/2026, 14:30 - Karan: Messages are end-to-end encrypted\n"
            "13/08/2026, 14:32 - Karan: real message\n",
        )

        result = parse_whatsapp_export(path, owner_addresses=OWNER)

        assert result.events == 1

    @pytest.mark.parametrize(
        "line",
        [
            "13/08/2026, 14:32 - Karan: hi",
            "[13/08/2026, 14:32:01] Karan: hi",
            "8/13/26, 2:32 PM - Karan: hi",
        ],
    )
    def test_the_common_export_variants_all_parse(self, tmp_path, line):
        path = write(tmp_path, "chat.txt", line + "\n")

        assert parse_whatsapp_export(path, owner_addresses=OWNER).events == 1

    def test_the_display_name_becomes_the_person_slug(self, tmp_path):
        """WhatsApp exports carry names, not numbers -- which is what lets one
        person's WhatsApp and email land in the same file."""
        path = write(tmp_path, "chat.txt", "13/08/2026, 14:32 - Karan Gupta: hi\n")

        result = parse_whatsapp_export(path, owner_addresses=OWNER)

        assert result.records[0].participants[0].person_slug == "karan-gupta"

    def test_an_unparseable_file_fails_loudly(self, tmp_path):
        """Silently importing zero messages looks exactly like an empty chat."""
        path = write(tmp_path, "chat.txt", "this is not a whatsapp export at all\n")

        with pytest.raises(ExportError, match="no WhatsApp messages"):
            parse_whatsapp_export(path, owner_addresses=OWNER)


class TestLinkedIn:
    def test_messages_parse(self, tmp_path):
        path = write(
            tmp_path, "messages.csv",
            "CONVERSATION ID,FROM,DATE,SUBJECT,CONTENT\n"
            "c1,Arjun Sambamoorthy,2026-08-13 03:54:00,,I want to connect\n",
        )

        result = parse_linkedin_messages(path, owner_addresses=OWNER)

        assert result.events == 1
        assert result.records[0].participants[0].person_slug == "arjun-sambamoorthy"

    def test_this_is_the_source_that_makes_linkedin_people_reachable(self, tmp_path):
        """Connection requests live here and nowhere else, which is exactly why
        the shared-envelope workaround was needed on the Gmail side."""
        path = write(
            tmp_path, "messages.csv",
            "CONVERSATION ID,FROM,DATE,SUBJECT,CONTENT\n"
            "c1,Arjun Sambamoorthy,2026-08-13,,hello\n",
        )

        result = parse_linkedin_messages(path, owner_addresses=OWNER)

        assert result.records[0].source == "linkedin"
        assert result.records[0].participants[0].address.endswith("@linkedin.local")

    def test_a_wrong_csv_fails_loudly(self, tmp_path):
        path = write(tmp_path, "messages.csv", "A,B\n1,2\n")

        with pytest.raises(ExportError, match="CONTENT column"):
            parse_linkedin_messages(path, owner_addresses=OWNER)

    def test_empty_rows_are_skipped(self, tmp_path):
        path = write(
            tmp_path, "messages.csv",
            "CONVERSATION ID,FROM,DATE,CONTENT\nc1,X,2026-08-13,\n"
            "c1,X,2026-08-13,real\n",
        )

        result = parse_linkedin_messages(path, owner_addresses=OWNER)

        assert result.events == 1
        assert result.skipped_empty == 1


class TestDetection:
    def test_txt_is_whatsapp(self, tmp_path):
        assert detect_format(write(tmp_path, "x.txt", "a")) == "whatsapp"

    def test_csv_is_linkedin(self, tmp_path):
        assert detect_format(write(tmp_path, "x.csv", "a")) == "linkedin"

    @pytest.mark.parametrize(
        "name,expected",
        [("claude_export.json", "claude"), ("chatgpt-data.json", "chatgpt"),
         ("gemini_takeout.json", "gemini")],
    )
    def test_the_name_identifies_assistant_exports(self, tmp_path, name, expected):
        assert detect_format(write(tmp_path, name, [])) == expected

    def test_shape_identifies_it_when_the_name_does_not(self, tmp_path):
        path = write(tmp_path, "export.json", CLAUDE)

        assert detect_format(path) == "claude"

    def test_an_unidentifiable_file_says_what_to_do(self, tmp_path):
        path = write(tmp_path, "export.json", [{"nothing": "recognisable"}])

        with pytest.raises(ExportError, match="Rename it"):
            detect_format(path)

    def test_an_unsupported_extension_is_refused(self, tmp_path):
        with pytest.raises(ExportError, match="unsupported"):
            detect_format(write(tmp_path, "x.pdf", "a"))
