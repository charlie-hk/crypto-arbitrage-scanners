#!/usr/bin/env python3
"""
arb_scanner.py
Spot arbitrage scanner. Finds, logs and alerts only - it never places orders.

tri   : triangular arbitrage inside one exchange (uses your real per-pair fees on Binance)
cross : price gaps for the same pair across exchanges, with deposit/withdraw checks

Install:
    pip install ccxt requests

Examples:
    python arb_scanner.py tri --max-fee-legs 0 --notional 1000 --loop 10
    python arb_scanner.py tri --max-fee-legs 1 --bnb --notional 500 --loop 10
    python arb_scanner.py cross --exchanges binance kucoin bybit okx

Read-only API keys are read from environment variables (or prompted for Binance):
    BINANCE_API_KEY , BINANCE_SECRET
Telegram alerts (optional):
    TELEGRAM_TOKEN , TELEGRAM_CHAT_ID
"""

import argparse
import csv
import os
import time
from datetime import datetime, timezone

import ccxt

STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USD", "USD1", "RLUSD", "U", "USDP", "XUSD"}

# (exchange_id, symbol) -> your real taker fee
PERSONAL_FEES = {}
ALERTED = {}   # (symbol, buy_on, sell_on) -> time of last telegram alert
BNB_DISCOUNT = False


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def now_ms():
    return int(time.time() * 1000)


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_exchange(name, key=None, secret=None, password=None):
    cfg = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if key and secret:
        cfg["apiKey"], cfg["secret"] = key, secret
    if password:
        cfg["password"] = password
    ex = getattr(ccxt, name)(cfg)
    ex.load_markets()
    return ex


def ask_keys():
    key = os.getenv("BINANCE_API_KEY") or input("Binance API Key (read-only): ").strip()
    secret = os.getenv("BINANCE_SECRET") or input("Binance Secret Key: ").strip()
    return key, secret


def load_personal_fees(ex):
    """Load your real trading fees for every pair from the account."""
    fees = ex.fetch_trading_fees()
    for s, f in fees.items():
        if f.get("taker") is not None:
            PERSONAL_FEES[(ex.id, s)] = float(f["taker"])
    active = spot_symbols(ex)
    zero = sorted(s for (eid, s), f in PERSONAL_FEES.items()
                  if eid == ex.id and f == 0 and s in active)
    print(f"personal fees loaded: {len(fees)} pairs | active zero-taker pairs: {len(zero)}")
    print("  " + ", ".join(zero))


def spot_symbols(ex):
    return {
        s for s, m in ex.markets.items()
        if m.get("spot") and m.get("active") is not False and ":" not in s
    }


def fetch_spot_tickers(ex):
    """All spot tickers in one request; keeps only pairs with a valid bid and ask."""
    syms = spot_symbols(ex)
    try:
        tickers = ex.fetch_tickers()
    except Exception:
        tickers = ex.fetch_tickers(list(syms))
    out = {}
    for s, t in tickers.items():
        if s in syms and t.get("bid") and t.get("ask") and t["bid"] > 0 and t["ask"] > 0:
            out[s] = t
    return out


def usd_price(cur, tickers):
    if cur in STABLES:
        return 1.0
    for q in ("USDT", "USDC", "FDUSD"):
        t = tickers.get(f"{cur}/{q}")
        if t:
            return (t["bid"] + t["ask"]) / 2
    return None


def quote_volume_usd(t, quote, tickers):
    qv = t.get("quoteVolume")
    if qv is None and t.get("baseVolume") and t.get("last"):
        qv = t["baseVolume"] * t["last"]
    p = usd_price(quote, tickers)
    return qv * p if (qv and p) else 0.0


def top_book_usd(t, side, quote, tickers):
    """USD size at the best price level, or None if the exchange does not report it."""
    vol = t.get("bidVolume" if side == "bid" else "askVolume")
    p = usd_price(quote, tickers)
    if not vol or not p:
        return None
    return vol * t[side] * p


def is_fresh(t, max_age_s):
    ts = t.get("timestamp")
    return ts is None or (now_ms() - ts) <= max_age_s * 1000


