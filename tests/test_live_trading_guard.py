"""Live trading stays off unless both process flags are set. No network."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution import live_guard
from execution.binance_futures import BinanceFutures, SymbolRules
from execution.delta_india import DeltaIndia
from execution.live_book import LiveBook, LivePosition, LiveState
from execution.live_guard import (
    CONFIRM_VALUE,
    ORDER_METHODS,
    LiveTradingDisabled,
    trading_mode_fields,
)
from tests.test_delta_ladder import FakeDelta, _book, _open_short

ROOT = Path(__file__).resolve().parents[1]
GUARD_FILE = "execution/live_guard.py"
CLIENT_FILES = {
    "execution/binance_futures.py": ("BinanceFutures", (
        "prepare", "market", "protect", "cancel_all",
    )),
    "execution/delta_india.py": ("DeltaIndia", (
        "place_market", "place_stop_market", "place_take_profit_market",
        "cancel_all", "edit_stop", "cancel_order", "set_leverage",
    )),
}
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
}
ORDER_PATH_MARKERS = (
    "/fapi/v1/order",
    "/fapi/v1/algoOrder",
    "/fapi/v1/leverage",
    "/fapi/v1/marginType",
    "/fapi/v1/allOpenOrders",
    "/fapi/v1/algoOpenOrders",
    "/v2/orders",
    "/orders/leverage",
)
SOL = SymbolRules("SOLUSDT", step=0.01, min_qty=0.01, tick=0.01, min_notional=5.0)


def _py_files():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _starts_with_guard(fn: ast.AST) -> bool:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body or not isinstance(body[0], ast.Expr):
        return False
    call = body[0].value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "assert_live_trading_allowed"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "assert_live_trading_allowed"
    )


def _walk_own_calls(fn: ast.AST):
    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                yield child
            yield from visit(child)
    yield from visit(fn)


def _functions(tree: ast.AST):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield item


def _string_bits(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return "".join(parts)
    return ""


def _mutating_order_request(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "_request":
        return False
    if not call.args or not isinstance(call.args[0], ast.Constant):
        return False
    if call.args[0].value not in {"POST", "PUT", "DELETE"}:
        return False
    path = _string_bits(call.args[1]) if len(call.args) > 1 else ""
    return any(bit in path for bit in ORDER_PATH_MARKERS)


def _order_attr_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in ORDER_METHODS:
            yield node.lineno, node.func.attr


def test_order_calls_live_only_in_the_guard_module():
    offenders = []
    for path in _py_files():
        rel = _rel(path)
        if rel == GUARD_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for lineno, name in _order_attr_calls(tree):
            offenders.append(f"{rel}:{lineno} .{name}()")
    assert offenders == []


def test_mutating_order_http_is_only_in_guarded_client_methods():
    offenders = []
    for path in _py_files():
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for fn in _functions(tree):
            guarded = _starts_with_guard(fn)
            for call in _walk_own_calls(fn):
                if not _mutating_order_request(call):
                    continue
                if rel not in CLIENT_FILES or not guarded:
                    offenders.append(f"{rel}:{call.lineno} {fn.name}")
    assert offenders == []


def test_every_client_mutation_starts_with_the_guard():
    missing = []
    for rel, (class_name, methods) in CLIENT_FILES.items():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        found = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef):
                        found[item.name] = item
        for name in methods:
            fn = found.get(name)
            if fn is None or not _starts_with_guard(fn):
                missing.append(f"{rel} {class_name}.{name}")
    assert missing == []


def test_guard_wrappers_assert_before_the_client_call():
    tree = ast.parse((ROOT / GUARD_FILE).read_text(encoding="utf-8"), filename=GUARD_FILE)
    wrappers = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    ]
    assert wrappers
    for fn in wrappers:
        assert _starts_with_guard(fn), fn.name
        attrs = [name for _, name in _order_attr_calls(fn)]
        assert attrs, fn.name


def test_startup_logs_trading_mode_from_the_guard():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"), filename="main.py")
    mains = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    ]
    assert len(mains) == 1
    saw_helper = False
    saw_event = False
    for node in ast.walk(mains[0]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "trading_mode_fields":
                saw_helper = True
        if isinstance(node, ast.Constant) and node.value == "trading_mode":
            saw_event = True
    assert saw_helper and saw_event


def test_default_mode_is_paper_and_live_off():
    assert trading_mode_fields() == {"mode": "paper/research", "live": "OFF"}
    with pytest.raises(LiveTradingDisabled) as caught:
        live_guard.assert_live_trading_allowed()
    assert CONFIRM_VALUE not in str(caught.value)


@pytest.mark.parametrize("enabled,confirm", [
    (None, None),
    ("true", None),
    (None, CONFIRM_VALUE),
    ("false", CONFIRM_VALUE),
    ("true", "yes"),
    ("1", CONFIRM_VALUE),
])
def test_partial_flags_keep_live_off(monkeypatch, enabled, confirm):
    if enabled is None:
        monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LIVE_TRADING_ENABLED", enabled)
    if confirm is None:
        monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    else:
        monkeypatch.setenv("LIVE_TRADING_CONFIRM", confirm)
    assert trading_mode_fields()["live"] == "OFF"
    with pytest.raises(LiveTradingDisabled):
        live_guard.assert_live_trading_allowed()


def test_both_flags_are_required_together(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "True")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", CONFIRM_VALUE)
    assert live_guard.live_trading_allowed()
    assert trading_mode_fields() == {"mode": "live", "live": "ON"}


class _Recorder:
    def __init__(self):
        self.calls = []

    async def market(self, *args, **kwargs):
        self.calls.append(("market", args, kwargs))
        return {"mocked": True}


@pytest.mark.asyncio
async def test_wrapper_reaches_a_mock_only_when_both_flags_match(monkeypatch):
    rec = _Recorder()
    with pytest.raises(LiveTradingDisabled):
        await live_guard.binance_market(rec, "SOLUSDT", "BUY", 0.01)
    assert rec.calls == []
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", CONFIRM_VALUE)
    out = await live_guard.binance_market(rec, "SOLUSDT", "BUY", 0.01)
    assert out == {"mocked": True}
    assert rec.calls[0][0] == "market"


def _http_mock(body):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = body
    http = MagicMock()
    http.request = AsyncMock(return_value=response)
    return http


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("prepare", ("SOLUSDT", 2)),
    ("market", ("SOLUSDT", "BUY", 0.1)),
    ("protect", ("SOLUSDT", "SELL", "STOP_MARKET", 1.0)),
    ("cancel_all", ("SOLUSDT",)),
])
async def test_binance_writes_raise_without_flags(name, args):
    http = _http_mock({})
    client = BinanceFutures(
        "k", "s", base_url="http://invalid.invalid", client=http,
    )
    with pytest.raises(LiveTradingDisabled):
        await getattr(client, name)(*args)
    http.request.assert_not_called()


@pytest.mark.asyncio
async def test_binance_market_with_flags_uses_the_mock_host(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", CONFIRM_VALUE)
    http = _http_mock({
        "orderId": 1, "avgPrice": "1", "executedQty": "0.01", "status": "FILLED",
    })
    client = BinanceFutures(
        "k", "s", base_url="http://invalid.invalid", client=http,
    )
    await getattr(client, "market")("SOLUSDT", "BUY", 0.01)
    url = http.request.await_args.args[1]
    assert url.startswith("http://invalid.invalid/")
    assert "binance.com" not in url


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,kwargs", [
    ("place_market", (1, "buy", 1), {}),
    ("place_stop_market", (1, "sell", 1, 1.0), {}),
    ("place_take_profit_market", (1, "sell", 1, 2.0), {}),
    ("cancel_all", (1,), {}),
    ("edit_stop", (9, 1, 1, 1.0), {}),
    ("cancel_order", (9, 1), {}),
    ("set_leverage", (1, 2), {}),
])
async def test_delta_writes_raise_without_flags(name, args, kwargs):
    http = _http_mock({"success": True, "result": {}})
    client = DeltaIndia(
        "k", "s", base_url="http://invalid.invalid", client=http,
    )
    with pytest.raises(LiveTradingDisabled):
        await getattr(client, name)(*args, **kwargs)
    http.request.assert_not_called()


@pytest.mark.asyncio
async def test_delta_place_market_with_flags_uses_the_mock_host(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", CONFIRM_VALUE)
    http = _http_mock({"success": True, "result": {"id": 1}})
    client = DeltaIndia(
        "k", "s", base_url="http://invalid.invalid", client=http,
    )
    await getattr(client, "place_market")(1, "buy", 1)
    url = http.request.await_args.args[1]
    assert url.startswith("http://invalid.invalid/")
    assert "delta.exchange" not in url


class _QuietExchange:
    def __init__(self, amt=0.0):
        self.amt = amt
        self.mutations = []

    async def rules(self, _symbol):
        return SOL

    async def usdt_balance(self):
        return {"balance": 20.0, "available": 20.0}

    async def position(self, _symbol):
        return {"amt": self.amt, "entry": 150.0, "mark": 150.0, "unrealized": 0.0}

    async def prepare(self, *_args, **_kwargs):
        self.mutations.append("prepare")

    async def market(self, *_args, **_kwargs):
        self.mutations.append("market")

    async def protect(self, *_args, **_kwargs):
        self.mutations.append("protect")

    async def cancel_all(self, *_args, **_kwargs):
        self.mutations.append("cancel_all")

    async def open_protection(self, _symbol):
        return 0


def _live_cfg():
    return SimpleNamespace(
        live_max_open=0, live_max_balance_usdt=20, live_daily_loss_pct=0.06,
        live_risk_pct=0.015, live_max_risk_pct=0.025, live_max_leverage=10,
        swing_hold_minutes=10080,
    )


@pytest.mark.asyncio
async def test_live_book_open_close_and_monitor_raise_without_flags(tmp_path):
    ex = _QuietExchange()
    book = LiveBook(
        ex, LiveState(str(tmp_path / "s.json"), str(tmp_path / "t.jsonl")), _live_cfg(),
    )
    with pytest.raises(LiveTradingDisabled):
        await book.open_from_paper("solusdt", "long", 150.0, 6.0, "swing_donchian", 1_000_000)
    assert ex.mutations == []

    book.state.positions["SOLUSDT"] = LivePosition(
        "SOLUSDT", "long", 0.1, 150.0, 144.0, 168.0, 1_000_000, 2_000_000,
        "swing_donchian", 150.0, 1, 0.3,
    )
    ex.amt = 0.1
    with pytest.raises(LiveTradingDisabled):
        await book.close("SOLUSDT", 1_000_001, "test")
    assert ex.mutations == []

    ex.amt = 0.0
    with pytest.raises(LiveTradingDisabled):
        await book.monitor(1_000_002)
    assert ex.mutations == []


class _FakeWithLeverage(FakeDelta):
    async def set_leverage(self, product_id, leverage):
        self.calls.append(("leverage", product_id, leverage))
        return {}


@pytest.mark.asyncio
async def test_delta_book_live_open_raises_before_any_order(tmp_path):
    fake = _FakeWithLeverage()
    book = _book(tmp_path, fake)
    with pytest.raises(LiveTradingDisabled):
        await _open_short(book)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_delta_shadow_still_places_nothing(tmp_path):
    fake = _FakeWithLeverage()
    book = _book(tmp_path, fake, mode="shadow", live=False)
    plan = await _open_short(book)
    assert plan.ok
    assert fake.calls == []
