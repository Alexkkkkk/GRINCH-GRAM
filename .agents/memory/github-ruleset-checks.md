---
name: GitHub ruleset required checks
description: GitHub branch rulesets must use the exact check-run names published by the workflow.
---

Для обязательных проверок в GitHub ruleset нужно указывать точные имена check-run, которые публикует workflow (например, `lint` и `docker-build`), а не имя workflow с префиксом вроде `CI / lint`.

**Почему:** GitHub блокирует merge, если контекст обязательной проверки не совпадает с фактическим именем check-run, даже когда сама проверка успешно завершилась.

**Как применять:** после изменения CI или ruleset сверять `actions/runs/.../jobs` и `commits/.../check-runs` с `required_status_checks` перед попыткой merge.