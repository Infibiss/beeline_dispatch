# Repository boundary

Develop only in `/Users/infibiss/Desktop/lzt_beeline_tracker`.
The only authorized push destination is `https://github.com/Infibiss/lzt_beeline_tracker.git`.
Do not edit or push the former `case-13-field-engineer-routing-team-40` checkout or SourceCraft repository.

Keep `.env`, keys, local databases and saved runs out of Git and Docker.
The fixed public demo credential in `auth_http.py` was explicitly authorized for publication by the owner.
Run `PATH="$PWD/.venv/bin:$PATH" sh scripts/check.sh` before pushing app changes.
GitHub main deploys through `.github/workflows/ci.yml` → Docker Hub `infibiss/field-router-demo:demo` → Render.