def taker_fee(ex, symbol, override):
    if override is not None:
        return override
    f = PERSONAL_FEES.get((ex.id, symbol))
    if f is None:
        f = ex.markets[symbol].get("taker") or 0.001
    if BNB_DISCOUNT and f > 0 and ex.id == "binance":
        f *= 0.75
    return f


def notify(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=10)
    except Exception as e:
        print(f"[telegram error] {e}")


def log_rows(path, rows, fields):
    if not rows:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def fmt(v, nd=4):
    return "-" if v is None else f"{v:.{nd}f}"


# ------------------------------------------------------------------
# order book execution simulation
# ------------------------------------------------------------------

def walk_buy(asks, spend_quote):
    """Spend a quote amount against the asks. Returns base received, or None if depth is insufficient."""
    got, left = 0.0, spend_quote
    for lvl in asks:
        price, amount = lvl[0], lvl[1]
        cost = price * amount
        if cost >= left:
            return got + left / price
        got += amount
        left -= cost
    return None


def walk_sell(bids, sell_base):
    """Sell a base amount into the bids. Returns quote received, or None."""
    got, left = 0.0, sell_base
    for lvl in bids:
        price, amount = lvl[0], lvl[1]
        if amount >= left:
            return got + left * price
        got += amount * price
        left -= amount
    return None


# ------------------------------------------------------------------
# mode 1: triangular arbitrage inside one exchange
# ------------------------------------------------------------------
# cycle:  Q1 --(buy X on X/Q1)--> X --(sell X on X/Q2)--> Q2 --(convert)--> Q1
# iterating ordered pairs (Q1,Q2) and (Q2,Q1) covers both directions of each cycle.

TRI_FIELDS = ["time", "exchange", "path", "leg1", "leg2", "leg3", "fee_legs", "fees_pct",
              "pct_top", "pct_book", "notional", "min_top_usd", "min_vol_usd"]


def find_triangles(ex, tickers, args):
    by_base = {}
    for s in tickers:
        base, quote = s.split("/")
        by_base.setdefault(base, {})[quote] = s

    results = []
    for base, quotes in by_base.items():
        if len(quotes) < 2:
            continue
        for q1, s1 in quotes.items():
            for q2, s2 in quotes.items():
                if q1 == q2:
                    continue
                if f"{q2}/{q1}" in tickers:
                    s3, side3 = f"{q2}/{q1}", "sell"
                    rate3 = tickers[s3]["bid"]
                elif f"{q1}/{q2}" in tickers:
                    s3, side3 = f"{q1}/{q2}", "buy"
                    rate3 = 1.0 / tickers[s3]["ask"]
                else:
                    continue

                f1, f2, f3 = (taker_fee(ex, s, args.fee) for s in (s1, s2, s3))
                fee_legs = sum(1 for f in (f1, f2, f3) if f > 0)
                if fee_legs > args.max_fee_legs:
                    continue

                t1, t2, t3 = tickers[s1], tickers[s2], tickers[s3]
                if not all(is_fresh(t, args.max_age) for t in (t1, t2, t3)):
                    continue

                x = (1.0 / t1["ask"]) * (1 - f1)
                y = x * t2["bid"] * (1 - f2)
                z = y * rate3 * (1 - f3)
                pct = (z - 1) * 100
                if pct < args.min_pct:
                    continue

                q3 = s3.split("/")[1]
                vols = [quote_volume_usd(t1, q1, tickers),
                        quote_volume_usd(t2, q2, tickers),
                        quote_volume_usd(t3, q3, tickers)]
                if min(vols) < args.min_volume:
                    continue

                tops = [top_book_usd(t1, "ask", q1, tickers),
                        top_book_usd(t2, "bid", q2, tickers),
                        top_book_usd(t3, "bid" if side3 == "sell" else "ask", q3, tickers)]
                tops = [v for v in tops if v is not None]

                results.append({
                    "exchange": ex.id, "path": f"{q1}>{base}>{q2}>{q1}",
                    "start": q1, "leg1": s1, "leg2": s2, "leg3": s3, "side3": side3,
                    "f1": f1, "f2": f2, "f3": f3,
                    "fee_legs": fee_legs, "fees_pct": (f1 + f2 + f3) * 100,
                    "pct_top": pct, "pct_book": None, "notional": args.notional,
                    "min_top_usd": min(tops) if tops else None,
                    "min_vol_usd": min(vols),
                })
    return results


