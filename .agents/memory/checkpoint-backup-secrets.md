---
    name: Replit checkpoint backup captures gitignored secrets
    description: A gitignored secret file (e.g. a locally-generated deploy SSH key) can still get committed to an internal Replit backup remote/branch, separate from origin.
    ---

    - Files placed under a `.gitignore`d directory (e.g. `deploy_secrets_local/`) to keep a locally-generated private key out of GitHub can still show up committed on an auxiliary remote/branch (seen as `gitsafe-backup/main`) that Replit's own checkpoint system manages — it is not the same safety boundary as `.gitignore` vs `origin`.
    **Why:** Replit's automatic checkpointing snapshots the working tree independently of what you intend to push to the user's real remote; it does not honor "I only meant this for local reference, never push it" the way a human collaborator would.
    **How to apply:** never write a real private key (SSH, API, etc.) to a file inside the project directory, even briefly and even .gitignore'd, if it must stay secret. If a locally-generated key does touch any commit (visible via `git log --all`/`git branch -a` showing an unfamiliar remote), treat it as compromised and rotate/regenerate rather than trusting the .gitignore boundary.
    