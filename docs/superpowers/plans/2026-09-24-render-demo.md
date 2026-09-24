# Render demo deployment

**Goal:** Publish the existing application as a resettable Free Web Service for organizers.

**Approved design:** One Python process, three supplied demo regions, authenticated access, HTTPS terminated by Render. SQLite and JSON live in a fresh temporary directory on each process start. No external database, paid service, changes to optimizer policy, or real employee data. SourceCraft remains the source of truth. A private Git mirror or container registry is needed because Render rejects its repository URL.

**Execution:** In this task, using planning, TDD and verification skills; existing permission to implement and publish the demo applies. No further design approval is needed.

- [x] Test public HTTPS origin: accepted domain, Secure cookies, foreign Host/Origin and forged forwarding headers rejected, CSRF still mandatory, anonymous health returns only status.
- [x] Implement explicit `public_origin` configuration in scenario_server; leave local behavior unchanged. Do not trust arbitrary forwarded headers.
- [x] Test cloud startup: mandatory origin/password, fixed 10 s morning / 3 s event budgets, isolated fresh state directory, three regions, no credential logging.
- [x] Add Render entrypoint and Dockerfile with Python dependencies and ephemeral demo state. Document reset/shared-state behavior and credentials.
- [x] Run focused tests, full `scripts/check.sh`, and public-origin HTTP smoke test using a separate temporary demo instance. Leave local user state untouched.
- [x] Prepare code source, configure Free service through Render, set secrets privately and verify deployment, login and three-region scenario flows.
- [x] Configure SourceCraft-triggered deployment after passed checks, or document the exact remaining access blocker if connection cannot be authorized.

The cloud machine's wall-clock search results must be checked on that machine. A local passing test is not a claim about Render resource capacity. Share demo access separately from source repository visibility; no secrets in Git or browser-visible client code.

## Evidence and remaining dependencies

- 556 tests passed; package and JavaScript syntax verified (`/tmp/render-demo-check.log`).
- Isolated HTTP run: all three regions' state/crew/risk/GPS/dispatcher endpoints returned 200 after login; three-profile preview returned 200 in 9.02 s. Parent RSS after preview was 410256 KiB on macOS (not a Linux container measurement and not total GPS child memory).
- Docker network recovered after login. Linux/amd64 image built and checked in a 512 MiB container across all three regions. Three previews took 9.03–9.58 s at 1 CPU. At Free-equivalent 0.1 CPU an East preview took 27.7 s. Final observed memory at 1 CPU: 364.4 MiB (not peak).
- User chose Docker Hub and explicitly authorized both uploading the image and creating/storing a Read-only token in Render. Private image `infibiss/field-router-demo:d716b32` uploaded successfully. Token expires 24.10.2026; saved in Render, not local files.
- Render service `srv-daqm427f3r2c73bbcq60` is live at https://beeline-field-router-demo.onrender.com; user supplied the admin password and Yandex values. Login, three candidates, actual YandexGPT explanation and approval passed publicly. A subsequent fact write hit the 512 MiB limit and reset the process.
- Root cause fixed in `385c468`: per-version views now retain only displayed route journeys, not the whole all-pairs database. Full solver geography is still persisted. 565 tests passed locally and in SourceCraft. Repeated 0.1 CPU / 512 MiB container test passed all three regions, three approvals and nine fact writes; cgroup peak 467013632 bytes, zero OOM events. Slowest preview 125.8 seconds (emulated local CPU), not a public Render timing claim.
- User explicitly authorized a separate Docker Hub Read & Write token (no Delete, 30 days) and storing it with the Render deploy hook in SourceCraft. Both secrets are saved; both Hub tokens expire 24.10.2026. Main now runs `release`: successful tests → build/push SHA image → deploy hook; PRs run checks only.
- First release successfully pushed `385c4687848747a01282e6fb42f1a599f2710129`. A trailing comma in the hook secret was corrected; subsequent CI diagnostics confirmed a connection timeout to Render even with the exact correct secret.

## Docker Hub trigger and remaining verification

- User explicitly approved sharing the Render hook with Docker Hub and creating the `demo` tag from the tested `9b36aa7` image.
- Commit `5f3cf89` changes CI to push only `demo`, with the full SHA in an OCI revision label; no direct Render connection from CI. Deployment guard tests: 10 passed.
- Public memory-fix version `e59274f` passed a fact write, replan and approval (East version 3), South-East preview, South-Center and GPS pages.
- After the user restored connectivity, the tested image was tagged `demo`, the approved Docker Hub webhook saved, and `5f3cf89` pushed to main. Render default image source now uses `demo`.
- Release #27 succeeded, published digest `866d0ccbb3bed6cabbe1c4a695b7ef700a2918eece661fe4538934e2b76211d8`. Docker Hub delivery history confirms success / HTTP 200 at 22:21 MSK on 24.09.2026. Render deploy `dep-daqngdvlk1mc73ee3hb0`, triggered via deploy hook, reached Live with the matching digest. The automatic chain is verified end to end.
