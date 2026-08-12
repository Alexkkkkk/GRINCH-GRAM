---
name: GitHub branch protection
description: Repository publishing constraints discovered while sending fixes from Replit.
---

The repository requires pull requests for `main`, and the connected GitHub publishing path may reject new branch pushes with `PUSH_REJECTED`. A local commit can therefore be fully verified while still not being present on GitHub.

**Why:** Direct pushes to `main` were rejected with `PULL_REQUEST_REQUIRED`; attempts to publish a new branch were rejected with `PUSH_REJECTED`, and the remote branch never appeared.

**How to apply:** Before claiming a GitHub fix is published, confirm the target branch exists with `git ls-remote` and confirm the PR URL. If branch publication is rejected, report the local commit and ask the repository owner to allow branch/PR creation or push the branch manually.