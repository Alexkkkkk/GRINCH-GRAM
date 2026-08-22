---
name: Git last-update display in dashboard
description: How the "last GitHub update" timestamp shown in the training banner is computed.
---

The dashboard shows a "Обновление с GitHub: <date time>" line in the training banner (top of
templates/index.html, `tb-meta` row). It's the last git commit's committer date, read via
`git log -1 --format=%cI` in `app.py` (`_git_last_update()`), rendered server-side into the
`index()` template context as `git_last_update`.

**Why:** the user wanted visibility into when the deployed code was last synced from GitHub,
without wiring up a GitHub API call or webhook — local git history already has this.

**How to apply:** only a *successful* git call is cached (indefinitely, since the commit date is
immutable until next deploy/restart); failures are not cached so a transient git/`.git` issue can
self-heal on the next request instead of showing "—" forever. `timeout=2` bounds worst-case
request latency. If the repo is ever deployed without a `.git` directory (e.g. a tarball), this
will always show "—" — that's expected and not a bug.
