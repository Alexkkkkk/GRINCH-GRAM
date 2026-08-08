---
name: Docker cp deploy — verify after restart
description: docker cp + restart to the VPS bot container once silently reverted to the old file; always re-check md5sum after restart, not just after cp.
---

When deploying a hotfixed file to the VPS via `docker cp <file> bot-bot-1:<path>`, the cron deploy and `webhook_watcher.sh` can concurrently recreate the container, making a post-copy file disappear. One run also showed the OLD file after restart even though `docker cp` alone had the new hash.

**Why:** source files are in the container writable layer (only `/app/data` is mounted), while autonomous deployment can recreate the container during a hotfix. A direct restart can also race with the copy.

**How to apply:** pause `quantumbrain-watcher` and hold `/opt/bot/.deploy.lock`, stop the container, copy files into the stopped container, start it, wait for healthy, then verify md5 inside the running container. Restore watcher and remove the lock. Always re-check hashes and health after startup.
