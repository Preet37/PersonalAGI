"""Source adapters: the only layer that knows what a source looks like.

Each adapter turns one source's native records into Events and Participants
(ARCHITECTURE.md D1). Nothing below this package may reference a sender, a
subject, a thread, a phone number, or a calendar organiser — those are all
source shapes, and the point of the Event type is that downstream code never
sees them.

Adding a source is writing one module here.
"""

from personalagi.adapters.base import EventRecord, ParticipantRecord

__all__ = ["EventRecord", "ParticipantRecord"]
