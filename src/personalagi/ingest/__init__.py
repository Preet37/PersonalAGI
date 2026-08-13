"""Sources that write into the vault.

Each ingester turns external events into log entries appended to the right
person/topic/date file. gmail.py is the first: read-only, multi-account.
"""
