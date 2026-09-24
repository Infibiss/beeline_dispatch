# Render demo deployment

**Goal:** Publish the existing application as a resettable Free Web Service for organizers.

**Approved design:** One Python process, three supplied demo regions, authenticated access, HTTPS terminated by Render. SQLite and JSON live in a fresh temporary directory on each process start. No external database, paid service, changes to optimizer policy, or real employee data. SourceCraft remains the source of truth. A private Git mirror or container registry is needed because Render rejects its repository URL.

**Execution:** In this task, using planning, TDD and verification skills; existing permission to implement and publish the demo applies. No further design approval is needed.

- [x] Test public HTTPS origin: accepted domain, Secure cookies, foreign Host/Origin and forged forwarding headers rejected, CSRF still mandatory, anonymous health returns only status.
- [x] Implement explicit `public_origin` configuration in scenario_server; leave local behavior unchanged. Do not trust arbitrary forwarded headers.
- [x] Test cloud startup: mandatory origin/password, fixed 10 s morning / 3 s event budgets, isolated fresh state directory, three regions, no credential logging.
- [x] Add Render entrypoint and Dockerfile with Python dependencies and ephemeral demo state. Document reset/shared-state behavior and credentials.
- [x] Run focused tests, full `scripts/check.sh`, and public-origin HTTP smoke test using a separate temporary demo instance. Leave local user state untouched.
- [ ] Prepare code source, configure Free service through Render, set secrets privately and verify deployment, login and three-region scenario flows.
- [ ] Configure SourceCraft-triggered deployment after passed checks, or document the exact remaining access blocker if connection cannot be authorized.

The cloud machine's wall-clock search results must be checked on that machine. A local passing test is not a claim about Render resource capacity. Share demo access separately from source repository visibility; no secrets in Git or browser-visible client code.

## Evidence and remaining dependencies

- 556 tests passed; package and JavaScript syntax verified (`/tmp/render-demo-check.log`).
- Isolated HTTP run: all three regions' state/crew/risk/GPS/dispatcher endpoints returned 200 after login; three-profile preview returned 200 in 9.02 s. Parent RSS after preview was 410256 KiB on macOS (not a Linux container measurement and not total GPS child memory).
- Docker network recovered after login. Linux/amd64 image built and checked in a 512 MiB container across all three regions. Three previews took 9.03–9.58 s at 1 CPU. At Free-equivalent 0.1 CPU an East preview took 27.7 s. Final observed memory at 1 CPU: 364.4 MiB (not peak).
- User chose Docker Hub and explicitly authorized both uploading the image and creating/storing a Read-only token in Render. Private image `infibiss/field-router-demo:d716b32` uploaded successfully. Token expires 24.10.2026; saved in Render, not local files.
- `d716b32` pushed to SourceCraft main. Render form: Free, Frankfurt, `beeline-field-router-demo`, `/healthz`; image connection succeeded. Creation awaits user entry of new admin password and LLM values. Service is not yet live.
- Deployment helper prepared and seven failure/success guard tests passed. Not wired into CI until Render service and deployment secrets exist.
