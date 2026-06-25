"""
DeDust DEX клиент для реальной торговли GRINCH/TON в блокчейне TON.
Все блокчейн-операции асинхронные — запускаются через _run().
"""
import asyncio
import logging
import secrets
import time
import threading
import requests
from typing import Optional

from pytoniq import WalletV5R1, LiteBalancer, Address
from pytoniq_core import Address as CoreAddress, begin_cell
from dedust import Asset, Factory, Pool, PoolType, JettonRoot

from config import Config
from price_feed import price_feed

log = logging.getLogger(__name__)

# 1 TON = 1_000_000_000 нанотонов
TON = 1_000_000_000

# Адрес мастер-контракта DeDust Factory в мейннете
_FACTORY_ADDR = "EQBfBWT7X2BHg9tXAxzhz2aKiNTU1tSvKBUIB6mmAR0096nr"


def _run(coro):
    """Запускает async-корутину синхронно в новом event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class DedustClient:
    """
    Синхронная обёртка над DeDust SDK для использования в Flask-приложении.

    Поддерживает:
    - Получение баланса TON и GRINCH
    - Оценку выхода свопа (цена без исполнения)
    - Своп TON → GRINCH (покупка)
    - Своп GRINCH → TON (продажа)
    """

    def __init__(self, mnemonic_override: str = None):
        self._lock = threading.Lock()
        self._mnemonic: list[str] = []
        self._ready = False
        self._error: Optional[str] = None
        self._last_price: Optional[float] = None

        mnemonic_raw = mnemonic_override or Config.TON_MNEMONIC
        if not mnemonic_raw:
            self._error = "TON_MNEMONIC не задан — DeDust-режим недоступен"
            log.warning(self._error)
            return

        words = mnemonic_raw.strip().split()
        if len(words) not in (24,):
            self._error = f"Мнемоника должна содержать 24 слова, получено: {len(words)}"
            log.error(self._error)
            return

        self._mnemonic = words
        self._ready = True
        log.info("[DeDust] Клиент инициализирован ✓")

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def error(self) -> Optional[str]:
        return self._error

    # ─────────────────────────────── helpers ───────────────────────────────

    async def _make_provider(self) -> LiteBalancer:
        provider = LiteBalancer.from_mainnet_config(trust_level=1, timeout=15)
        await provider.start_up()
        return provider

    async def _wallet_and_provider(self):
        provider = await self._make_provider()
        # WalletV5R1 (W5) — версия кошелька TonKeeper пользователя; mainnet global_id = -239
        wallet = await WalletV5R1.from_mnemonic(provider=provider, mnemonics=self._mnemonic, network_global_id=-239)
        return wallet, provider

    # ─────────────────────────── balance ───────────────────────────────────

    async def _get_balance_async(self) -> dict:
        provider = await self._make_provider()
        try:
            wallet = await WalletV5R1.from_mnemonic(provider=provider, mnemonics=self._mnemonic, network_global_id=-239)
            wallet_addr = wallet.address

            # TON баланс
            state = await provider.get_account_state(wallet_addr)
            ton_nano = getattr(state, "balance", 0) or 0
            ton_bal = ton_nano / TON

            # GRINCH баланс через Jetton-кошелёк
            grinch_bal = 0.0
            try:
                grinch_root = JettonRoot.create_from_address(Config.GRINCH_TOKEN_ADDRESS)
                grinch_wallet = await grinch_root.get_wallet(wallet_addr, provider)
                g_state = await provider.get_account_state(grinch_wallet.address)
                if getattr(g_state, "state", None) and g_state.state.type_ == "active":
                    raw = await grinch_wallet.get_balance(provider)
                    grinch_bal = raw / (10 ** 9)  # GRINCH использует 9 знаков
            except Exception as e:
                log.debug(f"[DeDust] GRINCH баланс недоступен: {e}")

            return {"TON": round(ton_bal, 6), "GRINCH": round(grinch_bal, 4)}
        finally:
            await provider.close_all()

    # ───────────── низкоуровневые балансы для проверки исполнения ─────────────

    async def _grinch_balance_nano(self, provider, addr) -> int:
        """GRINCH-баланс кошелька в нанотокенах (0, если jetton-кошелёк не задеплоен)."""
        try:
            grinch_root = JettonRoot.create_from_address(Config.GRINCH_TOKEN_ADDRESS)
            gw = await grinch_root.get_wallet(addr, provider)
            g_state = await provider.get_account_state(gw.address)
            if getattr(g_state, "state", None) and g_state.state.type_ == "active":
                return await gw.get_balance(provider)
        except Exception as e:
            log.debug(f"[DeDust] grinch balance poll: {e}")
        return 0

    async def _wait_for_settlement(self, provider, addr, *, direction: str,
                                   baseline_nano: int, min_delta_nano: int,
                                   timeout: int = 75, interval: int = 7):
        """Ждёт реального изменения GRINCH-баланса после отправки свопа.

        direction="increase" — покупка (GRINCH должен прийти).
        direction="decrease" — продажа (GRINCH должен уйти).

        Возвращает текущий баланс (нано) при подтверждении или None, если за
        timeout сек изменение так и не наступило (своп отскочил / не исполнился).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(interval)
            try:
                cur = await self._grinch_balance_nano(provider, addr)
            except Exception as e:
                log.debug(f"[DeDust] settlement poll error: {e}")
                continue
            if direction == "increase" and (cur - baseline_nano) >= min_delta_nano:
                return cur
            if direction == "decrease" and (baseline_nano - cur) >= min_delta_nano:
                return cur
        return None

    def get_balance(self) -> dict:
        if not self._ready:
            return {"TON": 0.0, "GRINCH": 0.0, "error": self._error}
        try:
            return _run(self._get_balance_async())
        except Exception as e:
            log.error(f"[DeDust] get_balance ошибка: {e}")
            return {"TON": 0.0, "GRINCH": 0.0, "error": str(e)}

    # ─────────────────────────── price / estimate ──────────────────────────

    def _grinch_address(self) -> Address:
        """Возвращает Address объект для GRINCH jetton master."""
        return Address(Config.GRINCH_TOKEN_ADDRESS)

    async def _get_pool(self, provider):
        ton_asset    = Asset.native()
        grinch_asset = Asset.jetton(self._grinch_address())
        # Реальный пул GRINCH/TON задан явным адресом (нестандартная комиссия 1%).
        # Factory.get_pool вернул бы канонический адрес дефолтной комиссии,
        # которого on-chain нет, и свопы отскакивали бы.
        pool_addr = (getattr(Config, "GRINCH_POOL_ADDRESS", "") or "").strip()
        if pool_addr:
            pool = Pool.create_from_address(CoreAddress(pool_addr))
        else:
            pool = await Factory.get_pool(PoolType.VOLATILE, [ton_asset, grinch_asset], provider)
        return pool, ton_asset, grinch_asset

    async def _estimate_async(self, sell_asset, amount_nano: int) -> dict:
        provider = await self._make_provider()
        try:
            pool, ton_asset, grinch_asset = await self._get_pool(provider)
            result = await pool.get_estimated_swap_out(sell_asset, amount_nano, provider)
            return result
        finally:
            await provider.close_all()

    def get_price_ton_per_grinch(self) -> Optional[float]:
        """
        Цена 1 GRINCH в TON, рассчитанная из резервов пула.
        Кэшируется на 30 сек.
        """
        if not self._ready:
            return None
        try:
            async def _reserves():
                provider = await self._make_provider()
                try:
                    pool, _, _ = await self._get_pool(provider)
                    reserves = await pool.get_reserves(provider)
                    return reserves
                finally:
                    await provider.close_all()

            reserves = _run(_reserves())
            # reserves[0] = TON резерв (нано), reserves[1] = GRINCH резерв (нано)
            if reserves and reserves[0] > 0 and reserves[1] > 0:
                price = (reserves[0] / TON) / (reserves[1] / (10 ** 9))
                self._last_price = price
                return price
        except Exception as e:
            log.debug(f"[DeDust] get_price ошибка: {e}")
        return self._last_price

    def estimate_buy(self, ton_amount: float) -> Optional[float]:
        """Сколько GRINCH получим за ton_amount TON (без исполнения)."""
        if not self._ready:
            return None
        try:
            nano = int(ton_amount * TON)
            result = _run(self._estimate_async(Asset.native(), nano))
            return result["amount_out"] / (10 ** 9)
        except Exception as e:
            log.debug(f"[DeDust] estimate_buy ошибка: {e}")
            return None

    def estimate_sell(self, grinch_amount: float) -> Optional[float]:
        """Сколько TON получим за grinch_amount GRINCH (без исполнения)."""
        if not self._ready:
            return None
        try:
            nano = int(grinch_amount * (10 ** 9))
            grinch_asset = Asset.jetton(self._grinch_address())
            result = _run(self._estimate_async(grinch_asset, nano))
            return result["amount_out"] / TON
        except Exception as e:
            log.debug(f"[DeDust] estimate_sell ошибка: {e}")
            return None

    # ─────────────────────── защита от проскальзывания ─────────────────────

    # Максимальная допустимая «протухлость» цены для исполнения свопа (сек).
    # Прайс-фид кэширует 30 сек; на исполнение допускаем до 120 сек, иначе
    # сделка отклоняется — чтобы не торговать по устаревшей котировке.
    _PRICE_MAX_STALE = 120

    @classmethod
    def _external_prices(cls) -> tuple:
        """Возвращает (ton_usd, grinch_usd) из внешнего прайс-фида или (None, None).

        Использует max_stale, чтобы не отдавать бесконечно устаревший кэш для
        исполнения свопа.
        """
        ton_usd = price_feed.get("TON", max_stale=cls._PRICE_MAX_STALE)
        grinch_usd = price_feed.get("GRINCH", max_stale=cls._PRICE_MAX_STALE)
        if ton_usd and grinch_usd and ton_usd > 0 and grinch_usd > 0:
            return ton_usd, grinch_usd
        return None, None

    # ── Реальные резервы пула (источник истины для курса свопа) ──────────────
    # Комиссия пула GRINCH/TON на DeDust нестандартная — 1% (CPMM v2).
    _POOL_FEE = 0.01
    _RESERVES_TIMEOUT = 8

    @staticmethod
    def _same_addr(a: str, b: str) -> bool:
        """Сравнивает TON-адреса независимо от формата (EQ/UQ/raw)."""
        try:
            return (CoreAddress(a).to_str(is_user_friendly=False)
                    == CoreAddress(b).to_str(is_user_friendly=False))
        except Exception:
            return (a or "").lower() == (b or "").lower()

    def _pool_reserves(self):
        """Читает РЕАЛЬНЫЕ резервы пула (ton_reserve, grinch_reserve) через TonAPI.

        Это единственный надёжный способ узнать фактический курс именно нашего
        1%-пула: типизированные get-методы DeDust SDK на этом CPMM-v2 контракте
        падают (exit 11), а внешний USD/priceNative-фид систематически расходится
        с пулом — из-за чего min-out оказывался завышен и пул отклонял свопы
        (exit 65535, bounce). По резервам считаем выход свопа точной формулой CPMM.

        Возвращает (ton_reserve, grinch_reserve) в обычных единицах или None.
        """
        pool = Config.GRINCH_POOL_ADDRESS
        try:
            r1 = requests.get(
                f"https://tonapi.io/v2/accounts/{pool}",
                headers={"Accept": "application/json"}, timeout=self._RESERVES_TIMEOUT,
            )
            ton_reserve = (r1.json().get("balance", 0) or 0) / TON
            r2 = requests.get(
                f"https://tonapi.io/v2/accounts/{pool}/jettons",
                headers={"Accept": "application/json"}, timeout=self._RESERVES_TIMEOUT,
            )
            grinch_reserve = None
            for b in r2.json().get("balances", []):
                jaddr = (b.get("jetton", {}) or {}).get("address", "")
                if self._same_addr(jaddr, Config.GRINCH_TOKEN_ADDRESS):
                    grinch_reserve = float(b.get("balance", 0)) / TON
                    break
            if ton_reserve > 0 and grinch_reserve and grinch_reserve > 0:
                return ton_reserve, grinch_reserve
        except Exception as e:  # noqa: BLE001
            log.warning(f"Не удалось прочитать резервы пула: {e}")
        return None

    def _cpmm_out(self, amount_in: float, reserve_in: float, reserve_out: float) -> float:
        """Точный выход свопа по формуле постоянного произведения (с комиссией 1%)."""
        amt = amount_in * (1 - self._POOL_FEE)
        return reserve_out * amt / (reserve_in + amt)

    def _min_out_buy_grinch(self, ton_amount: float):
        """Минимум GRINCH (нано), который должен прийти за ton_amount TON.

        Приоритет источников курса:
          1) РЕАЛЬНЫЕ резервы пула (точная CPMM-формула) — самый надёжный;
          2) priceNative пула (DexScreener) — серединная цена;
          3) перекрёстный USD-курс — последний резерв.
        Возвращает (min_nano, expected_grinch) или (None, None), если курс получить
        не удалось — тогда сделку нужно отклонить, а НЕ слать своп без защиты.
        """
        reserves = self._pool_reserves()
        if reserves:
            rt, rg = reserves
            expected_grinch = self._cpmm_out(ton_amount, rt, rg)
        else:
            ton_per_grinch = price_feed.get_grinch_ton_price(max_stale=self._PRICE_MAX_STALE)
            if ton_per_grinch and ton_per_grinch > 0:
                expected_grinch = ton_amount / ton_per_grinch
            else:
                ton_usd, grinch_usd = self._external_prices()
                if ton_usd is None:
                    return None, None
                expected_grinch = (ton_amount * ton_usd) / grinch_usd
        min_grinch = expected_grinch * (1 - Config.SLIPPAGE_PCT / 100.0)
        return int(min_grinch * (10 ** 9)), expected_grinch

    def _min_out_sell_ton(self, grinch_amount: float):
        """Минимум TON (нано), который должен прийти за grinch_amount GRINCH.

        Источники курса в том же приоритете, что и для покупки.
        Возвращает (min_nano, expected_ton) или (None, None), если цены нет.
        """
        reserves = self._pool_reserves()
        if reserves:
            rt, rg = reserves
            expected_ton = self._cpmm_out(grinch_amount, rg, rt)
        else:
            ton_per_grinch = price_feed.get_grinch_ton_price(max_stale=self._PRICE_MAX_STALE)
            if ton_per_grinch and ton_per_grinch > 0:
                expected_ton = grinch_amount * ton_per_grinch
            else:
                ton_usd, grinch_usd = self._external_prices()
                if ton_usd is None:
                    return None, None
                expected_ton = (grinch_amount * grinch_usd) / ton_usd
        min_ton = expected_ton * (1 - Config.SLIPPAGE_PCT / 100.0)
        return int(min_ton * TON), expected_ton

    # ─────────────── построение тела свопа (op 0xa5a7cbf8) ──────────────────
    # Пул GRINCH/TON — нестандартный CPMM-v2: своп исполняется сообщением
    # op 0xa5a7cbf8, отправленным НАПРЯМУЮ в пул (покупка — нативный TON прямо
    # в пул, без native-vault; продажа — jetton-transfer GRINCH в пул с
    # forward-payload свопа). Канонический dedust-SDK 1.1.4 шлёт легаси-op
    # 0x61ee542d через vault, который ЭТОТ контракт не понимает → exit 65535
    # (bounce). Формат тела выведён обратной разработкой реальных успешных
    # сделок и проверен ПОБАЙТОВО: реконструкция оригинальных тел из этих же
    # билдеров совпадает бит-в-бит. Константы ниже подтверждены тем же путём.
    _SWAP_OP        = 0xa5a7cbf8   # своп (root для покупки / forward для продажи)
    _LIMITS_PREFIX  = 0xc442500f   # ref0: префикс ячейки лимитов
    _PARAMS_C2      = 0x400        # ref1: константа-разделитель перед hash получателя
    _BUY_PARAMS_C1  = 0x800        # ref1: маркер направления — покупка
    _SELL_PARAMS_C1 = 0x801        # ref1: маркер направления — продажа
    _SELL_FP_PREFIX = 0xcbc33949   # forward-payload продажи: префикс
    _JETTON_XFER_OP = 0x0f8a7ea5   # стандартный jetton transfer

    def _build_limits_cell(self, min_out_nano: int, deadline: int):
        """ref0: префикс + min_out:Coins + 8 нулей + deadline:uint32 + 3 нуля."""
        return (begin_cell()
                .store_uint(self._LIMITS_PREFIX, 32)
                .store_coins(min_out_nano)
                .store_uint(0, 8)
                .store_uint(deadline, 32)
                .store_uint(0, 3)
                .end_cell())

    def _build_params_cell(self, recipient, c1: int):
        """ref1: адрес получателя + пустой реферал + c1 + salt + c2 + hash получателя.

        salt — 256-битный случайный id; пул его не валидирует (проверено:
        соответствующие аккаунты не существуют on-chain).
        """
        salt = secrets.randbits(256)
        recip_hash = int.from_bytes(recipient.hash_part, "big")
        return (begin_cell()
                .store_address(recipient)
                .store_address(None)
                .store_uint(c1, 16)
                .store_uint(salt, 256)
                .store_uint(self._PARAMS_C2, 16)
                .store_uint(recip_hash, 256)
                .end_cell())

    def _build_buy_body(self, recipient, amount_nano: int, min_out_nano: int, deadline: int):
        """Тело покупки: op 0xa5a7cbf8 — отправляется НАПРЯМУЮ в пул с нативным TON."""
        return (begin_cell()
                .store_uint(self._SWAP_OP, 32)
                .store_uint(secrets.randbits(64), 64)
                .store_coins(amount_nano)
                .store_ref(self._build_limits_cell(min_out_nano, deadline))
                .store_ref(self._build_params_cell(recipient, self._BUY_PARAMS_C1))
                .end_cell())

    def _build_sell_transfer_body(self, recipient, pool_addr, grinch_nano: int,
                                  min_out_nano: int, deadline: int, fwd_nano: int):
        """Тело продажи: jetton-transfer GRINCH НАПРЯМУЮ в пул с forward-payload свопа."""
        forward_payload = (begin_cell()
                           .store_uint(self._SELL_FP_PREFIX, 32)
                           .store_ref(self._build_limits_cell(min_out_nano, deadline))
                           .store_ref(self._build_params_cell(recipient, self._SELL_PARAMS_C1))
                           .end_cell())
        return (begin_cell()
                .store_uint(self._JETTON_XFER_OP, 32)
                .store_uint(secrets.randbits(64), 64)
                .store_coins(grinch_nano)
                .store_address(pool_addr)        # destination = ПУЛ
                .store_address(recipient)        # response_destination
                .store_maybe_ref(None)           # custom_payload = нет
                .store_coins(fwd_nano)           # forward_ton_amount
                .store_bit(1)                    # forward_payload в ref
                .store_ref(forward_payload)
                .end_cell())

    # ─────────────────────────── swap: buy ─────────────────────────────────

    async def _buy_async(self, ton_amount: float) -> dict:
        """TON → GRINCH: отправляем нативный TON НАПРЯМУЮ в пул с payload свопа."""
        # Защита от проскальзывания: считаем min-out ДО отправки средств.
        min_out_nano, expected_grinch = self._min_out_buy_grinch(ton_amount)
        if min_out_nano is None:
            return {
                "ok": False,
                "side": "buy",
                "error": (
                    "Нет актуальной цены GRINCH/TON для расчёта защиты от "
                    "проскальзывания — сделка отклонена (своп без min-out не "
                    "отправляется во избежание убыточного курса)."
                ),
            }

        wallet, provider = await self._wallet_and_provider()
        try:
            pool, ton_asset, _ = await self._get_pool(provider)

            amount_nano = int(ton_amount * TON)
            # Покупка шлёт нативный TON НАПРЯМУЮ в пул (op 0xa5a7cbf8); пул
            # берёт газ на выдачу GRINCH (с деплоем jetton-кошелька покупателя
            # при необходимости) и возвращает излишек. Реальные сделки
            # укладываются в ~0.2 TON; берём 0.3 TON с запасом.
            gas_nano    = int(0.3 * TON)

            # ── Preflight: хватает ли TON на сумму свопа + газ? ──────────────
            # Покупка отправляет amount_nano (на своп) + gas_nano (газ/комиссии).
            # Если на кошельке меньше — НЕ отправляем операцию вовсе, чтобы не
            # сжечь газ на заведомо неисполнимой транзакции.
            state = await provider.get_account_state(wallet.address)
            ton_nano = getattr(state, "balance", 0) or 0
            needed_nano = amount_nano + gas_nano + int(0.05 * TON)  # +запас на комиссии сети
            if ton_nano < needed_nano:
                return {
                    "ok": False,
                    "side": "buy",
                    "error": (
                        f"Недостаточно TON на кошельке платформы: есть "
                        f"{ton_nano / TON:.3f} TON, нужно ≥ {needed_nano / TON:.2f} TON "
                        f"(своп {ton_amount:.3f} + газ). Покупка отклонена."
                    ),
                    "need_ton": round(needed_nano / TON, 2),
                    "have_ton": round(ton_nano / TON, 3),
                }

            deadline = int(time.time()) + 300
            body = self._build_buy_body(
                recipient=wallet.address,
                amount_nano=amount_nano,
                min_out_nano=min_out_nano,
                deadline=deadline,
            )

            # Базовый GRINCH-баланс ДО свопа — для проверки реального исполнения.
            baseline_nano = await self._grinch_balance_nano(provider, wallet.address)

            # Своп шлётся НАПРЯМУЮ в пул (не через native-vault SDK).
            await wallet.transfer(
                destination=pool.address,
                amount=amount_nano + gas_nano,
                body=body,
            )

            # ── Проверка реального исполнения on-chain ───────────────────────
            # wallet.transfer лишь ШИРОКОВЕЩАЕТ транзакцию; своп в пуле может
            # отскочить (bounce) уже после отправки. Поэтому ждём, пока GRINCH
            # реально поступит. Требуем хотя бы половину ожидаемого объёма.
            min_delta = int(expected_grinch * 0.5 * (10 ** 9))
            confirmed = await self._wait_for_settlement(
                provider, wallet.address, direction="increase",
                baseline_nano=baseline_nano, min_delta_nano=min_delta,
            )
            if confirmed is None:
                return {
                    "ok": False,
                    "side": "buy",
                    "broadcast": True,
                    "error": (
                        "Своп отправлен, но GRINCH не поступил — ордер отскочил "
                        "(bounce) в пуле DeDust. TON возвращён на кошелёк (минус "
                        "сетевой газ). Вероятные причины: проскальзывание выше "
                        f"{Config.SLIPPAGE_PCT}% или нехватка ликвидности."
                    ),
                }

            return {
                "ok": True,
                "side": "buy",
                "ton_spent": ton_amount,
                "pool": str(pool.address),
                "min_grinch_out": round(min_out_nano / (10 ** 9), 6),
                "expected_grinch": round(expected_grinch, 6),
                "grinch_received": round((confirmed - baseline_nano) / (10 ** 9), 6),
                "slippage_pct": Config.SLIPPAGE_PCT,
            }
        finally:
            await provider.close_all()

    def buy(self, ton_amount: float) -> dict:
        """Покупка GRINCH за TON через DeDust. Блокирует до завершения транзакции."""
        if not self._ready:
            return {"ok": False, "error": self._error}
        # Сериализуем свопы на общем кастодиальном кошельке: проверка исполнения
        # опирается на изменение GRINCH-баланса, поэтому параллельные buy/sell
        # могли бы дать ложный результат. Лок гарантирует один своп за раз.
        with self._lock:
            try:
                return _run(self._buy_async(ton_amount))
            except Exception as e:
                log.error(f"[DeDust] buy ошибка: {e}")
                return {"ok": False, "error": str(e)}

    # ─────────────────────────── swap: sell ────────────────────────────────

    async def _sell_async(self, grinch_amount: float) -> dict:
        """GRINCH → TON: jetton-transfer GRINCH НАПРЯМУЮ в пул с forward-payload свопа.

        Газ: 0.35 TON прикладывается к сообщению; 0.25 TON форвардится в пул на
        исполнение свопа. Излишек возвращается на кошелёк.
        """
        # Защита от проскальзывания: считаем min-out TON ДО перевода жеттонов.
        min_out_nano, expected_ton = self._min_out_sell_ton(grinch_amount)
        if min_out_nano is None:
            return {
                "ok": False,
                "side": "sell",
                "error": (
                    "Нет актуальной цены GRINCH/TON для расчёта защиты от "
                    "проскальзывания — продажа отклонена (своп без min-out не "
                    "отправляется во избежание убыточного курса)."
                ),
            }

        wallet, provider = await self._wallet_and_provider()
        try:
            pool, _, grinch_asset = await self._get_pool(provider)

            grinch_root   = JettonRoot.create_from_address(Config.GRINCH_TOKEN_ADDRESS)
            grinch_wallet = await grinch_root.get_wallet(wallet.address, provider)

            amount_nano = int(grinch_amount * (10 ** 9))
            # Продажа: jetton-transfer GRINCH НАПРЯМУЮ в пул с forward-payload
            # свопа. Реальные сделки форвардят 0.25 TON; суммарно к сообщению
            # прикладываем 0.35 TON газа (излишек возвращается на кошелёк).
            gas_nano = int(0.35 * TON)
            fwd_nano = int(0.25 * TON)

            # ── Preflight: хватает ли TON на газ? ──────────────────────────
            # Своп-сообщение несёт gas_nano TON и форвардит fwd_nano в пул.
            # Если на кошельке меньше газа, транзакция уйдёт, но своп в пуле
            # упадёт (нечем оплатить forward) → GRINCH вернётся, газ сгорит.
            state = await provider.get_account_state(wallet.address)
            ton_nano = getattr(state, "balance", 0) or 0
            needed_nano = gas_nano + int(0.05 * TON)  # газ + запас на комиссии сети
            if ton_nano < needed_nano:
                return {
                    "ok": False,
                    "side": "sell",
                    "error": (
                        f"Недостаточно TON для газа: на кошельке "
                        f"{ton_nano / TON:.3f} TON, нужно ≥ {needed_nano / TON:.2f} TON. "
                        f"Пополните кошелёк TON, чтобы продать GRINCH."
                    ),
                    "need_ton": round(needed_nano / TON, 2),
                    "have_ton": round(ton_nano / TON, 3),
                }

            deadline = int(time.time()) + 600   # 10 мин
            transfer_body = self._build_sell_transfer_body(
                recipient=wallet.address,
                pool_addr=pool.address,
                grinch_nano=amount_nano,
                min_out_nano=min_out_nano,
                deadline=deadline,
                fwd_nano=fwd_nano,
            )

            # Базовый GRINCH-баланс ДО свопа — для проверки реального исполнения.
            baseline_nano = await self._grinch_balance_nano(provider, wallet.address)

            # GRINCH уходит jetton-transfer'ом В ПУЛ (destination=пул); своп
            # исполняется внутри пула по forward-payload. Сообщение шлём на наш
            # GRINCH jetton-кошелёк, он маршрутизирует жетоны в пул.
            await wallet.transfer(
                destination=grinch_wallet.address,
                amount=gas_nano,
                body=transfer_body,
            )

            # ── Проверка реального исполнения on-chain ───────────────────────
            # Если своп отскочит, GRINCH вернётся на кошелёк и баланс НЕ
            # уменьшится. Ждём фактического списания (хотя бы половины объёма).
            min_delta = int(grinch_amount * 0.5 * (10 ** 9))
            confirmed = await self._wait_for_settlement(
                provider, wallet.address, direction="decrease",
                baseline_nano=baseline_nano, min_delta_nano=min_delta,
            )
            if confirmed is None:
                return {
                    "ok": False,
                    "side": "sell",
                    "broadcast": True,
                    "error": (
                        "Своп отправлен, но GRINCH не списался — ордер отскочил "
                        "(bounce) в пуле DeDust. GRINCH возвращён на кошелёк "
                        "(минус сетевой газ). Вероятные причины: проскальзывание "
                        f"выше {Config.SLIPPAGE_PCT}% или нехватка ликвидности."
                    ),
                }

            return {
                "ok": True,
                "side": "sell",
                "grinch_spent": grinch_amount,
                "pool": str(pool.address),
                "min_ton_out": round(min_out_nano / TON, 6),
                "expected_ton": round(expected_ton, 6),
                "grinch_sold": round((baseline_nano - confirmed) / (10 ** 9), 6),
                "slippage_pct": Config.SLIPPAGE_PCT,
            }
        finally:
            await provider.close_all()

    def sell(self, grinch_amount: float) -> dict:
        """Продажа GRINCH за TON через DeDust. Блокирует до завершения транзакции."""
        if not self._ready:
            return {"ok": False, "error": self._error}
        # Сериализуем свопы (см. комментарий в buy): один своп за раз, иначе
        # параллельные операции исказят проверку GRINCH-баланса.
        with self._lock:
            try:
                return _run(self._sell_async(grinch_amount))
            except Exception as e:
                log.error(f"[DeDust] sell ошибка: {e}")
                return {"ok": False, "error": str(e)}

    # ─────────────────────────── transfer TON ──────────────────────────────

    async def _send_ton_async(self, recipient: str, amount_ton: float) -> dict:
        """Отправка TON на указанный адрес (для сбора комиссии платформы)."""
        wallet, provider = await self._wallet_and_provider()
        try:
            dest = Address(recipient)
            await wallet.transfer(
                destination=dest,
                amount=int(amount_ton * TON),
            )
            return {"ok": True, "amount": amount_ton, "to": recipient}
        finally:
            await provider.close_all()

    def send_ton(self, recipient: str, amount_ton: float) -> dict:
        """Отправляет amount_ton TON на адрес recipient (комиссия платформы)."""
        if not self._ready:
            return {"ok": False, "error": self._error}
        if amount_ton <= 0:
            return {"ok": False, "error": "amount <= 0"}
        try:
            return _run(self._send_ton_async(recipient, amount_ton))
        except Exception as e:
            log.error(f"[DeDust] send_ton ошибка: {e}")
            return {"ok": False, "error": str(e)}

    def get_wallet_address(self) -> Optional[str]:
        """Возвращает адрес кошелька (EQ-формат) без подключения к сети."""
        if not self._ready:
            return None
        try:
            async def _addr():
                provider = await self._make_provider()
                try:
                    wallet = await WalletV5R1.from_mnemonic(
                        provider=provider, mnemonics=self._mnemonic, network_global_id=-239
                    )
                    return wallet.address.to_str(is_user_friendly=True, is_bounceable=True)
                finally:
                    await provider.close_all()
            return _run(_addr())
        except Exception as e:
            log.debug(f"[DeDust] get_wallet_address ошибка: {e}")
            return None


# Синглтон — создаётся один раз при импорте
dedust_client = DedustClient()
