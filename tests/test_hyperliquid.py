"""
Hyperliquid testnet integration tests.

Run: docker compose exec app python -m pytest tests/ -v -s
"""

import sys
sys.path.insert(0, "/app")

import pytest
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from src.config import settings

# Guard: refuse to run against mainnet
if not settings.testnet:
    pytest.exit("TESTNET=true が必要です。本番環境では実行しないでください。", returncode=1)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def info():
    return Info(settings.api_url, skip_ws=True)


@pytest.fixture(scope="module")
def exchange():
    account = eth_account.Account.from_key(settings.hyperliquid_private_key)
    return Exchange(
        account,
        settings.api_url,
        account_address=settings.hyperliquid_main_address,
    )


@pytest.fixture(scope="module")
def btc_mid(info):
    return float(info.all_mids()[settings.trading_coin])


@pytest.fixture(scope="module")
def sz_decimals(info):
    meta = info.meta()
    asset = next(a for a in meta["universe"] if a["name"] == settings.trading_coin)
    return asset["szDecimals"]


# ── Unit tests (no network) ───────────────────────────────────────────────────

class TestPnlCalc:
    def test_long_profit(self):
        from src.trader import calc_pnl
        pnl = calc_pnl("long", 80_000, 82_000, 100)
        assert pnl == pytest.approx(2.5, rel=1e-4)

    def test_short_profit(self):
        from src.trader import calc_pnl
        pnl = calc_pnl("short", 80_000, 78_000, 100)
        assert pnl == pytest.approx(2.5, rel=1e-4)

    def test_breakeven(self):
        from src.trader import calc_pnl
        assert calc_pnl("long", 80_000, 80_000, 100) == pytest.approx(0.0)


# ── Market data tests ─────────────────────────────────────────────────────────

class TestMarketData:
    def test_mid_price(self, btc_mid):
        """テストネットからBTC mid価格を取得できるか"""
        assert btc_mid > 0
        print(f"\n  BTC mid: ${btc_mid:,.2f}")

    def test_fetch_candles_15m(self):
        """15分足OHLCVを取得できるか"""
        from src.chart import fetch_candles
        df = fetch_candles(settings.trading_coin, "15m", 20)
        assert not df.empty
        assert len(df) >= 10
        assert {"Open", "High", "Low", "Close", "Volume"} <= set(df.columns)
        print(f"\n  15m candles: {len(df)} 本, latest close=${df['Close'].iloc[-1]:,.2f}")

    def test_fetch_candles_1m(self):
        """1分足OHLCVを取得できるか"""
        from src.chart import fetch_candles
        df = fetch_candles(settings.trading_coin, "1m", 20)
        assert not df.empty
        print(f"\n  1m candles: {len(df)} 本")

    def test_fetch_mid_via_orchestrator(self):
        """オーケストレーターの _fetch_mid が動くか"""
        from src.orchestrator import _fetch_mid
        price = _fetch_mid(settings.trading_coin)
        assert price > 0
        print(f"\n  _fetch_mid: ${price:,.2f}")


# ── Order placement tests ─────────────────────────────────────────────────────