def verify_triangle(ex, r, tickers, args):
    """Net profit for `notional` USD walked through the real order books."""
    p = usd_price(r["start"], tickers)
    if not p:
        return None
    start = args.notional / p
    try:
        b1 = ex.fetch_order_book(r["leg1"], limit=args.depth)
        b2 = ex.fetch_order_book(r["leg2"], limit=args.depth)
        b3 = ex.fetch_order_book(r["leg3"], limit=args.depth)
    except Exception as e:
        print(f"  [orderbook error] {r['path']}: {e}")
        return None

    x = walk_buy(b1["asks"], start)
    if x is None:
        return None
    x *= 1 - r["f1"]
    y = walk_sell(b2["bids"], x)
    if y is None:
        return None
    y *= 1 - r["f2"]
    z = walk_sell(b3["bids"], y) if r["side3"] == "sell" else walk_buy(b3["asks"], y)
    if z is None:
        return None
    z *= 1 - r["f3"]
    return (z / start - 1) * 100


def run_tri(args):
    global BNB_DISCOUNT
    BNB_DISCOUNT = args.bnb

    key = secret = None
    if not args.no_keys:
        key, secret = ask_keys()
    ex = load_exchange(args.exchange, key, secret)
    print(f"{ex.id}: {len(spot_symbols(ex))} spot markets loaded")
    if key:
        load_personal_fees(ex)
    else:
        print("WARNING: no API key -> using public default fees (zero-fee pairs will be missed)")

    print(f"settings: max_fee_legs={args.max_fee_legs}  bnb={args.bnb}  notional=${args.notional}"
          f"  min_volume=${args.min_volume:,.0f}  log={args.log}")
    print("stop with Ctrl+C")

    while True:
        t0 = time.time()
        try:
            tickers = fetch_spot_tickers(ex)
            res = find_triangles(ex, tickers, args)
            res.sort(key=lambda r: r["pct_top"], reverse=True)
            for r in res[: args.verify]:
                r["pct_book"] = verify_triangle(ex, r, tickers, args)

            ts = stamp()
            print(f"\n[{ts}] {len(tickers)} tickers | {len(res)} candidates")
            if res:
                print(f"{'path':30} {'feeLegs':>7} {'fees%':>7} {'top%':>8} {'book%':>8} "
                      f"{'topUSD':>10} {'vol24hUSD':>14}")
            for r in res[: args.top]:
                r["time"] = ts
                print(f"{r['path']:30} {r['fee_legs']:>7} {fmt(r['fees_pct'], 3):>7} "
                      f"{fmt(r['pct_top']):>8} {fmt(r['pct_book']):>8} "
                      f"{fmt(r['min_top_usd'], 0):>10} {r['min_vol_usd']:>14,.0f}")
            log_rows(args.log, res[: args.top], TRI_FIELDS)

            for r in res[: args.verify]:
                if r["pct_book"] is not None and r["pct_book"] >= args.alert:
                    notify(f"TRI {ex.id} {r['path']}\nnet {r['pct_book']:.4f}% on ${args.notional}\n"
                           f"{r['leg1']} | {r['leg2']} | {r['leg3']}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[error] {e}")

        if args.loop <= 0:
            break
        time.sleep(max(0.0, args.loop - (time.time() - t0)))


# ------------------------------------------------------------------
# mode 2: cross-exchange price gaps
# ------------------------------------------------------------------

CROSS_FIELDS = ["time", "symbol", "buy_on", "sell_on", "ask", "bid",
                "pct", "pct_book", "transfer", "network", "wd_fee_pct", "net_after_wd",
                "notional", "min_vol_usd", "suspicious"]

# read-only API keys per exchange, taken from environment variables
CRED_ENV = {
    "binance": ("BINANCE_API_KEY", "BINANCE_SECRET", None),
    "okx": ("OKX_API_KEY", "OKX_SECRET", "OKX_PASSPHRASE"),
    "kucoin": ("KUCOIN_API_KEY", "KUCOIN_SECRET", "KUCOIN_PASSPHRASE"),
    "bybit": ("BYBIT_API_KEY", "BYBIT_SECRET", None),
}

