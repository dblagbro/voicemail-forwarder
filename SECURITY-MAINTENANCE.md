# Runtime reconciliation and security update

Status: candidate verified; production has not been changed by this patch.

Exact candidate: `sha256:efa8ab304beab278a16846840907f5774a8e3eff08ae97d034d23878cb67b08b`.
The new 2026-11-11 scan reports 0 Critical / 0 High / 0 Medium / 0 Low package
matches. The old deployed image retains its earlier 0 Critical / 2 High findings;
that baseline was not rescanned. All 15 offline tests and dependency consistency
checks pass. The new STLS tests first reproduced three assertions failing in the
unfixed code, then passed with the fail-closed change. An initial scanner attempt
failed because it used a peer-only binary path; the corrected complete run passed.
Intermediate scans identified packaging-tool findings; removing unused runtime
pip resolved those in the final image. Earlier candidates are not deployed.
See `evidence/candidate-20261111.json` for source, image and evidence hashes.

The previous public master did not contain the application currently deployed.
This change reconciles the four differing application/template files from the
existing local revision `73e6dd3`, whose tracked application hashes were matched
against the running image before editing. It preserves UIDL-before-RETR duplicate
suppression (including legacy seen-table compatibility and UIDL-less fallback),
attachment path containment, rotated/gzip log search and mailbox test UI. Host
usage statistics in source comments were omitted. Configuration, databases,
attachments, logs and credentials are excluded from the repository/build context.

The new changes upgrade Gunicorn 21.2.0 to 27.0.0 and Flask 3.0.3 to 3.1.3, pin
all Python runtime dependencies, pin the Python 3.12 Alpine base digest, and remove
unused compiler packages and pip from the runtime recipe. Dependency consistency
is checked during the build before pip is removed; the final image verifies its
runtime lock and packaging-tool absence. Gunicorn 27 includes request
framing validation; the tested gthread deployment remains one worker/eight threads.
Requested POP3 STLS now fails closed in both polling and connection testing.
Explicit legacy plaintext configuration remains unchanged; it is not silently
converted to TLS. A TLS failure delays that mailbox until its TLS service is fixed.

## Build and isolated tests

```sh
docker build -t voicemail-forwarder:security .
docker run --rm --network none --memory 512m --cpus 1 \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD/tests:/tests:ro" --entrypoint python \
  voicemail-forwarder:security -m unittest discover -s /tests -v
```

`tools/verify_runtime.py --source "$PWD" --output /chosen/evidence-directory
--image voicemail-forwarder:security` runs the build, offline tests and dependency
check and records image/source/log hashes. Add `--trivy /path/to/trivy
--trivy-cache /path/to/cache` for a new exact-image scan. On TMR, run capacity-heavy
work through the maintained capacity guard and preserve at least 64 GiB free.
The base digest and Python lock are pinned; Alpine security repository packages
can advance, so a later rebuild may produce a different image and must be tested
and scanned again. The recorded image ID identifies the exact accepted artifact.

The fixtures have empty/disposable configuration, fake POP3/SMTP interfaces and
loopback-only Gunicorn. Tests cover no-refetch/no-resend UIDLs, legacy and UIDL-less
compatibility, TLS rejection before credentials, attachment traversal/symlinks,
rotated logs, UI pages, SQLite backup integrity and malformed transfer encodings.
No real mailbox, mail delivery, attachment or credentials are test inputs.

## Production rollout and recovery gates

Do not deploy merely because this branch builds. First require merged source,
required CI/review gates, an exact candidate image/scan, fresh live identity checks,
no active maintenance worker and sufficient guarded disk capacity. Retain the
original image and frozen service configuration. Freeze only this service at a safe
poll boundary; take and independently restore a consistent SQLite/configuration
backup, with private local and SAN copies and integrity verification. Preserve all
existing data/log mounts, hostname/network behavior and unrelated Compose services.
Validate an isolated candidate and original-image recovery on disposable data.
Apply a compare-and-swap image-only Compose change, then recreate only
`voicemail-forwarder` with `--no-deps`; verify runtime image, state and proxy auth.
Never run `compose down` or remove volumes.

Once polling resumes, deduplication state may acquire new acknowledged forwards.
Recovery must preserve the latest live database. Never reload a pre-cutover database
or clear `state.db`: either can resend mail. Do not run an old image concurrently
against the same mailboxes or live data. A rollback to the original image also
restores its TLS downgrade behavior and old Gunicorn findings.

## Residual findings and limits

- The app has no built-in authentication/CSRF boundary; the deployment requires a
  trusted administration network and authenticated proxy. Its development session
  key must not be treated as an authentication control.
- Explicit plaintext POP3 remains supported for legacy installations. No provider
  sign-in, TLS negotiation or actual SMTP delivery has been used as acceptance.
- Existing SMTP-send-then-record ordering is not an exactly-once transaction; a
  process failure between those steps can cause a resend. Avoid forced termination
  during a poll. Manual/automatic poll concurrency also needs separate review.
- Configuration and local attachments remain sensitive operational data. Retain
  private backups; never attach them to issues, commits, PRs or public scan evidence.
- A package scan does not establish exploitability or absence of application flaws.

Primary references: [Gunicorn releases](https://github.com/benoitc/gunicorn/releases/tag/27.0.0),
[Flask release](https://pypi.org/project/Flask/3.1.3/).

The first PR secret check flagged a configuration-field tuple at the new mailbox
test validation. Inspection confirmed field names and variables, with no literal
credential. Equivalent separate field/value sequences remove the ambiguity, and
an additional missing-field regression verifies that validation never connects.
No scanner allowlist or suppression was added.
