# Single-service production rollout

`production_rollout.py` integrates the previously separate recovery primitives.
It supports this service's two writable bind mounts, host networking, single
Gunicorn worker and existing simple Compose service. It refuses unexpected
dependencies, writable mount overlap, configuration drift or a foreign container.
The global nonblocking operation lock prevents overlapping prepare, worker and
recovery commands. Keep the maintained storage capacity guard around heavy work.

Preparation creates a new private SAN journal and a separate local restore
directory. It reads production but does not change the service. Record and supply
the current container ID, image ID, runtime fingerprint and Compose SHA-256.
Inspect active maintenance units before preparation. Use the merged application
with explicit maintenance support; the older candidate cannot safely warm up.

The worker performs these operations in order:

1. Fence the backend TCP port in IPv4 and IPv6, terminate existing inbound
   connections, freeze the verified idle worker, publish the maintenance
   configuration and stop gracefully. It never force-kills a mail worker.
2. Stream a consistent data/log snapshot directly to the SAN and verify the
   manifest. Copy it to an independent local restore. Check SQLite with the
   retained original runtime, then boot the candidate with no network and explicit
   maintenance. Verify deduplication and attachment identities without using any
   real provider. The original-runtime check here is structural; retain the
   earlier full original-version boot/restart proof separately.
3. Remove only the stopped original container, retaining its exact image,
   runtime configuration, private configuration, snapshots and bind mounts.
   Recreate the canonical container using the Docker Engine API. There is no
   `docker compose up` for the whole stack and no volume removal.
4. Verify the prepared runtime template, start the candidate while maintenance
   remains active, and confirm it has not changed deduplication or attachments.
   Publish only the selected Compose image with a semantic check and a durable
   non-overwriting file operation. Retain unrelated bytes, comments and edits.
5. Restore the application configuration to activate work, restore the restart
   policy, check local readiness, remove the owned fence and require anonymous
   HTTPS status 401 at the supplied public route. Record committed status.

The recreated runtime retains the original environment, command, mounts and
resource limits. Expected differences are the exact image reference and recorded
Compose/generation labels. Docker's normalization of unspecified
`OomKillDisable: null` to the default `false` is accepted; `true` is rejected.
Every other Config/HostConfig/mount difference fails verification. See the
[Docker Engine create API](https://docs.docker.com/reference/api/engine/version/v1.51/)
and [Compose config command](https://docs.docker.com/reference/cli/docker/compose/config/).

## Recovery and retained evidence

Recovery fences ingress, stops only the owned candidate at a verified idle
boundary, and recreates the exact original image from its retained configuration.
The newly created container's ID changes. Generation labels allow recovery even
if atomic container creation succeeded just before the controller died.

Recovery never copies an older snapshot over live data. It preserves fresh
deduplication/attachment records and valid application configuration changes.
It reverses only its Compose image change while preserving unrelated edits. An
edit to the selected service or an ambiguous/foreign file or container causes a
conflict and is preserved for review. Private journals include enough state to
resume interrupted file publication. Running `recover` again is safe; a committed
rollout requires explicit `recover --force` to request rollback.

All snapshots, private inspect/configuration documents, journals and local
restore data remain retained for individual review. Export only
`rollout-*.sanitized.json`; never commit private journals or application data.
The operator requires the application's configuration to have no ACL extension;
it refuses unsupported ACLs during preparation. Canonical Compose publication
preserves its existing owner, group, mode and ACL. It makes no shared-tree
ownership or permission changes.

## Invocation

Run from the maintained repository at the reviewed merge commit. Before a live
run, verify required CI/reviews, the candidate's exact image/source evidence,
available storage, stopped-service constraints and the absence of another
maintenance job. Determine `PEAK_GIB` from the fresh restore's measured size and
the guard's reserve requirement; the immutable snapshot streams to SAN.

```sh
sudo python3 tools/maintenance/production_rollout.py prepare \
  --generation /mnt/s/backups/PRIVATE_PARENT/NEW_PRODUCTION_GENERATION \
  --runtime-root /var/backups/PRIVATE_LOCAL_PARENT/NEW_RESTORE \
  --container voicemail-forwarder \
  --expected-container EXACT_CONTAINER_ID \
  --expected-fingerprint EXACT_RUNTIME_SHA256 \
  --original-image sha256:EXACT_ORIGINAL_IMAGE_ID \
  --candidate-image sha256:EXACT_VERIFIED_CANDIDATE_IMAGE_ID \
  --compose /home/dblagbro/docker/docker-compose.yml \
  --expected-compose EXACT_COMPOSE_SHA256 \
  --anonymous-url https://PUBLIC_HOST/voicemail-forwarder/
```

Use an absolute helper path in both unit commands. Keep the capacity guard in
the foreground around `systemd-run --wait` so it covers the recovery backstop.
Do not introduce a timeout that would force an unsafe production stop.

```sh
sudo python3 /mnt/s/code/tmrwww01-docker-scripts/security/2026-11-09/storage/capacity_guard.py \
  --path / --peak-gib PEAK_GIB -- systemd-run --wait --collect \
  --unit=codex-vmail-production-UNIQUE_GENERATION \
  --property=Type=oneshot --property=TimeoutStartSec=infinity \
  --property=TimeoutStopSec=infinity \
  --property='ExecStopPost=/usr/bin/python3 /ABSOLUTE_REPO/tools/maintenance/production_rollout.py recover --generation /ABSOLUTE_GENERATION' \
  /usr/bin/python3 /ABSOLUTE_REPO/tools/maintenance/production_rollout.py worker \
  --generation /ABSOLUTE_GENERATION
```

After a controller interruption, inspect the durable journal and actual runtime
before issuing another command. Do not launch a second forward worker. Automatic
recovery handles controller exit through `ExecStopPost`; a host reboot or power
loss has not been proven by these tests. Retain the explicit recovery command
and the original image for that operational case.

After successful rollout, reconcile the exact deployed image/configuration and
sanitized receipt into the maintained deployment repository through its review
process. Treat source-persisted, PR-open, checks-passed, merged and deployed as
separate states. An operator merge is not a production deployment.

## Repeatable isolated verification

Run unit tests with `python3 -m unittest discover -s tools/maintenance -v`.
`rehearse_production_rollout.py` calls the same forward/recovery operator through
systemd using empty mailboxes, generated TLS/auth credentials and test-only ports.
It checks normal rollback, controller SIGKILL boundaries, fresh writes after
activation, and concurrent application/Compose edits. Production identity,
runtime configuration, start time and canonical Compose hash are compared
between cases. Only owned fixture containers/rules are removed; all generations
are retained.

```sh
sudo python3 /mnt/s/code/tmrwww01-docker-scripts/security/2026-11-09/storage/capacity_guard.py \
  --path / --peak-gib 1 -- python3 tools/maintenance/rehearse_production_rollout.py rehearse \
  --generation /mnt/s/backups/PRIVATE_PARENT/NEW_OPERATOR_REHEARSAL \
  --runtime-root /var/backups/PRIVATE_LOCAL_PARENT/NEW_FIXTURE_RUNTIME \
  --original-image sha256:EXACT_ORIGINAL_ID \
  --candidate-image sha256:EXACT_CANDIDATE_ID \
  --nginx-image sha256:EXACT_NGINX_ID
```

The receipt is `production-operator.sanitized.json`. TLS/auth checks use a
synthetic proxy on test ports; they do not prove authenticated production user
workflows, mail-provider acceptance, exploitability or reboot recovery. Reusing
an unchanged candidate is not a new package scan.
