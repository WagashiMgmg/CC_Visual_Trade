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

from src.config import settings, make_info, make_exchange

# Guard: refuse to run against mainnet
if not settings.testnet:
    pytest.exit("TESTNET=true が必要です。本番環境では実行しないでください。", returncode=1)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def info():
    return make_info()


@pytest.fixture(scope="module")
def exchange():
    account = eth_account.Account.from_key(settings.active_private_key)
    return make_exchange(account, account_address=settings.active_main_address)


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


# ── Dynamic position sizing tests (unit, no network) ─────────────────────

class TestDynamicPositionSizing:
    """Test ATR-based dynamic position sizing logic."""

    def test_dry_run_returns_fallback(self):
        """DRY_RUN=true ではフォールバック値を返す"""
        from src.trader import get_dynamic_position_size
        original = settings.dry_run
        try:
            settings.dry_run = True
            size, equity = get_dynamic_position_size()
            assert size == settings.position_size_usd
            assert equity == settings.position_size_usd / 2
        finally:
            settings.dry_run = original

    def test_formula_basic(self):
        """ATRベース計算: equity=50, ATR_pct=0.00412, multiplier=2 → ~121"""
        from unittest.mock import patch
        original = settings.dry_run
        try:
            settings.dry_run = False
            with patch("src.trader.get_account_equity", return_value=50.0), \
                 patch("src.trader.get_current_atr_pct", return_value=0.00412):
                from src.trader import get_dynamic_position_size
                size, equity = get_dynamic_position_size()
                # max_loss = 50 * 0.02 = 1.0
                # adverse = 0.00412 * 2.0 = 0.00824
                # raw_size = 1.0 / 0.00824 = 121.36
                assert equity == 50.0
                assert 120 <= size <= 123
        finally:
            settings.dry_run = original

    def test_min_clamp(self):
        """サイズ下限(min_position_usd)でクランプされる"""
        from unittest.mock import patch
        original = settings.dry_run
        try:
            settings.dry_run = False
            # equity very small → size below min
            with patch("src.trader.get_account_equity", return_value=1.0), \
                 patch("src.trader.get_current_atr_pct", return_value=0.01):
                from src.trader import get_dynamic_position_size
                size, equity = get_dynamic_position_size()
                assert size == settings.min_position_usd
        finally:
            settings.dry_run = original

    def test_max_clamp(self):
        """サイズ上限(max_position_usd)でクランプされる"""
        from unittest.mock import patch
        original = settings.dry_run
        try:
            settings.dry_run = False
            # equity huge → size above max
            with patch("src.trader.get_account_equity", return_value=100_000.0), \
                 patch("src.trader.get_current_atr_pct", return_value=0.001):
                from src.trader import get_dynamic_position_size
                size, equity = get_dynamic_position_size()
                assert size == settings.max_position_usd
        finally:
            settings.dry_run = original

    def test_zero_atr_fallback(self):
        """ATR=0の場合はフォールバック値を返す"""
        from unittest.mock import patch
        original = settings.dry_run
        try:
            settings.dry_run = False
            with patch("src.trader.get_account_equity", return_value=50.0), \
                 patch("src.trader.get_current_atr_pct", return_value=0.0):
                from src.trader import get_dynamic_position_size
                size, equity = get_dynamic_position_size()
                # adverse = 0 → fallback to position_size_usd, clamped by min/max
                assert size == min(settings.max_position_usd,
                                   max(settings.min_position_usd, settings.position_size_usd))
        finally:
            settings.dry_run = original

    def test_api_failure_fallback(self):
        """API失敗時はフォールバック値を返す"""
        from unittest.mock import patch
        original = settings.dry_run
        try:
            settings.dry_run = False
            with patch("src.trader.get_account_equity", side_effect=Exception("API down")):
                from src.trader import get_dynamic_position_size
                size, equity = get_dynamic_position_size()
                assert size == settings.position_size_usd
                assert equity == settings.position_size_usd / 2
        finally:
            settings.dry_run = original

    def test_high_atr_reduces_size(self):
        """ATRが高い → サイズ縮小"""
        from unittest.mock import patch
        original = settings.dry_run
        try:
            settings.dry_run = False
            with patch("src.trader.get_account_equity", return_value=50.0):
                from src.trader import get_dynamic_position_size
                with patch("src.trader.get_current_atr_pct", return_value=0.002):
                    size_low_vol, _ = get_dynamic_position_size()
                with patch("src.trader.get_current_atr_pct", return_value=0.008):
                    size_high_vol, _ = get_dynamic_position_size()
                assert size_low_vol > size_high_vol
        finally:
            settings.dry_run = original


