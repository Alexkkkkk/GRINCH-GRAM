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

Дефолтный таймаут: все запросы через SESSION автоматически получают
timeout=10с, если вызывающий код не указал явный timeout=....
Это страховка от зависания при недоступных внешних API.
"""
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None

# Дефолтный таймаут (connect, read) в секундах.
# Используется если вызывающий код не передал явный timeout=.
_DEFAULT_TIMEOUT = 10


class _TimeoutSession(requests.Session):
    """requests.Session с дефолтным таймаутом на все запросы.

    requests.Session сам по себе не поддерживает глобальный timeout —
    его нужно указывать при каждом вызове. Этот класс подставляет
    _DEFAULT_TIMEOUT если вызывающий код забыл или намеренно не указал.
    Явный timeout= всегда имеет приоритет (kwargs.setdefault).
    """
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        return super().request(method, url, **kwargs)


SESSION = _TimeoutSession()

_retry_kwargs = dict(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(429, 502, 503, 504),
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
