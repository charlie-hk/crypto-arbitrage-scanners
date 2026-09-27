#!/usr/bin/env python3
"""
funding_scanner.py
Binance funding-rate arbitrage scanner + exit watcher.
Suggests and alerts only - it never places orders.

scan  : ranks tokens by realised funding over the last 1/3/7/30 days
watch : monitors positions you hold and alerts when funding
        weakens or flips against you

Install:
    pip install ccxt requests

Examples:
    python funding_scanner.py scan
    python funding_scanner.py scan --bnb --top 15
    python funding_scanner.py scan --keys --loop 1800          (every 30 min + Telegram alerts)
    python funding_scanner.py watch ONE:rev STEEM:rev --loop 300
    python funding_scanner.py watch BTC:pos --loop 300

pos = positive carry  (buy spot + short perp)          when funding is positive
rev = reverse carry   (borrow & sell spot + long perp) when funding is negative

Read-only API key (optional, for margin borrow rates in 'rev' mode):
    prompted with --keys, or read from BINANCE_API_KEY / BINANCE_SECRET
Telegram (optional): TELEGRAM_TOKEN , TELEGRAM_CHAT_ID
"""

import argparse
import os
import time
from datetime import datetime, timezone

import ccxt

DAY_MS = 86_400_000


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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


def connect(use_keys):
    key = secret = None
    if use_keys:
        key = os.getenv("BINANCE_API_KEY") or input("Binance API Key (read-only): ").strip()
        secret = os.getenv("BINANCE_SECRET") or input("Binance Secret Key: ").strip()
    cfg = {"enableRateLimit": True}
    if key and secret:
        cfg.update(apiKey=key, secret=secret)
    spot = ccxt.binance(dict(cfg, options={"defaultType": "spot"}))
    fut = ccxt.binanceusdm({"enableRateLimit": True})
    spot.load_markets()
    fut.load_markets()
    return spot, fut, bool(key and secret)


def perp_to_spot(perp_symbol):
    # 'ONE/USDT:USDT' -> 'ONE/USDT'
    return perp_symbol.split(":")[0]


def funding_stats(fut, perp_symbol, direction_hint=None):
    """Funding statistics for the last 30 days, in % of position size."""
    since = fut.milliseconds() - 30 * DAY_MS
    hist = fut.fetch_funding_rate_history(perp_symbol, since=since, limit=1000)
    if len(hist) < 3:
        return None
    hist.sort(key=lambda h: h["timestamp"])
    now = fut.milliseconds()
    interval_h = max(1.0, (hist[-1]["timestamp"] - hist[-2]["timestamp"]) / 3_600_000)

    def cum(days):
        return 100 * sum(h["fundingRate"] for h in hist if h["timestamp"] > now - days * DAY_MS)

    last7 = [h["fundingRate"] for h in hist if h["timestamp"] > now - 7 * DAY_MS]
    c1, c3, c7, c30 = cum(1), cum(3), cum(7), cum(30)
    sign = direction_hint if direction_hint else (1 if c7 >= 0 else -1)
    consistency = (sum(1 for r in last7 if r * sign > 0) / len(last7)) if last7 else 0.0
    days_covered = (now - hist[0]["timestamp"]) / DAY_MS
    return {
        "interval_h": interval_h, "c1": c1, "c3": c3, "c7": c7, "c30": c30,
        "consistency": consistency, "days_covered": days_covered,
    }


def borrow_daily_pct(spot, base):
    """Daily margin borrow rate in %. Needs an API key. None if unavailable."""
    try:
        r = spot.fetch_cross_borrow_rate(base)
        rate, period = r.get("rate"), r.get("period") or DAY_MS
        if rate is None:
            return None
        return 100 * rate * (DAY_MS / period)
    except Exception:
        return None


def fees_roundtrip_pct(spot, fut, spot_sym, perp_sym, bnb):
    """Entry + exit cost for both legs, in % of position size."""
    fs = spot.markets[spot_sym].get("taker") or 0.001
    ff = fut.markets[perp_sym].get("taker") or 0.0005
    if bnb:
        fs *= 0.75
        ff *= 0.90
    return 2 * (fs + ff) * 100


# ------------------------------------------------------------------
# scan
# ------------------------------------------------------------------

