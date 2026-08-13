"""Activation and meeting prep.

The reframe: the system used to ask "is this email important?". A Deepgram job
alert is objectively bulk mail; it mattered because of who the owner knows
there and what he owes them, none of which was in the email. So the question is
"what does this connect to", and the output is what the message woke up.

The hard part is STOPPING. Three hops and everything connects to everything,
and the system tells you a lunch order relates to your entire life. Most of
these tests are about the brakes.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from personalagi import db as db_module
from personalagi.activate import (
    Node,
    activate,
    build_edges,
    label_nodes,
    open_loops,
    render_activation,
    seeds_for_event,
)
from personalagi.config import Settings
from personalagi.models import Commitment, Edge, Event, Goal, Participant, PersonRole
from personalagi.prep import build_prep, render_prep, upcoming_meetings
from personalagi.records import ActivationLimits, NodeType, Provenance

NOW = datetime(2026, 8, 13, 9, 0)


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    yield Settings(
        database_url=f"sqlite:///{tmp_path / 'a.db'}",
        context_dir=tmp_path / "ctx",
        owner_emails="preet@example.com",
    )
    db_module._engine = None


def session_for(settings):
    return Session(db_module.init_db(settings))


def add_event(settings, source_id, text="body", *, title="", people=(), when=NOW,
              source="gmail", provenance=Provenance.EXTERNAL):
    with session_for(settings) as session:
        event = Event(
            source=source, source_id=source_id, account_label="personal",
            thread_key="t", title=title, text=text, timestamp=when,
            timestamp_ms=int(when.replace(tzinfo=UTC).timestamp() * 1000),
            provenance=provenance, ingested_at=NOW,
        )
        session.add(event)
        session.flush()
        for slug, role, address in people:
            session.add(
                Participant(
                    event_id=event.id, address=address, display_name=slug.title(),
                    role=role, person_slug=slug,
                )
            )
        session.commit()
        return event.id


def add_commitment(settings, event_id, *, slug="karan", direction="i_owe",
                   what="Send the prospectus", goal_id=None):
    with session_for(settings) as session:
        row = Commitment(
            event_id=event_id, direction=direction, person_slug=slug,
            person_name=slug.title(), person_email=f"{slug}@example.com",
            what=what, what_hash=what[:8], quote=f"I'll {what.lower()}",
            status="open", promised_at=NOW, manually_closed=False,
            extracted_at=NOW, goal_id=goal_id,
        )
        session.add(row)
        session.commit()
        return row.id


def add_goal(settings, slug="hackdev", title="Land HackDev sponsors"):
    with session_for(settings) as session:
        goal = Goal(slug=slug, title=title, status="active",
                    created_at=NOW, updated_at=NOW)
        session.add(goal)
        session.commit()
        return goal.id


class TestEdgeBuilding:
    def test_participants_become_person_event_edges(self, settings):
        add_event(settings, "g1", people=[("karan", "from", "karan@example.com")])

        result = build_edges(settings)

        assert result.created >= 2  # both directions

    def test_a_robot_participant_gets_no_edge(self, settings):
        """person_slug is empty for robots, the owner, and shared envelopes.
        Edges to those would connect every bulk sender to everything."""
        with session_for(settings) as session:
            event = Event(
                source="gmail", source_id="g1", title="", text="x",
                timestamp=NOW, timestamp_ms=1, provenance=Provenance.EXTERNAL,
                ingested_at=NOW,
            )
            session.add(event)
            session.flush()
            session.add(
                Participant(event_id=event.id, address="no-reply@x.com",
                            role="from", person_slug="")
            )
            session.commit()

        assert build_edges(settings).created == 0

    def test_commitments_link_people_to_obligations(self, settings):
        event_id = add_event(settings, "g1",
                             people=[("karan", "from", "karan@example.com")])
        add_commitment(settings, event_id)

        build_edges(settings)

        with session_for(settings) as session:
            relations = {
                e.relation for e in session.execute(
                    __import__("sqlalchemy").select(Edge)
                ).scalars()
            }
        assert "owes" in relations

    def test_it_is_idempotent(self, settings):
        add_event(settings, "g1", people=[("karan", "from", "karan@example.com")])

        first = build_edges(settings).created
        second = build_edges(settings).created

        assert first > 0
        assert second == 0

    def test_every_edge_is_traceable_to_a_record(self, settings):
        """No edge is inferred by a model; each is a fact already stored."""
        add_event(settings, "g1", people=[("karan", "from", "karan@example.com")])
        build_edges(settings)

        with session_for(settings) as session:
            edges = list(
                session.execute(__import__("sqlalchemy").select(Edge)).scalars()
            )

        assert all(e.source_event_id is not None for e in edges)


class TestActivationBrakes:
    """Without these the feature is noise."""

    def chain(self, settings, length: int):
        """A -> B -> C -> ... via weak `mentions` edges."""
        with session_for(settings) as session:
            for i in range(length):
                session.add(
                    Edge(
                        src_type="person", src_id=f"p{i}",
                        dst_type="person", dst_id=f"p{i+1}",
                        relation="mentions", weight=0.4, created_at=NOW,
                    )
                )
            session.commit()

    def test_energy_decays_with_every_hop(self, settings):
        self.chain(settings, 3)

        lit = activate([Node("person", "p0")], settings)
        by_id = {a.node.id: a.energy for a in lit}

        assert by_id["p1"] > by_id.get("p2", 0)

    def test_the_threshold_stops_the_walk(self, settings):
        """0.4^3 = 0.064, below the 0.15 floor, so p3 is never reached."""
        self.chain(settings, 4)

        ids = {a.node.id for a in activate([Node("person", "p0")], settings)}

        assert "p1" in ids and "p2" in ids
        assert "p3" not in ids

    def test_depth_is_capped_independently_of_energy(self, settings):
        """Even strong edges stop. Without this, a dense graph of strong links
        reaches everything."""
        with session_for(settings) as session:
            for i in range(6):
                session.add(
                    Edge(src_type="person", src_id=f"p{i}", dst_type="person",
                         dst_id=f"p{i+1}", relation="owes", weight=0.99,
                         created_at=NOW)
                )
            session.commit()

        lit = activate(
            [Node("person", "p0")], settings,
            limits=ActivationLimits(threshold=0.0, max_depth=2),
        )

        assert max(a.depth for a in lit) <= 2

    def test_the_node_cap_bounds_a_dense_graph(self, settings):
        with session_for(settings) as session:
            for i in range(50):
                session.add(
                    Edge(src_type="person", src_id="hub", dst_type="person",
                         dst_id=f"n{i}", relation="owes", weight=0.9,
                         created_at=NOW)
                )
            session.commit()

        lit = activate(
            [Node("person", "hub")], settings,
            limits=ActivationLimits(max_nodes=10),
        )

        assert len(lit) <= 10

    def test_a_strong_relation_reaches_further_than_a_weak_one(self, settings):
        with session_for(settings) as session:
            session.add(Edge(src_type="person", src_id="a", dst_type="goal",
                             dst_id="1", relation="owes", weight=0.9,
                             created_at=NOW))
            session.add(Edge(src_type="person", src_id="a", dst_type="event",
                             dst_id="1", relation="mentions", weight=0.4,
                             created_at=NOW))
            session.commit()

        lit = {a.node.type: a.energy for a in activate([Node("person", "a")], settings)}

        assert lit["goal"] > lit["event"]

    def test_seeds_are_not_returned_as_results(self, settings):
        self.chain(settings, 2)

        assert all(a.node.id != "p0" for a in activate([Node("person", "p0")], settings))

    def test_no_seeds_activates_nothing(self, settings):
        assert activate([], settings) == []

    def test_an_isolated_node_says_so(self, settings):
        assert "genuinely isolated" in render_activation([])


class TestTheDeepgramCase:
    """The example the whole reframe came from.

    A bulk job alert is worthless on its own. It matters because of who it
    connects to and what is open with them.
    """

    def test_an_event_lights_up_the_open_commitment_behind_it(self, settings):
        goal_id = add_goal(settings)
        talk = add_event(settings, "chat1",
                         people=[("karan", "from", "karan@example.com")])
        add_commitment(settings, talk, slug="karan", goal_id=goal_id)
        alert = add_event(settings, "alert1", title="Deepgram is hiring",
                          people=[("karan", "from", "karan@example.com")])
        build_edges(settings)

        lit = label_nodes(activate(seeds_for_event(alert, settings), settings), settings)
        loops = open_loops(lit, settings)

        assert any("Send the prospectus" in loop for loop in loops)
        assert any("HackDev" in loop for loop in loops)

    def test_the_path_explains_how_it_got_there(self, settings):
        """A surprising connection has to be explainable or it reads as magic
        and stops being trustworthy."""
        talk = add_event(settings, "chat1",
                         people=[("karan", "from", "karan@example.com")])
        add_commitment(settings, talk, slug="karan")
        build_edges(settings)

        lit = activate([Node(NodeType.PERSON, "karan")], settings)

        assert all(a.why() for a in lit)


class TestPrep:
    def test_it_reports_having_nothing_rather_than_inventing(self, settings):
        """A confident-looking brief assembled from nothing is worse than
        admitting there is nothing -- unprepared at least knows it is."""
        add_event(settings, "g1", people=[("karan", "from", "karan@example.com")])
        prep = build_prep("karan", settings)
        prep.recent = []
        prep.profile = ""

        assert "no data in any connected source" in render_prep(prep)

    def test_an_unknown_person_returns_none(self, settings):
        assert build_prep("nobody", settings) is None

    def test_it_finds_someone_by_address(self, settings):
        add_event(settings, "g1", people=[("karan", "from", "karan@example.com")])

        assert build_prep("karan@example.com", settings).person_slug == "karan"

    def test_commitments_appear_in_both_directions(self, settings):
        event_id = add_event(settings, "g1",
                             people=[("karan", "from", "karan@example.com")])
        add_commitment(settings, event_id, direction="i_owe", what="Send deck")
        add_commitment(settings, event_id, direction="they_owe", what="Intro me")

        prep = build_prep("karan", settings)

        assert len(prep.you_owe) == 1
        assert len(prep.they_owe) == 1

    def test_every_claim_carries_its_source_event(self, settings):
        """The layer where a hallucination does the most damage."""
        event_id = add_event(settings, "g1", title="Sponsorship",
                             people=[("karan", "from", "karan@example.com")])
        add_commitment(settings, event_id)

        prep = build_prep("karan", settings)
        rendered = render_prep(prep)

        assert f"event {event_id}" in rendered
        assert all(c.event_id is not None for c in prep.you_owe)

    def test_generated_events_never_appear_in_a_brief(self, settings):
        add_event(settings, "real", title="Real thing",
                  people=[("karan", "from", "karan@example.com")])
        add_event(settings, "made-up", title="System summary",
                  people=[("karan", "from", "karan@example.com")],
                  provenance=Provenance.GENERATED)

        prep = build_prep("karan", settings)

        assert all("System summary" not in c.text for c in prep.recent)

    def test_the_role_relative_to_a_goal_is_stated(self, settings):
        """Prep that ignores role writes a generic dossier. An advocate and a
        recommender need different asks."""
        goal_id = add_goal(settings, slug="cmu", title="Get into CMU")
        add_event(settings, "g1", people=[("pratik", "from", "pratik@example.com")])
        with session_for(settings) as session:
            session.add(
                PersonRole(person_slug="pratik", goal_id=goal_id,
                           role="recommender", created_at=NOW)
            )
            session.commit()

        prep = build_prep("pratik", settings)

        assert any("recommender" in g for g in prep.goals)

    def test_what_they_asked_for_quotes_them_not_the_owner(self, settings):
        add_event(
            settings, "g1",
            text="Hi Preet,\nCould you send me the prospectus this week?\nThanks",
            people=[("karan", "from", "karan@example.com")],
        )

        prep = build_prep("karan", settings)

        assert any("prospectus" in c.text for c in prep.last_asks)

    def test_the_owners_own_asks_are_not_quoted_back_at_him(self, settings):
        add_event(
            settings, "sent1",
            text="Could you send me your availability?",
            people=[("karan", "to", "karan@example.com"),
                    ("", "from", "preet@example.com")],
        )

        prep = build_prep("karan", settings)

        assert prep.last_asks == []


class TestMeetings:
    def test_only_calendar_events_inside_the_window(self, settings):
        add_event(settings, "soon", source="calendar", title="Arjun sync",
                  when=NOW + timedelta(hours=5))
        add_event(settings, "far", source="calendar", title="Later",
                  when=NOW + timedelta(days=10))
        add_event(settings, "mail", source="gmail", when=NOW + timedelta(hours=2))

        rows = upcoming_meetings(settings, hours=24, now=NOW)

        assert [r.source_id for r in rows] == ["soon"]

    def test_a_past_meeting_is_not_upcoming(self, settings):
        add_event(settings, "past", source="calendar", when=NOW - timedelta(hours=2))

        assert upcoming_meetings(settings, hours=24, now=NOW) == []


class TestSharedEnvelopeIdentity:
    """A shared envelope carries a real person's name.

    Refusing to attach the ADDRESS is correct — it would fuse 215 unrelated
    LinkedIn requesters into one file. Refusing the PERSON too made all 215
    invisible to the graph, including the one the owner is meeting on Monday.
    """

    def test_a_person_on_a_shared_envelope_is_still_resolved(self):
        from personalagi.adapters.base import resolve_participant

        resolved = resolve_participant(
            "invitations@linkedin.com",
            "Arjun Sambamoorthy",
            headers={"list-unsubscribe": "<mailto:x>"},
            shared={"invitations@linkedin.com"},
        )

        assert resolved.is_automated is True      # the envelope is bulk
        assert resolved.person_slug == "arjun-sambamoorthy"  # the person is not

    def test_the_shared_address_is_never_attached_to_them(self):
        """Attaching it is what would fuse 215 people into one record."""
        from personalagi.adapters.base import resolve_participant
        from personalagi.context.people import slug_for

        resolved = resolve_participant(
            "invitations@linkedin.com", "Arjun Sambamoorthy",
            shared={"invitations@linkedin.com"},
        )

        # Resolved from the NAME alone, never from the address.
        assert resolved.person_slug == slug_for("Arjun Sambamoorthy", "")
        assert "linkedin" not in resolved.person_slug

    def test_two_people_on_one_envelope_stay_separate(self):
        from personalagi.adapters.base import resolve_participant

        a = resolve_participant("invitations@linkedin.com", "Arjun S",
                                shared={"invitations@linkedin.com"})
        b = resolve_participant("invitations@linkedin.com", "Dana O",
                                shared={"invitations@linkedin.com"})

        assert a.person_slug != b.person_slug

    def test_a_plain_newsletter_still_gets_no_person(self):
        """Scoped to SHARED addresses, not to bulk mail generally. A newsletter
        has one constant display name so it never qualifies."""
        from personalagi.adapters.base import resolve_participant

        resolved = resolve_participant(
            "news@brand.com", "Brand Weekly",
            headers={"list-unsubscribe": "<mailto:x>"},
            shared=set(),
        )

        assert resolved.person_slug == ""

    def test_a_nameless_shared_sender_gets_no_person(self):
        from personalagi.adapters.base import resolve_participant

        resolved = resolve_participant(
            "bulk@brand.com", "", shared={"bulk@brand.com"}
        )

        assert resolved.person_slug == ""
