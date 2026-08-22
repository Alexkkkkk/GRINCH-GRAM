---
name: GitHub CI quality gates
description: Decision for safe automation around this trading project’s GitHub workflows
---

GitHub automation must never silently rewrite the trading branch. Formatting, lint
fixes, and AI-generated documentation are proposed through reviewable pull requests.
Blocking checks should focus first on correctness, security, dependency risk, and
container buildability; broad style cleanup belongs in a separate PR.

**Why:** This repository contains financial calculations, blockchain operations, and
background workers. An automatic direct push could change live trading behavior without
human review.

**How to apply:** Keep CI required and fail-closed for real defects. Keep auto-fix
workflows scoped to safe fixes, use least-privilege permissions, and require review
before merging generated changes.