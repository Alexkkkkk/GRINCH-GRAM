---
name: Docker cp deploy — verify after restart
description: docker cp + restart to the VPS bot container once silently reverted to the old file; always re-check md5sum after restart, not just after cp.
---

When deploying a hotfixed file to the VPS via `docker cp <file> bot-bot-1:<path> && docker restart bot-bot-1`, one run showed the OLD file content after restart even though `docker cp` alone (verified right before restart) had the new hash. Re-running `cp` + `restart` a second time was stable and the new hash persisted correctly.

**Why:** cause not fully isolated (no bind mount on that path per `docker inspect .Mounts` — only `/app/data` is a volume — so it should be a plain writable-layer file). Suspect a race between `docker cp` finishing and `docker restart` reading the file, or a transient VPS blip.

**How to apply:** after any `docker cp` + `restart` deploy to this VPS, always re-verify the file's md5sum *inside the container after the restart has completed* (not just right after the cp). Do not assume the first attempt held — confirm before telling the user the deploy is live.
