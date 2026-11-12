# Consistent backup preparation

This Linux/root procedure snapshots the existing `voicemail-forwarder` container.
It does not deploy an image, stop/restart the service, alter mail configuration,
change its restart policy, or restore old application data.

Run the standard-library unit tests with:

```sh
python3 -m unittest discover -s tools/maintenance -v
```

`quiesce_backup.py` checks the exact image, container identity, host configuration
and independent writable data/log bind mounts. It pauses the container and samples
the single Gunicorn worker with frame-only `py-spy` (tested with 0.4.2; never
`--locals`). A sample is accepted only when the poller is waiting, the HTTP main
thread is selecting, and any request worker is at its verified standard-library
blocking queue read. Application frames, incomplete samples, GIL-owning threads,
manual polls, configuration writes and mail sends are rejected. The queue source
file hash is checked through the frozen worker's root filesystem. No application
values are exported.

The consistent local copy is hashed and checked for SQLite integrity while the
container remains frozen. It is then unfrozen immediately. The independent local
copy is copied to the SAN and restored to a third directory, with matching file
hashes and SQLite integrity checks. The same original container must remain running
and return HTTP 200. HTML, logs, settings, manifests and database contents remain
private. Only `result.sanitized.json` may be exported.

## Execution

First inspect ongoing maintenance and live identities. Use a NEW generation under
the durable local backup root and an existing root-owned mode-0700 SAN parent.
Budget at least three copies of data/logs plus filesystem overhead; the launcher
reserves 1 GiB through the shared host capacity guard and requires the 64 GiB host
reserve. If current data exceeds that estimate, stop and revise the budget.

```sh
sudo python3 tools/maintenance/launch_backup.py \
  --expected-image sha256:EXACT_CURRENT_IMAGE \
  --generation /var/backups/voicemail-YYYYMMDD-HHMMSS \
  --san-root /mnt/s/backups/PRIVATE_PARENT \
  --guard /mnt/s/code/tmrwww01-docker-scripts/security/2026-11-09/storage/capacity_guard.py
```

The launcher creates a transient systemd unit with a five-minute start timeout and
an `ExecStopPost` recovery command. Always use it for production. Recovery releases
only this exact container's freeze and verifies HTTP readiness. It never restarts
an externally stopped service or replaces a concurrent edit. A changed container
identity/configuration requires operator investigation. If a controller dies,
inspect its journal and sanitized result; rerun `recover --generation PATH` only
for that owned generation if the backstop did not finish. A reboot/power-loss
recovery is not proven by these tests; verify actual Docker state after reboot.

## Offline recovery rehearsal

`rehearse_quiescence.py` creates labelled fixtures using the exact original image,
fresh synthetic data, `--network none`, no published ports and resource limits.
Run through the same capacity guard, using new local/SAN rehearsal directories.
It tests normal completion and real controller SIGKILL at seven journal boundaries;
the systemd backstop must resume the fixture with unchanged configuration, valid
SQLite and HTTP 200. Every fixture ID is recorded; only owned fixtures are removed.
Backup directories and all production volumes remain intact.

The first exploratory attempt rejected an idle request thread because py-spy's
`active` heuristic marks `SimpleQueue.get` active. It failed closed and resumed.
The final check accepts only the exact verified queue-read source location.

## Limits and remaining rollout gates

This is a point-in-time backup. The live service can write newer deduplication and
attachment records immediately after it is unfrozen. Never overwrite those newer
records with this snapshot. A rollout still needs controlled ingress/poller
quiescence, a fresh cutover snapshot, isolated original/candidate recovery checks,
single-service image replacement and live acceptance. Real POP3/SMTP/provider
behavior is not exercised by the synthetic tests. Raw application routes retain
their existing authentication/CSRF limitations; this helper does not change them.

Keep all private generations outside Git. Do not publish config, cookies,
credentials, mailbox names, message/attachment contents, raw logs or private
manifests. No unchanged-image vulnerability scan is implied by this preparation.

## Recorded preparation result — 2026-11-11

The original production image remains running, with its container identity and
start time unchanged. The 05:50 UTC snapshot completed in a 3.43-second systemd
unit: 151 files / 13,717,540 bytes verified locally, on the SAN and in an
independent restoration. The original service returned HTTP 200. This unit
duration includes verification; it is not a measurement of freeze duration.

`verify_restore.py` booted both the original and previously scanned candidate
from separate private SAN-snapshot copies. Both returned HTTP 200, preserved
all deduplication/attachment records and attachment bytes, passed SQLite
integrity, and stopped cleanly. Polling/alerts were disabled only in these
independent copies; network access and published ports were absent. Both owned
fixture containers were removed. Production and the source snapshot were unchanged.
Sanitized receipts are in `evidence/`; no mailbox values, attachments or private
configuration are included. The candidate remains undeployed.

Reproduce the offline restore check through the capacity guard with a 1 GiB
budget, passing `verify_restore.py --backup-generation PRIVATE_COMPLETED_GENERATION
--generation NEW_PRIVATE_TEST_GENERATION --original-image EXACT_ORIGINAL_ID
--candidate-image EXACT_CANDIDATE_ID`. A completed backup journal and its matching
SAN manifest are mandatory. This test never writes back into live data.

