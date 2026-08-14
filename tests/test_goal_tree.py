"""The nested goals file.

Hierarchy is not decoration: it changes which deadlines apply, which goals look
urgent, and whether a parent counts as active. The flat model reported "Land
HackDev sponsors has had no activity" while a sponsor conversation was live,
because the activity was attached to a child.
"""

from datetime import date

import pytest

from personalagi import db as db_module
from personalagi.config import Settings
from personalagi.goal_tree import inherit, load_tree, parse_tree, render_tree, sync_tree

TREE = """---
type: goal_tree
---

# Goals

## Land a full-time role starting January 2027
why: Graduating December. Need an offer early enough to process
     the paperwork.
status: active

### Cisco
why: Fastest path.
people: dj-sampath, arjun-sambamoorthy:decision_maker
deadline: 2026-08-26
- [x] Meeting with DJ
- [ ] Deep dive call with Arjun

### McCoy
people: sameer-mccoy
- [ ] Follow up on the full-time conversation

## Build a startup
status: active

### Loomin
history: Won GTC. Met Jensen. The post went viral, which is how Pratik found me.
- [ ] Move out of beta

## Proposed

### Something the system guessed
- [ ] a step
"""


@pytest.fixture
def settings(tmp_path):
    db_module._engine = None
    (tmp_path / "ctx" / "goals").mkdir(parents=True)
    (tmp_path / "ctx" / "goals" / "goals.md").write_text(TREE)
    yield Settings(
        database_url=f"sqlite:///{tmp_path / 't.db'}", context_dir=tmp_path / "ctx"
    )
    db_module._engine = None


class TestParsing:
    def test_headings_become_a_hierarchy(self):
        goals = {g.slug: g for g in parse_tree(TREE)}

        assert goals["cisco"].parent == "land-a-full-time-role-starting-january-2027"
        assert "cisco" in goals["land-a-full-time-role-starting-january-2027"].children

    def test_steps_and_done_markers_parse(self):
        cisco = next(g for g in parse_tree(TREE) if g.slug == "cisco")

        assert [s.done for s in cisco.steps] == [True, False]

    def test_people_parse_with_and_without_roles(self):
        cisco = next(g for g in parse_tree(TREE) if g.slug == "cisco")

        assert cisco.people["dj-sampath"] == "contact"
        assert cisco.people["arjun-sambamoorthy"] == "decision_maker"

    def test_a_wrapped_why_keeps_its_second_line(self):
        """Without continuation handling, every multi-line `why` silently
        loses everything after the first line."""
        top = next(
            g for g in parse_tree(TREE)
            if g.slug == "land-a-full-time-role-starting-january-2027"
        )

        assert "paperwork" in top.why

    def test_history_is_captured(self):
        """Loomin caused GTC caused Jensen caused the post caused Pratik. That
        chain is why a follow-up should carry where the person came from."""
        loomin = next(g for g in parse_tree(TREE) if g.slug == "loomin")

        assert "viral" in loomin.history

    def test_proposed_goals_are_flagged_not_confirmed(self):
        proposed = [g for g in parse_tree(TREE) if g.proposed]

        assert len(proposed) == 1
        assert proposed[0].title == "Something the system guessed"


class TestInheritance:
    def test_a_child_with_no_date_inherits_the_parent_s(self):
        goals = {g.slug: g for g in inherit(parse_tree(TREE))}

        # McCoy states no deadline; it answers to the parent's, which the
        # parent got from Cisco.
        assert goals["mccoy"].deadline is not None

    def test_a_parent_takes_its_earliest_child_deadline(self):
        """The parent names no date and is urgent anyway, because Cisco is.
        Without this a top-level goal can never become urgent at all."""
        goals = {g.slug: g for g in inherit(parse_tree(TREE))}

        assert goals["land-a-full-time-role-starting-january-2027"].deadline == (
            date(2026, 8, 26)
        )

    def test_a_child_keeps_its_own_date_over_the_parent_s(self):
        goals = {g.slug: g for g in inherit(parse_tree(TREE))}

        assert goals["cisco"].deadline == date(2026, 8, 26)

    def test_a_branch_with_no_dates_anywhere_stays_undated(self):
        goals = {g.slug: g for g in inherit(parse_tree(TREE))}

        assert goals["build-a-startup"].deadline is None


class TestSync:
    def test_it_indexes_the_tree(self, settings):
        counts = sync_tree(settings)

        assert counts["goals"] == 5  # 2 top-level + 3 children
        assert counts["proposed"] == 1

    def test_proposed_goals_are_never_indexed(self, settings):
        from sqlmodel import Session, select

        from personalagi.models import Goal

        sync_tree(settings)
        with Session(db_module.get_engine(settings)) as session:
            slugs = {g.slug for g in session.execute(select(Goal)).scalars()}

        assert "something-the-system-guessed" not in slugs

    def test_parent_links_are_stored(self, settings):
        from sqlmodel import Session, select

        from personalagi.models import Goal

        sync_tree(settings)
        with Session(db_module.get_engine(settings)) as session:
            cisco = session.execute(
                select(Goal).where(Goal.slug == "cisco")
            ).scalar_one()

        assert cisco.parent_slug == "land-a-full-time-role-starting-january-2027"

    def test_a_goal_deleted_from_the_file_stops_driving_the_sweep(self, settings):
        """The file is the source of truth. A goal removed there must stop
        firing alerts, or deleted examples nag forever."""
        from sqlmodel import Session, select

        from personalagi.models import Goal

        sync_tree(settings)
        path = settings.context_dir / "goals" / "goals.md"
        path.write_text(TREE.replace("### McCoy\npeople: sameer-mccoy\n- [ ] Follow up on the full-time conversation\n", ""))
        sync_tree(settings)

        with Session(db_module.get_engine(settings)) as session:
            slugs = {g.slug for g in session.execute(select(Goal)).scalars()}

        assert "mccoy" not in slugs

    def test_it_is_idempotent(self, settings):
        sync_tree(settings)
        second = sync_tree(settings)

        assert second["goals"] == 5


class TestActivityRollup:
    def test_a_child_s_activity_lifts_the_parent(self, settings):
        """The complaint this fixes: a parent reported as having no activity
        while a live conversation sat on one of its children."""
        from datetime import datetime

        from sqlmodel import Session, select

        from personalagi.goal_tree import roll_up_activity
        from personalagi.models import Goal

        sync_tree(settings)
        when = datetime(2026, 8, 13, 9, 0)
        with Session(db_module.get_engine(settings)) as session:
            child = session.execute(
                select(Goal).where(Goal.slug == "cisco")
            ).scalar_one()
            child.last_activity = when
            session.commit()

        roll_up_activity(settings)

        with Session(db_module.get_engine(settings)) as session:
            parent = session.execute(
                select(Goal).where(
                    Goal.slug == "land-a-full-time-role-starting-january-2027"
                )
            ).scalar_one()

        assert parent.last_activity == when


class TestRendering:
    def test_the_outline_shows_nesting_and_deadlines(self, settings):
        output = render_tree(load_tree(settings))

        assert "Cisco" in output
        assert "via child" in output  # the parent's date came from a child

    def test_an_empty_file_says_it_is_a_file_not_a_form(self, settings):
        assert "not a form" in render_tree([])
