import os
import stat

# ── Восстанавливаем GitHub deploy key из workspace-файла при каждом старте ──
_key_src = os.path.join(os.path.dirname(__file__), ".local", "keys", "github_deploy")
_key_dst = os.path.expanduser("~/.ssh/github_deploy")
_ssh_cfg  = os.path.expanduser("~/.ssh/config")
try:
    if os.path.exists(_key_src):
        os.makedirs(os.path.expanduser("~/.ssh"), exist_ok=True)
        with open(_key_src) as _f:
            _key_data = _f.read()
        with open(_key_dst, "w") as _f:
            _f.write(_key_data)
        os.chmod(_key_dst, stat.S_IRUSR | stat.S_IWUSR)
        # SSH config — используем deploy key для github.com
        _cfg = (
            "Host github.com\n"
            "  HostName github.com\n"
            "  User git\n"
            "  IdentityFile ~/.ssh/github_deploy\n"
            "  StrictHostKeyChecking no\n"
        )
        with open(_ssh_cfg, "w") as _f:
            _f.write(_cfg)
        os.chmod(_ssh_cfg, stat.S_IRUSR | stat.S_IWUSR)
except Exception as _e:
    pass  # не ломаем запуск бота если что-то пошло не так

from app import app, socketio

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