def scan_once(spot, fut, has_keys, args):
    rates = fut.fetch_funding_rates()
    fut_t = fut.fetch_tickers()
    spot_t = spot.fetch_tickers()

    # step 1: quick filter on the current rate
    pre = []
    for ps, r in rates.items():
        m = fut.markets.get(ps)
        if not m or not m.get("linear") or m.get("settle") != "USDT" or not m.get("active", True):
            continue
        ss = perp_to_spot(ps)
        if ss not in spot.markets or not spot.markets[ss].get("active", True) or ss not in spot_t:
            continue  # no matching spot pair (e.g. 1000PEPE)
        fr = r.get("fundingRate")
        if fr is None:
            continue
        fv = (fut_t.get(ps) or {}).get("quoteVolume") or 0
        sv = (spot_t.get(ss) or {}).get("quoteVolume") or 0
        if min(fv, sv) < args.min_volume:
            continue
        pre.append((abs(fr), ps, ss, fv, sv))
    pre.sort(reverse=True)
    pre = pre[: args.candidates]

    # step 2: realised funding history
    rows = []
    for _, ps, ss, fv, sv in pre:
        try:
            st = funding_stats(fut, ps)
        except Exception as e:
            print(f"  [history error] {ps}: {e}")
            continue
        if not st:
            continue
        direction = "pos" if st["c7"] >= 0 else "rev"
        sign = 1 if direction == "pos" else -1
        base = ss.split("/")[0]
        borrow = None
        if direction == "rev":
            if not spot.markets[ss].get("margin"):
                continue  # 'rev' requires the token to be borrowable on margin
            if has_keys:
                borrow = borrow_daily_pct(spot, base)

        daily_7d = sign * st["c7"] / 7              # daily % of position (7-day average)
        daily_1d = sign * st["c1"]                  # daily % (last 24h)
        net_7d = daily_7d - (borrow or 0)
        net_1d = daily_1d - (borrow or 0)
        day_cap = net_7d / 2                        # capital is split across both legs (1x hedge)
        day_cap_1d = net_1d / 2
        fees = fees_roundtrip_pct(spot, fut, ss, ps, args.bnb)
        breakeven = (fees / net_7d) if net_7d > 0 else None
        if st["consistency"] < args.min_consistency or day_cap <= 0:
            continue

        rows.append({
            "sym": base, "dir": direction, "int": st["interval_h"],
            "c1": st["c1"], "c3": st["c3"], "c7": st["c7"], "c30": st["c30"],
            "cons": st["consistency"], "borrow": borrow,
            "day_cap": day_cap, "month_cap": day_cap * 30, "month_1d": day_cap_1d * 30,
            "fading": daily_1d < args.fade_ratio * daily_7d,
            "breakeven": breakeven, "vol": min(fv, sv),
        })

    rows.sort(key=lambda r: r["day_cap"], reverse=True)
    return rows


def print_rows(rows, args):
    print(f"\n[{stamp()}]  ranked by net daily yield on capital (7-day average, 1x hedge)")
    print(f"{'token':10}{'dir':>4}{'int':>4}{'1d%':>8}{'3d%':>8}{'7d%':>8}{'30d%':>9}"
          f"{'same%':>7}{'borrow/d':>9}{'cap/30d%':>9}{'30d@1d%':>9}{'BE days':>8}{'vol$':>12}")
    for r in rows[: args.top]:
        b = "-" if r["dir"] == "pos" else ("?" if r["borrow"] is None else f"{r['borrow']:.3f}")
        be = "-" if r["breakeven"] is None else f"{r['breakeven']:.1f}"
        fade = "  FADING" if r["fading"] else ""
        print(f"{r['sym']:10}{r['dir']:>4}{r['int']:>3.0f}h{r['c1']:>8.3f}{r['c3']:>8.3f}{r['c7']:>8.3f}"
              f"{r['c30']:>9.3f}{100*r['cons']:>6.0f}%{b:>9}{r['month_cap']:>9.2f}{r['month_1d']:>9.2f}"
              f"{be:>8}{r['vol']:>12,.0f}{fade}")


def qualifies(r, args):
    return (
        args.alert_dir in (r["dir"], "both")
        and r["month_cap"] >= args.alert_month
        and r["month_1d"] >= args.alert_month
        and r["cons"] >= args.alert_consistency
        and r["breakeven"] is not None and r["breakeven"] <= args.alert_be
        and not r["fading"]
        and not (r["dir"] == "rev" and r["borrow"] is None)
    )


def alert_text(r, title):
    b = "" if r["dir"] == "pos" else f"\nborrow/day {r['borrow']:.3f}%"
    how = ("buy spot + short perp" if r["dir"] == "pos"
           else "borrow & sell spot + long perp")
    return (f"{title}: {r['sym']} ({r['dir']})\n{how}\n"
            f"est. 30d on capital: {r['month_cap']:.2f}% (7d avg) / {r['month_1d']:.2f}% (last 24h)\n"
            f"funding 1d {r['c1']:.3f}% | 7d {r['c7']:.3f}% | same {100*r['cons']:.0f}%\n"
            f"break-even {r['breakeven']:.1f} days | vol ${r['vol']:,.0f}{b}")