NET_ALIASES = {
    "ETH": "ERC20", "ETHEREUM": "ERC20", "BSC": "BEP20", "BNB SMART CHAIN": "BEP20",
    "TRX": "TRC20", "TRON": "TRC20", "ARB": "ARBITRUM", "ARBONE": "ARBITRUM",
    "ARBITRUM ONE": "ARBITRUM", "MATIC": "POLYGON", "OP": "OPTIMISM", "SOLANA": "SOL",
    "AVAXC": "AVAX", "AVAX C-CHAIN": "AVAX",
}


def creds_for(name):
    k, s, p = CRED_ENV.get(name, (None, None, None))
    return (os.getenv(k) if k else None,
            os.getenv(s) if s else None,
            os.getenv(p) if p else None)


def norm_net(n):
    n = str(n).upper().strip()
    return NET_ALIASES.get(n, n)


def load_transfer_info(ex):
    """base -> {network: {dep, wd, fee, contract}}, or None if unavailable."""
    try:
        cur = ex.fetch_currencies()
    except Exception as e:
        print(f"  [transfer status {ex.id}] {str(e)[:120]}")
        return None
    if not cur:
        return None
    out = {}
    for code, c in cur.items():
        nets = {}
        for nk, n in (c.get("networks") or {}).items():
            info = n.get("info") or {}
            contract = (info.get("contractAddress") or info.get("ctAddr")
                        or info.get("contract_address") or "")
            nets[norm_net(n.get("network") or nk)] = {
                "dep": n.get("deposit"), "wd": n.get("withdraw"),
                "fee": n.get("fee"), "contract": str(contract).lower().strip(),
            }
        if not nets:
            nets["*"] = {"dep": c.get("deposit"), "wd": c.get("withdraw"),
                         "fee": c.get("fee"), "contract": ""}
        out[code] = nets
    return out


def route_transfer(t_buy, t_sell, base, price, notional):
    """Buy on A, withdraw from A, deposit to B: cheapest open common network and its fee."""
    if t_buy is None or t_sell is None:
        return {"status": "unknown"}
    a, b = t_buy.get(base), t_sell.get(base)
    if not a or not b:
        return {"status": "unknown"}
    best, mismatch, common = None, False, False
    for net, na in a.items():
        nb = b.get(net)
        if not nb:
            continue
        common = True
        if na["contract"] and nb["contract"] and na["contract"] != nb["contract"]:
            mismatch = True
            continue
        if na["wd"] is False or nb["dep"] is False:
            continue
        fee_pct = (na["fee"] * price / notional * 100) if na["fee"] is not None else None
        unsure = na["wd"] is None or nb["dep"] is None
        cand = (fee_pct if fee_pct is not None else 999.0, net, fee_pct, unsure)
        if best is None or cand < best:
            best = cand
    if best:
        return {"status": "unsure" if best[3] else "open", "network": best[1], "wd_fee_pct": best[2]}
    if mismatch:
        return {"status": "diff-token"}
    if not common:
        return {"status": "no-common-net"}
    return {"status": "closed"}


def verify_cross(exs, r, args):
    """Net profit for `notional` USD walked through both exchanges' order books."""
    try:
        ba = exs[r["buy_on"]].fetch_order_book(r["symbol"], limit=args.depth)
        bb = exs[r["sell_on"]].fetch_order_book(r["symbol"], limit=args.depth)
    except Exception as e:
        print(f"  [orderbook error] {r['symbol']}: {e}")
        return None
    x = walk_buy(ba["asks"], args.notional)          # quote = USDT/USDC
    if x is None:
        return None
    x *= 1 - r["fa"]
    y = walk_sell(bb["bids"], x)
    if y is None:
        return None
    y *= 1 - r["fb"]
    return (y / args.notional - 1) * 100


