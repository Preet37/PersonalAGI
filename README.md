# PersonalAGI

A personal context system. Markdown files are the source of truth — not a
database. One file per person, per topic, per date.

Each file has two layers:

- a short **YAML frontmatter profile** — always loaded, cheap, the stable facts
- a **chronological log** below it — retrieved selectively, never loaded whole

SQLite with FTS5 indexes the logs for search. The index is derived: delete
`data/` and reindex and you lose nothing. No vector database yet.

Status: Gmail ingest works (mail → SQLite). The vault, search, LLM layer, and
API are still stubs.

## Layout

```
context/           the vault — GITIGNORED, see context.example/
  people/          one file per person
  topics/          one file per topic or project
  daily/           one file per date
context.example/   synthetic files showing the format
src/personalagi/
  cli.py           auth / ingest / status commands
  config.py        settings from .env
  models.py        Message and IngestState tables
  db.py            engine, schema init (FTS5 lands here later)
  context/         markdown read/write, frontmatter, log appends
  ingest/
    auth.py        OAuth per account label, read-only scope
    fetch.py       watermark logic, listing, retry/backoff
    normalize.py   MIME -> flat record; HTML and quote stripping
    gmail.py       orchestration and the watermark transaction
  search/          FTS5 query layer over the logs
  llm/             Groq client, prompts, extraction
  api/             FastAPI app and routers
scripts/           OAuth bootstrap, reindex, backfill
tests/
data/              personalagi.db — derived, gitignored
credentials/       OAuth client secret — gitignored
tokens/            per-account OAuth tokens — gitignored
```

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then fill in GROQ_API_KEY and GMAIL_ACCOUNTS
mkdir -p context/{people,topics,daily} data
```

Gmail uses read-only scope (`gmail.readonly`). The OAuth client lives at
`credentials/credentials.json`; each account in `GMAIL_ACCOUNTS` is authorized
separately and gets its own token file under `tokens/`.

## Gmail ingest

```bash
python -m personalagi auth personal        # once per account, opens a browser
python -m personalagi ingest --account personal --limit 50
python -m personalagi ingest --all
python -m personalagi status               # watermarks and counts per account
```

`auth` is the only interactive command. `ingest` never opens a browser, so it
is safe to run from cron; if a token has expired it fails with instructions
rather than blocking on a prompt.

`--dry-run` lists what would be fetched, writes nothing, and leaves the
watermark untouched. Worth using for a first look at an account.

Ingest writes to SQLite only. Nothing is written to `context/` yet, and no
LLM is called — **`ingest` makes no Groq requests at all.**

### Incremental sync

Re-runs are incremental, not full re-pulls. Each account carries a watermark
in the `ingest_state` table and the sync picks one of two strategies:

1. **`history.list`** — Gmail's real change feed, cheap and exact. Used
   whenever `last_history_id` is still within Gmail's retention window
   (roughly one week).
2. **`messages.list(q="after:…")`** — a date query on `internalDate`. Used to
   bootstrap a new account, and as the fallback when the history watermark has
   aged out. Never expires, but it is a search rather than a change feed.

Three properties hold this together:

- **The `historyId` is captured before listing**, not after. Anything arriving
  mid-run gets a higher id and is picked up on the next run instead of falling
  into a gap.
- **The date fallback overlaps backwards** by `INGEST_OVERLAP_SECONDS`
  (default 24h). Mail is not delivered in `internalDate` order — delayed
  delivery and IMAP imports both land "in the past" — so a strict boundary
  drops messages permanently and silently.
- **Overlap is free because writes are idempotent.**
  `UNIQUE(account_label, gmail_id)` with `ON CONFLICT DO NOTHING` means
  re-fetching costs quota, never correctness. The key is composite on purpose:
  the same message in two accounts is two rows, with different labels and
  visibility.

The watermark advances **only after the message batch commits**. A crash
between the two re-fetches messages you already have (cheap, deduped) rather
than skipping messages you never got (silent, permanent).

**Not handled, deliberately:** deletions and label changes. Ingest asks only
for `messageAdded`, and the date fallback cannot see either. For an
append-only personal log this is the right default — deleting an email later
does not make it untrue that you received it — but it does mean SQLite and
Gmail will diverge over time.

### Quota and operational notes

Gmail allows **250 quota units/second/user**. `messages.get` and
`messages.list` cost 5 units each, `history.list` 2, `getProfile` 1. Backfill
is one `get` per message, so it is the expensive path; 20k messages is roughly
100k units. Rate-limit responses (429, and 403 with a rate-limit reason) are
retried with exponential backoff and jitter. A 403 *without* a rate-limit
reason is treated as a permission failure and not retried.

Two things that will bite before quota does:

- **Refresh tokens expire after 7 days** while the OAuth app is in *Testing*
  publishing status with External user type — that is the red "unverified app"
  consent screen. Expect to re-run `auth` weekly until the app is published or
  switched to Internal. Ingest reports this explicitly rather than failing
  obscurely.
- **Workspace domains can block unverified apps** at the admin level. That
  failure appears at consent time, not at refresh.

## Data handling

This repo is public. The data it operates on is not.

**Never leaves the machine:**

- `context/` — the entire vault: real names, email addresses, and summaries of
  private correspondence. Gitignored. `context.example/` exists so the file
  format is visible in the repo without shipping any real data.
- `data/personalagi.db` — the ingest store and (later) FTS5 index. **It holds
  the normalized body text of every ingested email**, quoting stripped but
  otherwise complete, plus sender names and addresses. It is the single most
  sensitive file in the project — more so than the vault, because it is
  verbatim rather than summarized. Gitignored.
- `credentials/`, `tokens/` — OAuth client secret and per-account access
  tokens. Gitignored.
- `.env` — API keys. Gitignored. Only `.env.example` is committed, with
  placeholders.
- Raw MIME. Bodies are normalized on the way in — HTML flattened, quoted reply
  chains dropped — and the original message is never written to disk.

**Sent to the Groq API:**

Groq is the only egress path in this system. Nothing below is implemented yet
— **as of today, `ingest` makes zero Groq calls**; mail goes Gmail → SQLite
and stops there. What follows describes where this is headed:

- **During ingest** — the content of an individual email (sender, subject,
  body) is sent to Groq to be summarized into a log entry. This means the text
  of your correspondence, including whatever the other person wrote, is
  transmitted to a third party.
- **At query time** — the log entries that FTS5 retrieved for your question,
  plus the frontmatter profiles of any people involved, are sent as prompt
  context.

Nothing else is transmitted. There is no telemetry, no analytics, and no other
network destination. Gmail access is read-only and outbound only in the sense
of fetching — nothing is ever written back to your mailbox.

What this means in practice: **anything you ingest, you have disclosed to
Groq.** That includes correspondence from people who did not consent to it.
Consult Groq's current data-retention and training policy before pointing this
at a real mailbox, and consider excluding accounts or senders where that
tradeoff is not acceptable.

The FastAPI server has no authentication. It binds to `127.0.0.1` by default.
Do not expose it on a network interface.

## License

MIT
