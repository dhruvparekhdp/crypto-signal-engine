"""Print fresh Binance USD-M order rules for the watchlist, in the format of analysis/binance_filters._RAW.

    python -m scripts.binance_filters

Public endpoint, no key. Paste the output over _RAW when Binance changes a rule.
"""
import httpx

from analysis.binance_filters import RULES


def main() -> None:
    info = httpx.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20).json()
    for s in info["symbols"]:
        if s["symbol"] in RULES:
            f = {x["filterType"]: x for x in s["filters"]}
            print(f'    "{s["symbol"]}": ({float(f["MARKET_LOT_SIZE"]["stepSize"]):g}, '
                  f'{float(f["LOT_SIZE"]["minQty"]):g}, {float(f["MIN_NOTIONAL"]["notional"]):g}, '
                  f'{float(f["PRICE_FILTER"]["tickSize"]):g}),')


if __name__ == "__main__":
    main()
