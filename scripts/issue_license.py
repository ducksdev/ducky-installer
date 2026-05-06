#!/usr/bin/env python3
"""Issue a Pro license token for Ducky Pool.

Usage:
    DUCKY_LICENSE_SECRET=your-secret python3 issue_license.py customer@example.com

The secret MUST match what's set in your production deployment's environment
(or whatever DUCKY_LICENSE_SECRET your customer's install will be running).

Anyone with the secret can mint valid tokens. Store the secret in a password
manager. If it leaks, rotate to a new secret in your next release — old
tokens stop verifying immediately, and you'll need to re-issue keys to
existing paying customers.

Token format (inspectable, not encrypted):
    base64url(email|tier|issued_ts).hex(hmac_sha256)

Example output:
    Y3VzdG9tZXJAZXhhbXBsZS5jb218cHJvfDE3MzMyMDc4MzQ.5f2a7b...
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: issue_license.py <email> [tier]", file=sys.stderr)
        print("  tier defaults to 'pro' (currently the only paid tier).", file=sys.stderr)
        return 2

    email = sys.argv[1].strip().lower()
    tier = sys.argv[2].strip().lower() if len(sys.argv) > 2 else "pro"

    if "@" not in email:
        print(f"Refusing: '{email}' doesn't look like an email.", file=sys.stderr)
        return 2
    if tier != "pro":
        print(f"Unsupported tier: {tier!r}. Only 'pro' is supported.", file=sys.stderr)
        return 2

    secret = os.environ.get("DUCKY_LICENSE_SECRET", "").encode("utf-8")
    if not secret:
        print(
            "DUCKY_LICENSE_SECRET env var not set.\n"
            "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'",
            file=sys.stderr,
        )
        return 1

    payload = f"{email}|{tier}|{int(time.time())}"
    payload_b64 = (
        base64.urlsafe_b64encode(payload.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"{payload_b64}.{sig}"

    print()
    print(f"Email:   {email}")
    print(f"Tier:    {tier}")
    print(f"Issued:  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print()
    print("License key (send this to the customer):")
    print()
    print(token)
    print()
    print("Customer pastes this into Settings → License → Save.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
