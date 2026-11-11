# Consistent backup preparation

This Linux/root procedure snapshots the existing `voicemail-forwarder` container.
It does not deploy an image, stop/restart the service, alter mail configuration,
change its restart policy, or restore old application data.

Run the 16 standard-library unit tests with:

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
