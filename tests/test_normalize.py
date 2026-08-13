import base64

from personalagi.ingest import normalize


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class TestParseSender:
    def test_name_and_email(self):
        assert normalize.parse_sender("Dana Okafor <dana@example.com>") == (
            "Dana Okafor",
            "dana@example.com",
        )

    def test_bare_address_gets_readable_name(self):
        assert normalize.parse_sender("dana.okafor@example.com") == (
            "Dana Okafor",
            "dana.okafor@example.com",
        )

    def test_encoded_word_name(self):
        name, email = normalize.parse_sender("=?UTF-8?B?RGFuYSDDlmthZm9y?= <dana@example.com>")
        assert email == "dana@example.com"
        assert "Dana" in name

    def test_address_is_lowercased(self):
        _, email = normalize.parse_sender("X <Dana.Okafor@Example.COM>")
        assert email == "dana.okafor@example.com"

    def test_empty(self):
        assert normalize.parse_sender("") == ("", "")


class TestStripHtml:
    def test_tags_removed_and_entities_decoded(self):
        html = "<p>Hello &amp; welcome</p><p>Second</p>"
        assert normalize.strip_html(html) == "Hello & welcome\nSecond"

    def test_script_and_style_dropped(self):
        html = "<style>p{color:red}</style><p>Body</p><script>alert(1)</script>"
        assert normalize.strip_html(html) == "Body"

    def test_blockquote_dropped(self):
        html = "<p>My reply</p><blockquote><p>Your original</p></blockquote>"
        out = normalize.strip_html(html)
        assert "My reply" in out
        assert "Your original" not in out

    def test_gmail_quote_div_truncates(self):
        html = '<div>My reply</div><div class="gmail_quote"><div>Old thread</div></div>'
        out = normalize.strip_html(html)
        assert "My reply" in out
        assert "Old thread" not in out

    def test_invisible_characters_removed(self):
        assert normalize.strip_html("<p>a&nbsp;b​c</p>") == "a bc"

    def test_br_becomes_newline(self):
        assert normalize.strip_html("one<br>two") == "one\ntwo"


class TestStripQuoted:
    def test_on_wrote_cutoff(self):
        text = "Sounds good.\n\nOn Mon, Aug 10, 2026 at 9:20 AM Dana <d@example.com> wrote:\n> old"
        assert normalize.strip_quoted(text) == "Sounds good."

    def test_on_wrote_wrapped_across_lines(self):
        text = (
            "Sounds good.\n\n"
            "On Mon, Aug 10, 2026 at 9:20 AM Dana Okafor\n"
            "<dana@example.com> wrote:\n"
            "> the original message\n"
        )
        assert normalize.strip_quoted(text) == "Sounds good."

    def test_original_message_marker(self):
        text = "Reply body\n\n-----Original Message-----\nFrom: someone"
        assert normalize.strip_quoted(text) == "Reply body"

    def test_outlook_header_block(self):
        text = "Reply body\n\nFrom: Dana\nSent: Monday\nTo: Me\nSubject: Re: thing"
        assert normalize.strip_quoted(text) == "Reply body"

    def test_bare_from_line_is_not_a_cutoff(self):
        # No Sent:/To:/Subject: follows, so this is prose, not a quote header.
        text = "From: the desk of a tired researcher\n\nActual content here."
        assert "Actual content here." in normalize.strip_quoted(text)

    def test_interleaved_reply_is_preserved(self):
        text = "> your question\nmy answer\n> second question\nsecond answer"
        assert normalize.strip_quoted(text) == "my answer\nsecond answer"

    def test_signature_kept(self):
        text = "Body\n\n--\nDana Okafor\nExample Labs"
        assert "Example Labs" in normalize.strip_quoted(text)

    def test_empty(self):
        assert normalize.strip_quoted("") == ""


class TestExtractBody:
    def test_prefers_plain_over_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("plain wins")}},
                {"mimeType": "text/html", "body": {"data": b64("<p>html loses</p>")}},
            ],
        }
        assert normalize.extract_body(payload) == "plain wins"

    def test_falls_back_to_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": b64("<p>only html</p>")}},
            ],
        }
        assert normalize.extract_body(payload) == "only html"

    def test_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": b64("deep body")}},
                    ],
                },
                {"mimeType": "application/pdf", "body": {"attachmentId": "abc"}},
            ],
        }
        assert normalize.extract_body(payload) == "deep body"

    def test_attachment_only_yields_empty(self):
        payload = {"mimeType": "application/pdf", "body": {"attachmentId": "abc"}}
        assert normalize.extract_body(payload) == ""

    def test_quoting_stripped_from_plain(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": b64("new text\n\n-----Original Message-----\nold")},
        }
        assert normalize.extract_body(payload) == "new text"


class TestNormalizeMessage:
    def test_full_message(self):
        raw = {
            "id": "18f0abc",
            "threadId": "18f0aaa",
            "internalDate": "1786353600000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Dana Okafor <dana@example.com>"},
                    {"name": "Subject", "value": "Benchmark v2 spec"},
                    {"name": "To", "value": "me@example.com"},
                ],
                "body": {"data": b64("Spec attached. Need the harness by the 24th.")},
            },
        }
        msg = normalize.normalize_message(raw, "work")

        assert msg.gmail_id == "18f0abc"
        assert msg.thread_id == "18f0aaa"
        assert msg.account_label == "work"
        assert msg.sender_name == "Dana Okafor"
        assert msg.sender_email == "dana@example.com"
        assert msg.subject == "Benchmark v2 spec"
        assert "harness by the 24th" in msg.body_text
        assert msg.internal_date_ms == 1786353600000
        assert msg.timestamp.tzinfo is None  # stored naive-UTC
        assert msg.timestamp.year == 2026

    def test_missing_headers_do_not_raise(self):
        msg = normalize.normalize_message({"id": "x", "threadId": "t"}, "personal")
        assert msg.subject == ""
        assert msg.sender_email == ""
        assert msg.body_text == ""
        assert msg.internal_date_ms == 0

    def test_as_row_matches_model_columns(self):
        from personalagi.models import Message

        raw = {"id": "x", "threadId": "t", "internalDate": "1786353600000", "payload": {}}
        row = normalize.normalize_message(raw, "personal").as_row()
        columns = set(Message.model_fields) - {"id"}
        assert set(row) == columns