def run_scan(args):
    spot, fut, has_keys = connect(args.keys)
    alerted = {}   # sym -> last row that was alerted
    if args.loop > 0:
        msg = (f"funding scanner started. every {args.loop/60:.0f} min | alert: {args.alert_dir}, "
               f">= {args.alert_month}%/30d, BE <= {args.alert_be}d, same >= {100*args.alert_consistency:.0f}%")
        print(msg)
        notify(msg)

    while True:
        t0 = time.time()
        try:
            rows = scan_once(spot, fut, has_keys, args)
            print_rows(rows, args)

            good = {r["sym"]: r for r in rows if qualifies(r, args)}
            for sym, r in good.items():
                if sym not in alerted:
                    notify(alert_text(r, "NEW OPPORTUNITY"))
                    print(f"  -> alert sent: {sym}")
            for sym in list(alerted):
                if sym not in good:
                    notify(f"NO LONGER QUALIFIES: {sym}\n(if you are in it, check the watcher)")
                    print(f"  -> dropped: {sym}")
                    del alerted[sym]
            alerted.update(good)

            if any(r["dir"] == "rev" and r["borrow"] is None for r in rows[: args.top]):
                print("NOTE: 'rev' rows with borrow '?' ignore margin interest. Run with --keys to include it.")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{stamp()}] error: {e}")

        if args.loop <= 0:
            break
        print(f"next scan in {args.loop/60:.0f} min ...")
        time.sleep(max(0.0, args.loop - (time.time() - t0)))


# ------------------------------------------------------------------
# watch
# ------------------------------------------------------------------

def run_watch(args):
    spot, fut, has_keys = connect(args.keys)
    positions = []
    for p in args.positions:
        base, _, d = p.partition(":")
        base = base.upper().split("/")[0]
        d = (d or "pos").lower()
        if d not in ("pos", "rev"):
            print(f"bad direction in {p} (use pos or rev)")
            return
        ps = f"{base}/USDT:USDT"
        if ps not in fut.markets:
            print(f"{ps} not found on Binance futures")
            return
        positions.append((base, ps, d))

    last_state = {}
    print(f"watching {len(positions)} position(s). exit if 24h funding in your favour < "
          f"{args.exit_below}% or next rate flips. Ctrl+C to stop.")
    while True:
        for base, ps, d in positions:
            try:
                sign = 1 if d == "pos" else -1
                st = funding_stats(fut, ps, direction_hint=sign)
                nxt = fut.fetch_funding_rate(ps).get("fundingRate") or 0.0
                borrow = borrow_daily_pct(spot, base) if (d == "rev" and has_keys) else None
            except Exception as e:
                print(f"[{stamp()}] {base}: error {e}")
                continue

            favour_24h = sign * st["c1"] - (borrow or 0)      # daily % in your favour
            favour_7d = sign * st["c7"] / 7 - (borrow or 0)
            next_ok = nxt * sign > 0

            if favour_24h < args.exit_below or not next_ok:
                state = "EXIT"
            elif favour_24h < 0.5 * favour_7d:
                state = "WEAKENING"
            else:
                state = "HOLD"

            b = "" if borrow is None else f" borrow/d {borrow:.3f}%"
            line = (f"[{stamp()}] {base:8} {d}  {state:9} 24h {favour_24h:+.3f}%  "
                    f"7d-avg/day {favour_7d:+.3f}%  next {100*nxt:+.4f}%{b}")
            print(line)
            if state != last_state.get(base) and state in ("EXIT", "WEAKENING"):
                notify(line)
            last_state[base] = state

        if args.loop <= 0:
            break
        time.sleep(args.loop)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Binance funding arbitrage scanner / exit watcher (no trading)")
    sub = p.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--candidates", type=int, default=40, help="how many to check history for")
    s.add_argument("--min-volume", type=float, default=2_000_000, help="min 24h USD volume on spot AND perp")
    s.add_argument("--min-consistency", type=float, default=0.7,
                   help="share of last-7d funding payments in your favour (0-1)")
    s.add_argument("--bnb", action="store_true", help="BNB fee discount")
    s.add_argument("--keys", action="store_true", help="use read-only API key (margin borrow rates)")
    s.add_argument("--loop", type=float, default=0, help="rescan every N seconds (0 = once), e.g. 1800")
    s.add_argument("--fade-ratio", type=float, default=0.6,
                   help="FADING if last-24h funding < this x the 7-day daily average")
    s.add_argument("--alert-dir", choices=["pos", "rev", "both"], default="pos",
                   help="which directions to send telegram alerts for")
    s.add_argument("--alert-month", type=float, default=1.5,
                   help="alert if estimated 30-day %% on capital >= this (both 7d and 24h based)")
    s.add_argument("--alert-consistency", type=float, default=0.9)
    s.add_argument("--alert-be", type=float, default=5, help="max break-even days for an alert")

    w = sub.add_parser("watch")
    w.add_argument("positions", nargs="+", help="e.g. ONE:rev BTC:pos")
    w.add_argument("--loop", type=float, default=300, help="seconds between checks (0 = once)")
    w.add_argument("--exit-below", type=float, default=0.02,
                   help="EXIT when last-24h funding in your favour is below this %% per day")
    w.add_argument("--keys", action="store_true", help="use read-only API key (margin borrow rates)")

    args = p.parse_args()
    try:
        run_scan(args) if args.mode == "scan" else run_watch(args)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
