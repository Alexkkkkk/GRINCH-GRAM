"""
http_client.py — общая HTTP-сессия с keep-alive пулом соединений.

Раньше каждый requests.get()/post() открывал новое TCP+TLS соединение —
это самая дорогая часть похода во внешний API (DexScreener, GeckoTerminal,
CoinGecko, TonCenter). Один общий requests.Session() с настроенным
HTTPAdapter переиспользует уже открытые соединения (keep-alive),
что на практике ускоряет повторные запросы к тому же хосту в разы
(нет повторного TCP/TLS handshake).

Использование: замените `requests.get(...)` на `SESSION.get(...)`
(сигнатура полностью совместима — это тот же requests API).
"""
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None

SESSION = requests.Session()

_retry_kwargs = dict(
    total=1,
    backoff_factor=0.2,
    status_forcelist=(502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
)

if Retry is not None:
    try:
        _retry = Retry(**_retry_kwargs)
    except TypeError:
        _retry_kwargs["method_whitelist"] = _retry_kwargs.pop("allowed_methods")
        _retry = Retry(**_retry_kwargs)
    _adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=_retry)
else:
    _adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)

SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)