## Idle-stop primitives and isolated interruption rehearsal

`quiescent_stop.py` is a library, not a production deployment command. The
caller must fence HTTP ingress, reset existing service connections, freeze the
exact original container and accept a complete idle-worker sample. The library
then durably fences polling, alerts and SMTP test actions in the private config,
sets restart=no, queues TERM to both verified Gunicorn worker and master while
still frozen, and unfreezes. Only exit 0 without OOM is accepted; no worker
force-kill is used. Recovery restores original configuration and restart policy
and resumes only the owned original container. Application data is never rolled
back. Concurrent configuration/runtime edits fail closed for operator review.

`ingress_fence.py` owns one temporary INPUT-chain generation in IPv4 and IPv6
for an exact backend TCP port. Only root socket-marked loopback probes bypass
the fence. Removal checks ownership and preserves unexpected foreign rules.
This is an outage fence, not a permanent authentication or CSRF fix.

`rehearse_stop.py` uses fresh synthetic mail data, network=none, no published
container ports and resource limits. A separate IPv4/IPv6 loopback listener on
port18089 exercises the host firewall; it does not pretend to test production
Nginx or end-to-end mail ingress. The transient systemd worker has ExecStopPost
recovery and controlled SIGKILL injection at durable boundaries. Run only with
the shared capacity guard, using a NEW private generation and the exact retained
original image:

```sh
sudo python3 /mnt/s/code/tmrwww01-docker-scripts/security/2026-11-09/storage/capacity_guard.py \
  --peak-gib 1 -- python3 tools/maintenance/rehearse_stop.py rehearse \
  --generation /var/backups/codex-remediation-20261111/NEW_REHEARSAL \
  --image sha256:EXACT_RETAINED_ORIGINAL_IMAGE
```

These primitives still need integration into a production operator with a fresh
cutover snapshot, canonical Compose image publication, candidate warmup, explicit
activation/recovery boundaries and live acceptance. Do not deploy by calling the
library ad hoc. Controller-kill tests do not establish reboot/power-loss recovery.

Recorded idle-stop verification: 28 maintenance unit tests and 11 exact-original
image fixtures passed on November11. Ten cases durably recorded the intended
SIGKILL injection before controller exit; all recovered with original config
and restart policy, valid SQLite, HTTP200 and no remaining owned firewall chain.
All 11 fixture containers were removed. The host firewall check used a separate
loopback listener, so end-to-end production ingress remains unverified.
Production was not changed and the candidate remains undeployed.

## Integrated TLS ingress and activation rehearsal

`rehearse_ingress_activation.py` connects a temporary TLS/Basic Auth Nginx proxy
to original and candidate mail containers on a test host port. It exercises
fenced warmup, configuration activation, and rollback while preserving newly
committed deduplication and attachment records. Seven actual controller SIGKILL
cases plus normal completion must recover the original configuration, restart
policy, authenticated HTTP 200 and anonymous HTTP 401. The systemd backstop must
remove only its own firewall rules. Only newly owned synthetic containers are
removed; all generation directories remain retained.

The fixture refuses real mailboxes, SMTP and alerts. Exact images, generation
labels, mount paths, command and host network are checked before mutation.
The backend port must be unused and in 18000..18999. No production Nginx or
canonical Compose file is changed, and the production container identity,
configuration and start time are compared between cases.

Run through the shared capacity guard with a 1 GiB budget. Supply a **new local
runtime directory** and a **new SAN evidence directory**. The production mutable
data is local: the fixture must model that filesystem boundary. The initial
all-NFS fixture failed closed because NFS rejected `RENAME_NOREPLACE`; original
service recovery and fixture removal succeeded. The helper now probes that
primitive before creating any container. It does not weaken its non-overwriting
publication guarantee. SAN journals and immutable evidence remain on the SAN.

```sh
sudo python3 /mnt/s/code/tmrwww01-docker-scripts/security/2026-11-09/storage/capacity_guard.py \
  --peak-gib 1 -- python3 tools/maintenance/rehearse_ingress_activation.py rehearse \
  --generation /mnt/s/backups/PRIVATE_PARENT/NEW_INGRESS_REHEARSAL \
  --runtime-root /var/backups/PRIVATE_LOCAL_PARENT/NEW_SYNTHETIC_RUNTIME \
  --original-image sha256:EXACT_ORIGINAL_ID \
  --candidate-image sha256:EXACT_CANDIDATE_ID \
  --nginx-image sha256:EXACT_NGINX_ID
```

`ingress-activation.sanitized.json` contains status-only evidence. Private TLS
keys, generated Basic Auth credentials, journals and fixture data must stay
outside Git. This remains a rehearsal, not a production operator: fresh cutover
backup/recovery, canonical Compose publication, real provider acceptance and
reboot/power-loss recovery remain separate gates. An unchanged-image scan is
not implied by these tests.