class TestFeeRatePct:
    """Test fee rate percentage calculation."""

    def test_fee_rate_pct_formula(self):
        """get_fee_rate_pct() = user_fee_rate * 2 * 100"""
        from unittest.mock import patch
        with patch("src.trader.get_user_fee_rate", return_value=0.00045):
            from src.trader import get_fee_rate_pct
            rate = get_fee_rate_pct()
            assert rate == pytest.approx(0.09, rel=1e-4)

    def test_fee_rate_pct_with_different_rate(self):
        """異なるフィーレートでも正しく計算"""
        from unittest.mock import patch
        with patch("src.trader.get_user_fee_rate", return_value=0.00035):
            from src.trader import get_fee_rate_pct
            rate = get_fee_rate_pct()
            assert rate == pytest.approx(0.07, rel=1e-4)


class TestCurrentAtrPct:
    """Test ATR percentage calculation."""

    def test_atr_pct_from_candles(self):
        """fetch_candles + _atr で ATR% が正しく計算されるか"""
        import pandas as pd
        from unittest.mock import patch

        # Create mock candle data with known ATR
        dates = pd.date_range("2024-01-01", periods=30, freq="15min", tz="UTC")
        df = pd.DataFrame({
            "Open":   [100.0] * 30,
            "High":   [102.0] * 30,
            "Low":    [98.0] * 30,
            "Close":  [101.0] * 30,
            "Volume": [1000.0] * 30,
        }, index=dates)

        with patch("src.chart.fetch_candles", return_value=df):
            from src.trader import get_current_atr_pct
            atr_pct = get_current_atr_pct("BTC")
            # ATR should be ~4.0 (high-low=4, all true ranges equal)
            # atr_pct = 4.0 / 101.0 ≈ 0.0396
            assert 0.03 < atr_pct < 0.05


