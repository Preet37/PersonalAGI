"""Reading and writing the markdown vault.

Owns the file format: YAML frontmatter profile (always loaded, cheap) above a
chronological log (retrieved selectively). Appends are date-ordered and
idempotent per source message id, so re-ingesting the same email is a no-op.
"""