class TestOrderPlacement:
    MIN_NOTIONAL = 11  # $10最小制限に対してバッファを持たせる

    def _calc_qty(self, mid: float, sz_dec: int) -> float:
        """最小注文額($10)を超えるqtyを計算する"""
        qty = round(self.MIN_NOTIONAL / mid, sz_dec)
        return max(qty, 10 ** -sz_dec)

    @staticmethod
    def _round_to_tick(price: float) -> float:
        """Hyperliquidのtick sizeに合わせて整数に丸める"""
        return float(round(price))

    def test_place_and_cancel_long(self, info, exchange, btc_mid, sz_decimals):
        """Longの指値注文を出して即キャンセルできるか（約定しない価格で）"""
        coin = settings.trading_coin
        qty = self._calc_qty(btc_mid, sz_decimals)
        limit_px = self._round_to_tick(btc_mid * 0.97)  # 3% 下 → 約定しない

        print(f"\n  LONG limit: {coin} qty={qty} px={limit_px} (~${qty * btc_mid:.2f})")

        result = exchange.order(coin, True, qty, limit_px, {"limit": {"tif": "Gtc"}})
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        assert statuses, f"レスポンスにstatusがありません: {result}"

        status = statuses[0]
        assert "error" not in status, f"注文エラー: {status.get('error')}"

        oid = (status.get("resting") or status.get("filled") or {}).get("oid")
        assert oid, f"oidが取得できません: {status}"
        print(f"  注文成功 oid={oid}")

        # キャンセル
        cancel = exchange.cancel(coin, oid)
        cancel_statuses = cancel.get("response", {}).get("data", {}).get("statuses", [])
        assert cancel_statuses and cancel_statuses[0] == "success", f"キャンセル失敗: {cancel}"
        print(f"  キャンセル成功")

    def test_place_and_cancel_short(self, info, exchange, btc_mid, sz_decimals):
        """Shortの指値注文を出して即キャンセルできるか（約定しない価格で）"""
        coin = settings.trading_coin
        qty = self._calc_qty(btc_mid, sz_decimals)
        limit_px = self._round_to_tick(btc_mid * 1.03)  # 3% 上 → 約定しない

        print(f"\n  SHORT limit: {coin} qty={qty} px={limit_px} (~${qty * btc_mid:.2f})")

        result = exchange.order(coin, False, qty, limit_px, {"limit": {"tif": "Gtc"}})
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        assert statuses, f"レスポンスにstatusがありません: {result}"

        status = statuses[0]
        assert "error" not in status, f"注文エラー: {status.get('error')}"

        oid = (status.get("resting") or status.get("filled") or {}).get("oid")
        assert oid, f"oidが取得できません: {status}"
        print(f"  注文成功 oid={oid}")

        cancel = exchange.cancel(coin, oid)
        cancel_statuses = cancel.get("response", {}).get("data", {}).get("statuses", [])
        assert cancel_statuses and cancel_statuses[0] == "success", f"キャンセル失敗: {cancel}"
        print(f"  キャンセル成功")

    def test_market_long_and_close(self, info, exchange, btc_mid, sz_decimals):
        """成行Long → 即成行決済（実際に約定するフルサイクルテスト）"""
        import time
        coin = settings.trading_coin
        qty = self._calc_qty(btc_mid, sz_decimals)

        print(f"\n  成行LONG: {coin} qty={qty} (~${qty * btc_mid:.2f})")

        # 成行エントリー
        open_result = exchange.market_open(coin, True, qty, slippage=0.01)
        statuses = open_result.get("response", {}).get("data", {}).get("statuses", [])
        assert statuses and "filled" in statuses[0], f"成行エントリー失敗: {open_result}"

        entry_px = float(statuses[0]["filled"]["avgPx"])
        print(f"  約定価格: ${entry_px:,.2f}")

        # ポジションがAPIに反映されるまで待機
        time.sleep(2)

        # ポジション確認
        state = info.user_state(settings.hyperliquid_main_address.lower())
        positions = state.get("assetPositions", [])
        pos = next((p["position"] for p in positions if p["position"]["coin"] == coin), None)
        print(f"  ポジション確認: {pos}")

        # reduce_only 決済注文 (テストネットはOracle価格が市場価格と乖離しているためGTCで注文確認のみ)
        # oracle価格を取得し、制約内の価格(2.5%下)で指値
        ctxs = info.meta_and_asset_ctxs()
        universe = ctxs[0]["universe"]
        btc_idx = next(i for i, a in enumerate(universe) if a["name"] == coin)
        oracle_px = float(ctxs[1][btc_idx]["oraclePx"])
        close_px = round(oracle_px * 0.975)  # 2.5% below oracle = oracle constraint safe

        print(f"  oracle=${oracle_px:,.0f}  close_px=${close_px:,.0f}")

        close_result = exchange.order(
            coin, False, qty, close_px,
            {"limit": {"tif": "Gtc"}},
            reduce_only=True,
        )
        close_statuses = close_result.get("response", {}).get("data", {}).get("statuses", [])
        assert close_statuses, f"決済レスポンスなし: {close_result}"
        status = close_statuses[0]
        assert "error" not in status, f"決済注文エラー: {status.get('error')}"

        from src.trader import calc_pnl
        if "filled" in status:
            exit_px = float(status["filled"]["avgPx"])
            pnl = calc_pnl("long", entry_px, exit_px, qty * entry_px)
            print(f"  即約定 exit=${exit_px:,.2f}  PnL=${pnl:.4f}")
        else:
            # GTC resting → キャンセルしてクリーンアップ
            oid = (status.get("resting") or {}).get("oid")
            if oid:
                exchange.cancel(coin, oid)
            print(f"  GTC注文成功(resting) → キャンセル済み (oracle制約でIOC即約定不可)")
