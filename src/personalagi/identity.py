"""Source-agnostic identity heuristics: is this address a person or a machine?

This lives above every adapter on purpose. The rules here were all learned
from real Gmail data, but none of them are *about* Gmail — an iMessage handle
or a calendar organiser needs the same two questions answered:

  1. Is this address a robot? (`looks_automated`)
  2. Is this address a shared envelope many humans send through?
     (`shared_addresses`)

Stage 8 moves participant resolution here wholesale. Keeping the functions
free of any Message import is what makes that a move rather than a rewrite.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Robot markers anywhere in the local part, delimiter-bounded. The prefix-only
# version of this missed `jobalerts-noreply@linkedin.com` — 377 messages in the
# real corpus — because the marker was not at the start.
_AUTOMATED_RE = re.compile(
    r"(?:^|[.\-+_])"
    r"("
    # machine senders
    r"no-?reply|do-?not-?reply|notifications?|alerts?|mailer|bounce|postmaster|"
    r"automated|digest|newsletter|unsubscribe|noreply|"
    # role accounts: a shared mailbox is a company, not a person, and a
    # person file for one is landfill. Dropping these was a regression that
    # gave support@luma.com a 338-entry "person" file.
    r"support|billing|receipts?|invoices?|sales|admin|info|contact|hello|help|"
    r"service|marketing|offers|deals|events|apply|careers|jobs|team|press|"
    r"newsroom|editors?|premium|promotions?|invitations?|updates?|news"
    r")"
    r"(?:[.\-+_]|$)",
    re.IGNORECASE,
)

# An address used by more than this many distinct display names is a shared
# bulk sender, not a person. LinkedIn sends every connection request from
# invitations@linkedin.com with the requester's name in the From header, so
# keying identity on the address would fuse hundreds of people into one file.
SHARED_ADDRESS_NAME_THRESHOLD = 3


def looks_automated(email: str) -> bool:
    """Heuristic, and deliberately conservative.

    Matches a robot marker anywhere in the local part, delimiter-bounded.
    Bounding is what keeps it conservative: `alerts@` and `job-alerts@` match,
    but `alerta@` and `dana@` do not. False negatives cost one junk file;
    false positives lose a real person, so the bound matters more than reach.
    """
    local = (email or "").split("@", 1)[0]
    return bool(_AUTOMATED_RE.search(local))


def shared_addresses(
    pairs: Iterable[tuple[str, str]],
    threshold: int = SHARED_ADDRESS_NAME_THRESHOLD,
) -> set[str]:
    """Addresses used by many different display names.

    Takes (address, display_name) pairs rather than messages so any source can
    call it. Data-driven rather than a hardcoded blocklist of providers: any
    bulk sender that stamps a human's name onto a shared envelope address gets
    caught, not just the ones I happened to think of.
    """
    names_by_address: dict[str, set[str]] = {}
    for address, name in pairs:
        address = (address or "").strip().lower()
        name = (name or "").strip().lower()
        if address and name:
            names_by_address.setdefault(address, set()).add(name)
    return {
        address for address, names in names_by_address.items() if len(names) > threshold
    }


def is_human_sender(email: str, shared: set[str] | None = None) -> bool:
    """Both tests at once: not a robot address, not a shared envelope.

    `shared` is passed in rather than computed because it is a property of the
    whole corpus, not of one address, and recomputing it per message would be
    quadratic.
    """
    address = (email or "").strip().lower()
    if not address:
        return False
    if looks_automated(address):
        return False
    return address not in (shared or set())
