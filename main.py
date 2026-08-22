import logging
import os
import stat
import sys

# ── Logging setup ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Восстанавливаем GitHub deploy key из workspace-файла при каждом старте ──
_key_src = os.path.join(os.path.dirname(__file__), ".local", "keys", "github_deploy")
_key_dst = os.path.expanduser("~/.ssh/github_deploy")
_ssh_cfg = os.path.expanduser("~/.ssh/config")

try:
    if os.path.exists(_key_src):
        # ⚠️ Проверка: deploy key НЕ должен попадать в git
        repo_root = os.path.dirname(os.path.abspath(__file__))
        git_dir = os.path.join(repo_root, ".git")
        if os.path.exists(git_dir):
            import subprocess

            result = subprocess.run(
                ["git", "check-ignore", _key_src],
                capture_output=True,
                text=True,
                cwd=repo_root,
            )
            if result.returncode != 0:
                logger.warning(
                    "🚨 SECURITY: %s is tracked by git! "
                    "Add it to .gitignore immediately and rotate the key.",
                    _key_src,
                )

        os.makedirs(os.path.expanduser("~/.ssh"), mode=0o700, exist_ok=True)

        with open(_key_src, encoding="utf-8") as _f:
            _key_data = _f.read()
        with open(_key_dst, "w", encoding="utf-8") as _f:
            _f.write(_key_data)
        os.chmod(_key_dst, stat.S_IRUSR | stat.S_IWUSR)

        # SSH config — accept-new вместо no (защита от MITM)
        _cfg = (
            "Host github.com\n"
            "  HostName github.com\n"
            "  User git\n"
            "  IdentityFile ~/.ssh/github_deploy\n"
            "  StrictHostKeyChecking accept-new\n"
            "  IdentitiesOnly yes\n"
        )
        with open(_ssh_cfg, "w", encoding="utf-8") as _f:
            _f.write(_cfg)
        os.chmod(_ssh_cfg, stat.S_IRUSR | stat.S_IWUSR)

        logger.info("🔑 GitHub deploy key configured successfully.")
    else:
        logger.info("ℹ️ GitHub deploy key not found at %s — skipping.", _key_src)
except Exception as exc:
    logger.error("❌ Failed to configure deploy key: %s", exc)
    # Не прерываем запуск, но предупреждаем, что git-операции могут не работать

from app import app, socketio

if __name__ == "__main__":
    # Синхронизируем порт с Dockerfile (3000), fallback 5000 для dev
    port = int(os.environ.get("PORT", 3000))
    logger.info("🚀 Starting GRINCH-GRAM on port %d", port)

    # allow_unsafe_werkzeug нужен только для dev/Replit.
    # В production (gunicorn) этот блок не выполняется.
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
