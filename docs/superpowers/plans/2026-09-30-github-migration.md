# Migration to private GitHub and public demo access

Working directory: `/Users/infibiss/Desktop/lzt_beeline_tracker` only.
The SourceCraft checkout is read-only; never edit or push it.

1. Copy all working files including ignored local data; import main history while preserving GitHub origin.
2. Repair the copied Python environment to import this checkout.
3. Add a compact demo-login section, enabled only for the explicitly public demo password; cover normal/private login and public demo startup.
4. Add GitHub Actions: full checks/package on PR and main, Docker publication only after successful main checks. Reuse Docker Hub → Render webhook.
5. Configure repository secret through encrypted GitHub API without printing credentials.
6. Run checks, inspect diff and secret exclusions, commit and push only GitHub main.
7. Verify Actions, Docker publication, Render deployment, visible credentials and login on live site.

No paid resources or changes to source datasets. Docker context remains allowlisted.
