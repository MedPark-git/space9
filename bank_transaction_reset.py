"""LEGACY TEST RESET — DISABLED IN PRODUCTION.

This filename is kept only to make accidental imports fail closed. The destructive
reset used during the 2026-09-14 test incident is no longer executable from the
Production application. Any future data-reset utility must live in an approved
maintenance procedure with explicit user approval and a fresh backup.
"""

def register(*args, **kwargs):
    raise RuntimeError("bank_transaction_reset is disabled in Production")


def run(*args, **kwargs):
    raise RuntimeError("bank_transaction_reset is disabled in Production")


if __name__ == "__main__":
    raise SystemExit("bank_transaction_reset is disabled in Production")