class TestPnlPercentConversion:
    """Test that PnL is correctly expressed as price change %."""

    def test_reflection_pnl_pct(self):
        """_build_reflection_prompt が PnL% を正しく計算するか"""
        from src.reflection import _build_reflection_prompt
        trade_info = {
            "archive_dir": "/app/charts/trade_1",
            "trade_id": 1,
            "coin": "BTC",
            "side": "long",
            "entry_price": 80000.0,
            "exit_price": 80800.0,
            "pnl_usd": 1.0,      # $1 profit on $100 size = 1%
            "size_usd": 100.0,
            "entry_time": "2024-01-01T00:00:00",
            "exit_time": "2024-01-01T01:00:00",
        }
        prompt = _build_reflection_prompt(trade_info, None, fee_rate_pct=0.09)
        # PnL should be +1.00%
        assert "+1.00%" in prompt
        assert "WIN" in prompt
        # Fee note should show %
        assert "~0.090%" in prompt

    def test_reflection_loss_pct(self):
        """LOSS時のPnL%表示"""
        from src.reflection import _build_reflection_prompt
        trade_info = {
            "archive_dir": "/app/charts/trade_2",
            "trade_id": 2,
            "coin": "BTC",
            "side": "short",
            "entry_price": 80000.0,
            "exit_price": 80400.0,
            "pnl_usd": -0.5,
            "size_usd": 100.0,
            "entry_time": "2024-01-01T00:00:00",
            "exit_time": "2024-01-01T01:00:00",
        }
        prompt = _build_reflection_prompt(trade_info, None, fee_rate_pct=0.09)
        assert "-0.50%" in prompt
        assert "LOSS" in prompt

    def test_fee_note_pct_format(self):
        """fee_note が % フォーマットで出力されるか"""
        from src.reflection import fee_note
        note = fee_note(0.09)
        assert "~0.090%" in note
        assert "$" not in note


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

    @staticmethod
    def _parse_statuses(result: dict) -> list:
        """Parse statuses from order/cancel result, handling string response (wallet not found)."""
        resp = result.get("response", {})
        if isinstance(resp, str):
            if "does not exist" in resp:
                pytest.skip(f"テストネットウォレット未登録: {resp}")
            pytest.fail(f"APIエラー: {resp}")
        return resp.get("data", {}).get("statuses", [])

    def test_place_and_cancel_long(self, info, exchange, btc_mid, sz_decimals):
        """Longの指値注文を出して即キャンセルできるか（約定しない価格で）"""
        coin = settings.trading_coin
        qty = self._calc_qty(btc_mid, sz_decimals)
        limit_px = self._round_to_tick(btc_mid * 0.97)  # 3% 下 → 約定しない

        print(f"\n  LONG limit: {coin} qty={qty} px={limit_px} (~${qty * btc_mid:.2f})")

        result = exchange.order(coin, True, qty, limit_px, {"limit": {"tif": "Gtc"}})
        statuses = self._parse_statuses(result)
        assert statuses, f"レスポンスにstatusがありません: {result}"

        status = statuses[0]
        assert "error" not in status, f"注文エラー: {status.get('error')}"

        oid = (status.get("resting") or status.get("filled") or {}).get("oid")
        assert oid, f"oidが取得できません: {status}"
        print(f"  注文成功 oid={oid}")

        # キャンセル
        cancel = exchange.cancel(coin, oid)
        cancel_statuses = self._parse_statuses(cancel)
        assert cancel_statuses and cancel_statuses[0] == "success", f"キャンセル失敗: {cancel}"
        print(f"  キャンセル成功")

    def test_place_and_cancel_short(self, info, exchange, btc_mid, sz_decimals):
        """Shortの指値注文を出して即キャンセルできるか（約定しない価格で）"""
        coin = settings.trading_coin
        qty = self._calc_qty(btc_mid, sz_decimals)
        limit_px = self._round_to_tick(btc_mid * 1.03)  # 3% 上 → 約定しない

        print(f"\n  SHORT limit: {coin} qty={qty} px={limit_px} (~${qty * btc_mid:.2f})")

        result = exchange.order(coin, False, qty, limit_px, {"limit": {"tif": "Gtc"}})
        statuses = self._parse_statuses(result)
        assert statuses, f"レスポンスにstatusがありません: {result}"

        status = statuses[0]
        assert "error" not in status, f"注文エラー: {status.get('error')}"

        oid = (status.get("resting") or status.get("filled") or {}).get("oid")
        assert oid, f"oidが取得できません: {status}"
        print(f"  注文成功 oid={oid}")

        cancel = exchange.cancel(coin, oid)
        cancel_statuses = self._parse_statuses(cancel)
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
        statuses = self._parse_statuses(open_result)
        assert statuses and "filled" in statuses[0], f"成行エントリー失敗: {open_result}"

        entry_px = float(statuses[0]["filled"]["avgPx"])
        print(f"  約定価格: ${entry_px:,.2f}")

        # ポジションがAPIに反映されるまで待機
        time.sleep(2)

        # ポジション確認
        state = info.user_state(settings.active_main_address.lower())
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
        close_statuses = self._parse_statuses(close_result)
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


# ── Dynamic sizing integration tests (testnet API) ───────────────────────

class TestDynamicSizingIntegration:
    """Testnet API integration tests for dynamic position sizing."""

    def test_get_account_equity(self):
        """テストネットからaccountValueを取得できるか"""
        from src.trader import get_account_equity
        equity = get_account_equity()
        assert isinstance(equity, float)
        assert equity >= 0
        print(f"\n  Account equity: ${equity:.2f}")

    def test_get_current_atr_pct(self):
        """15分足ATR(14)を価格比%で取得できるか"""
        from src.trader import get_current_atr_pct
        atr_pct = get_current_atr_pct(settings.trading_coin)
        assert isinstance(atr_pct, float)
        assert atr_pct > 0
        assert atr_pct < 1  # 100% ATR would be extreme
        print(f"\n  ATR_pct: {atr_pct:.6f} ({atr_pct * 100:.3f}%)")

    def test_get_fee_rate_pct(self):
        """テストネットからフィーレート(%)を取得できるか"""
        from src.trader import get_fee_rate_pct
        rate = get_fee_rate_pct()
        assert isinstance(rate, float)
        assert rate > 0
        assert rate < 1  # <1% round-trip is expected
        print(f"\n  Fee rate (round-trip): {rate:.4f}%")

    def test_get_dynamic_position_size_live(self):
        """テストネットでATRベースの動的サイジングが動くか"""
        from src.trader import get_dynamic_position_size
        original = settings.dry_run
        try:
            settings.dry_run = False
            size, equity = get_dynamic_position_size()
            assert isinstance(size, float)
            assert settings.min_position_usd <= size <= settings.max_position_usd
            assert equity >= 0
            print(f"\n  Dynamic size: ${size:.2f} (equity=${equity:.2f})")
        finally:
            settings.dry_run = original