def run_cross(args):
    exs = {}
    for n in args.exchanges:
        key, secret, pw = creds_for(n)
        try:
            exs[n] = load_exchange(n, key, secret, pw)
            print(f"{n}: {len(spot_symbols(exs[n]))} spot markets loaded"
                  f"{' (with API key)' if key else ''}")
        except Exception as e:
            print(f"[{n}] failed with API key: {str(e)[:150]}" if key else f"[skip {n}] {e}")
            if key:
                try:
                    exs[n] = load_exchange(n)
                    print(f"{n}: loaded WITHOUT key (transfer status unavailable)")
                except Exception as e2:
                    print(f"[skip {n}] {e2}")
    if len(exs) < 2:
        print("need at least 2 working exchanges")
        return

    transfer, last_status = {}, 0.0

    while True:
        t0 = time.time()
        all_t = {}
        for n, ex in exs.items():
            try:
                all_t[n] = fetch_spot_tickers(ex)
            except Exception as e:
                print(f"[tickers error {n}] {e}")

        rows = []
        symbols = set().union(*[set(t) for t in all_t.values()]) if all_t else set()
        for s in symbols:
            base, quote = s.split("/")
            if quote not in args.quotes:
                continue
            have = [n for n in all_t if s in all_t[n]]
            if len(have) < 2:
                continue
            for a in have:
                for b in have:
                    if a == b:
                        continue
                    ta, tb = all_t[a][s], all_t[b][s]
                    if not (is_fresh(ta, args.max_age) and is_fresh(tb, args.max_age)):
                        continue
                    fa = taker_fee(exs[a], s, args.fee)
                    fb = taker_fee(exs[b], s, args.fee)
                    out = (1.0 / ta["ask"]) * (1 - fa) * tb["bid"] * (1 - fb)
                    pct = (out - 1) * 100
                    if pct < args.min_pct:
                        continue
                    vol = min(quote_volume_usd(ta, quote, all_t[a]),
                              quote_volume_usd(tb, quote, all_t[b]))
                    if vol < args.min_volume:
                        continue
                    rows.append({
                        "symbol": s, "buy_on": a, "sell_on": b,
                        "ask": ta["ask"], "bid": tb["bid"], "pct": pct,
                        "min_vol_usd": vol, "suspicious": pct >= args.suspicious,
                        "fa": fa, "fb": fb, "pct_book": None, "notional": args.notional,
                        "transfer": None, "network": None, "wd_fee_pct": None, "net_after_wd": None,
                    })

        if time.time() - last_status >= args.status_every:
            for n, ex in exs.items():
                transfer[n] = load_transfer_info(ex)
                ok = "ok" if transfer[n] else "UNAVAILABLE (needs read-only API key)"
                print(f"  transfer status {n}: {ok}")
            last_status = time.time()

        rows.sort(key=lambda r: r["pct"], reverse=True)
        for r in [r for r in rows if not r["suspicious"]][: args.verify]:
            r["pct_book"] = verify_cross(exs, r, args)
            base = r["symbol"].split("/")[0]
            tr = route_transfer(transfer.get(r["buy_on"]), transfer.get(r["sell_on"]),
                                base, r["ask"], args.notional)
            r["transfer"] = tr["status"]
            r["network"] = tr.get("network")
            r["wd_fee_pct"] = tr.get("wd_fee_pct")
            if r["pct_book"] is not None and r["wd_fee_pct"] is not None:
                r["net_after_wd"] = r["pct_book"] - r["wd_fee_pct"]

        ts = stamp()
        print(f"\n[{ts}] {len(rows)} cross-exchange candidates (after taker fees, before withdrawal fees)")
        print(f"{'symbol':16}{'buy on':>9}{'sell on':>9}{'top%':>8}{'book%':>8}{'transfer':>14}"
              f"{'network':>10}{'wdFee%':>8}{'net%':>8}{'vol24hUSD':>13}  note")
        for r in rows[: args.top]:
            r["time"] = ts
            note = "CHECK: same token? withdrawals open?" if r["suspicious"] else ""
            book = "-" if r["pct_book"] is None else f"{r['pct_book']:.3f}"
            wdf = "-" if r.get("wd_fee_pct") is None else f"{r['wd_fee_pct']:.3f}"
            net = "-" if r.get("net_after_wd") is None else f"{r['net_after_wd']:.3f}"
            print(f"{r['symbol']:16}{r['buy_on']:>9}{r['sell_on']:>9}{r['pct']:>8.3f}{book:>8}"
                  f"{r.get('transfer') or '-':>14}{r.get('network') or '-':>10}{wdf:>8}{net:>8}"
                  f"{r['min_vol_usd']:>13,.0f}  {note}")
        log_rows(args.log, rows[: args.top], CROSS_FIELDS)

        now = time.time()
        for r in rows[: args.top]:
            if r["suspicious"] or r["pct_book"] is None:
                continue
            status = r.get("transfer")
            if status != "open" and not (args.allow_unknown and status in ("unknown", "unsure")):
                continue
            value = r.get("net_after_wd")
            if value is None:
                value = r["pct_book"]
            if value < args.alert:
                continue
            k = (r["symbol"], r["buy_on"], r["sell_on"])
            if now - ALERTED.get(k, 0) < args.realert:
                continue
            ALERTED[k] = now
            wd_line = (f"withdraw fee {r['wd_fee_pct']:.3f}% -> net {r['net_after_wd']:.3f}%"
                       if r.get("net_after_wd") is not None else "withdraw fee: unknown")
            notify(f"CROSS {r['symbol']}  [transfer: {status}"
                   f"{' via ' + r['network'] if r.get('network') else ''}]\n"
                   f"buy on {r['buy_on']} @ {r['ask']}\n"
                   f"sell on {r['sell_on']} @ {r['bid']}\n"
                   f"after trading fees: {r['pct_book']:.3f}% on ${args.notional:,.0f}\n"
                   f"{wd_line}\nvol ${r['min_vol_usd']:,.0f}")
            print(f"  -> alert sent: {r['symbol']} {r['buy_on']}->{r['sell_on']}")

        if args.loop <= 0:
            break
        time.sleep(max(0.0, args.loop - (time.time() - t0)))


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Spot arbitrage scanner (alerts only, no trading)")
    sub = p.add_subparsers(dest="mode", required=True)

    def common(sp, log_default, min_volume, alert_default):
        sp.add_argument("--fee", type=float, default=None,
                        help="force one taker fee for all pairs (normally leave empty)")
        sp.add_argument("--min-pct", type=float, default=0.0, help="min net %% to list (default 0)")
        sp.add_argument("--min-volume", type=float, default=min_volume, help="min 24h volume per pair in USD")
        sp.add_argument("--max-age", type=float, default=120, help="ignore tickers older than N seconds")
        sp.add_argument("--top", type=int, default=20, help="rows to print/log")
        sp.add_argument("--alert", type=float, default=alert_default, help="telegram alert threshold in %%")
        sp.add_argument("--loop", type=float, default=0, help="repeat every N seconds (0 = run once)")
        sp.add_argument("--log", default=log_default, help="CSV log file")

    t = sub.add_parser("tri", help="triangular arbitrage inside one exchange")
    t.add_argument("--exchange", default="binance")
    t.add_argument("--notional", type=float, default=1000, help="USD size used to verify on the order book")
    t.add_argument("--depth", type=int, default=20, help="order book levels to fetch")
    t.add_argument("--verify", type=int, default=10, help="how many top candidates to verify on the book")
    t.add_argument("--max-fee-legs", type=int, default=3,
                   help="only triangles with at most N legs that have a fee (0 = fully zero-fee)")
    t.add_argument("--bnb", action="store_true", help="apply 25%% BNB discount to non-zero fees")
    t.add_argument("--no-keys", action="store_true", help="run without API key (public default fees)")
    common(t, "tri_log.csv", 20_000, 0.02)

    c = sub.add_parser("cross", help="price gaps between exchanges")
    c.add_argument("--exchanges", nargs="+", default=["binance", "kucoin", "bybit", "okx"])
    c.add_argument("--quotes", nargs="+", default=["USDT", "USDC"])
    c.add_argument("--suspicious", type=float, default=10.0,
                   help="gaps above this %% are flagged (often different token or closed withdrawals)")
    c.add_argument("--notional", type=float, default=1000, help="USD size used to verify on both order books")
    c.add_argument("--depth", type=int, default=20)
    c.add_argument("--verify", type=int, default=10, help="how many top candidates to verify on the books")
    c.add_argument("--realert", type=float, default=1800, help="seconds before alerting the same route again")
    c.add_argument("--status-every", type=float, default=600,
                   help="refresh deposit/withdraw status every N seconds")
    c.add_argument("--allow-unknown", action="store_true",
                   help="also alert when transfer status is unknown (no API key)")
    common(c, "cross_log.csv", 200_000, 0.15)

    args = p.parse_args()
    try:
        run_tri(args) if args.mode == "tri" else run_cross(args)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
