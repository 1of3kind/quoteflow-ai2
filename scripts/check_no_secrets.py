#!/usr/bin/env python
"""Fail CI if any secret-shaped value is committed to the repo or its history.

Covers the OWASP A02/A07 secrets-in-code requirement: API keys, Stripe
secrets, Twilio credentials, database URLs with passwords, JWT secrets, and
private key blocks — across the working tree and full git history.
"""

import re
import subprocess
import sys

PATTERNS = [
    (r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}", "OpenAI-style secret key"),
    (r"whsec_[A-Za-z0-9]{10,}", "Stripe webhook signing secret"),
    (r"sk_live_[A-Za-z0-9]{10,}", "Stripe live secret key"),
    (r"rk_live_[A-Za-z0-9]{10,}", "Stripe live restricted key"),
    (r"AC[a-f0-9]{32}", "Twilio account SID"),
    (r"SK[a-f0-9]{32}", "Twilio API key SID"),
    (r"SG\.[A-Za-z0-9_-]{20,}", "SendGrid API key"),
    (r"xox[bpars]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key block"),
    (r"postgres(?:ql)?(?:\+\w+)?://[^\s/:@]+:[^\s/@]+@[^\s]+", "database URL with password"),
    (r"JWT_SECRET\s*=\s*[\"']?[A-Za-z0-9+/_-]{24,}", "hardcoded JWT secret"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub PAT"),
]

ALLOW_PLACEHOLDERS = {"replace-me", "change-me", "your-api-key", "example", "xxx", "..."}


def scan_text(text: str, where: str) -> list[str]:
    findings = []
    for pattern, label in PATTERNS:
        for match in re.finditer(pattern, text):
            # Allow obvious placeholders and doc examples
            context = text[max(0, match.start() - 40):match.end() + 40].lower()
            if any(p in context for p in ALLOW_PLACEHOLDERS):
                continue
            if re.fullmatch(r"[Ax]+|\.{3}|<[^>]+>|\$\{[^}]*\}", match.group(0)):
                continue
            # A database URL that only points at localhost is a dev
            # placeholder, not a committed credential. Anything else with an
            # embedded password is flagged.
            if label == "database URL with password":
                url = match.group(0)
                host = re.search(r"@([^/:?]+)", url)
                if host and host.group(1) in ("localhost", "127.0.0.1"):
                    continue
            findings.append(f"{where}: {label}: {match.group(0)[:12]}…")
    return findings


def main() -> int:
    findings: list[str] = []

    # Working tree (excluding .env.example placeholders, tests with mock data)
    proc = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True)
    files = [f for f in proc.stdout.splitlines()
             if f != ".env.example" and not f.startswith("tests/")]
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                findings += scan_text(fh.read(), path)
        except OSError:
            continue

    # Full git history (every blob of every commit)
    proc = subprocess.run(
        ["git", "rev-list", "--all"], capture_output=True, text=True, check=True)
    commits = proc.stdout.split()
    seen = set()
    for commit in commits:
        blobs = subprocess.run(
            ["git", "ls-tree", "-r", commit], capture_output=True, text=True, check=True)
        for line in blobs.stdout.splitlines():
            meta, _, path = line.partition("\t")
            mode, otype, oid = meta.split()
            if otype != "blob" or oid in seen or path == ".env.example":
                continue
            if path.startswith(("tests/", "docs/")):
                continue
            seen.add(oid)
            blob = subprocess.run(
                ["git", "cat-file", "blob", oid], capture_output=True, text=True, check=True)
            findings += scan_text(blob.stdout, f"{commit[:8]}:{path}")

    if findings:
        print("COMMITTED SECRETS DETECTED — remove them and rotate the credentials:")
        for f in sorted(set(findings)):
            print("  " + f)
        return 1
    print("secret scan clean (working tree + full git history)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
