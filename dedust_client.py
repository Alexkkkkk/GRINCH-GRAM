"""
DeDust DEX клиент для реальной торговли GRINCH/TON в блокчейне TON.
Все блокчейн-операции асинхронные — запускаются через _run().
"""
import asyncio
import logging
import time
import threading
from typing import Optional

from pytoniq import WalletV5R1, LiteBalancer, Address
from pytoniq_core import Address as CoreAddress
from dedust import Asset, Factory, Pool, PoolType, VaultNative, VaultJetton, JettonRoot, SwapParams

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

    def _min_out_buy_grinch(self, ton_amount: float):
        """Минимум GRINCH (нано), который должен прийти за ton_amount TON.

        Рассчитывается от справедливой цены внешнего фида с буфером SLIPPAGE_PCT
        (комиссия пула + проскальзывание). Возвращает (min_nano, expected_grinch)
        или (None, None), если цену получить не удалось — тогда сделку нужно
        отклонить, а НЕ слать своп без защиты.
        """
        ton_usd, grinch_usd = self._external_prices()
        if ton_usd is None:
            return None, None
        expected_grinch = (ton_amount * ton_usd) / grinch_usd
        min_grinch = expected_grinch * (1 - Config.SLIPPAGE_PCT / 100.0)
        return int(min_grinch * (10 ** 9)), expected_grinch

    def _min_out_sell_ton(self, grinch_amount: float):
        """Минимум TON (нано), который должен прийти за grinch_amount GRINCH.

        Возвращает (min_nano, expected_ton) или (None, None), если цены нет.
        """
        ton_usd, grinch_usd = self._external_prices()
        if ton_usd is None:
            return None, None
        expected_ton = (grinch_amount * grinch_usd) / ton_usd
        min_ton = expected_ton * (1 - Config.SLIPPAGE_PCT / 100.0)
        return int(min_ton * TON), expected_ton

    # ─────────────────────────── swap: buy ─────────────────────────────────

    async def _buy_async(self, ton_amount: float) -> dict:
        """TON → GRINCH: отправляем TON в NativeVault с payload свопа."""
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
            native_vault = await Factory.get_native_vault(provider)

            amount_nano = int(ton_amount * TON)
            gas_nano    = int(0.25 * TON)

            swap_params = SwapParams(
                deadline=int(time.time()) + 300,
                recipient_address=wallet.address,
            )

            payload = VaultNative.create_swap_payload(
                amount=amount_nano,
                pool_address=pool.address,
                limit=min_out_nano,
                swap_params=swap_params,
            )

            await wallet.transfer(
                destination=native_vault.address,
                amount=amount_nano + gas_nano,
                body=payload,
            )

            return {
                "ok": True,
                "side": "buy",
                "ton_spent": ton_amount,
                "vault": str(native_vault.address),
                "min_grinch_out": round(min_out_nano / (10 ** 9), 6),
                "expected_grinch": round(expected_grinch, 6),
                "slippage_pct": Config.SLIPPAGE_PCT,
            }
        finally:
            await provider.close_all()

    def buy(self, ton_amount: float) -> dict:
        """Покупка GRINCH за TON через DeDust. Блокирует до завершения транзакции."""
        if not self._ready:
            return {"ok": False, "error": self._error}
        try:
            return _run(self._buy_async(ton_amount))
        except Exception as e:
            log.error(f"[DeDust] buy ошибка: {e}")
            return {"ok": False, "error": str(e)}

    # ─────────────────────────── swap: sell ────────────────────────────────

    async def _sell_async(self, grinch_amount: float) -> dict:
        """GRINCH → TON: переводим GRINCH-жеттон в JettonVault с payload свопа.

        Газ: 0.6 TON total (DeDust рекомендует ≥ 0.5 TON для jetton swap).
        Forward: 0.35 TON — достаточно для исполнения свопа внутри vault.
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
            jetton_vault  = await Factory.get_jetton_vault(grinch_root, provider)
            grinch_wallet = await grinch_root.get_wallet(wallet.address, provider)

            amount_nano = int(grinch_amount * (10 ** 9))
            # Увеличены газ и forward: 0.6 TON total, 0.35 TON forwarded
            gas_nano = int(0.6 * TON)
            fwd_nano = int(0.35 * TON)

            # ── Preflight: хватает ли TON на газ? ──────────────────────────
            # Своп-сообщение несёт gas_nano TON и форвардит fwd_nano в vault.
            # Если на кошельке меньше газа, транзакция уйдёт, но своп внутри
            # vault упадёт (нечем оплатить forward) → GRINCH вернётся, газ сгорит.
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

            swap_params = SwapParams(
                deadline=int(time.time()) + 600,   # 10 мин (было 5)
                recipient_address=wallet.address,
            )

            forward_payload = VaultJetton.create_swap_payload(
                pool_address=pool.address,
                limit=min_out_nano,
                swap_params=swap_params,
            )

            transfer_payload = grinch_wallet.create_transfer_payload(
                destination=jetton_vault.address,
                amount=amount_nano,
                response_address=wallet.address,
                forward_amount=fwd_nano,
                forward_payload=forward_payload,
            )

            await wallet.transfer(
                destination=grinch_wallet.address,
                amount=gas_nano,
                body=transfer_payload,
            )

            return {
                "ok": True,
                "side": "sell",
                "grinch_spent": grinch_amount,
                "vault": str(jetton_vault.address),
                "min_ton_out": round(min_out_nano / TON, 6),
                "expected_ton": round(expected_ton, 6),
                "slippage_pct": Config.SLIPPAGE_PCT,
            }
        finally:
            await provider.close_all()

    def sell(self, grinch_amount: float) -> dict:
        """Продажа GRINCH за TON через DeDust. Блокирует до завершения транзакции."""
        if not self._ready:
            return {"ok": False, "error": self._error}
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
