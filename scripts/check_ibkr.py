#!/usr/bin/env python3
"""Phase 0 smoke test — prove the IBKR gateway connection (no trading).

Exit criterion from docs/ROADMAP.md: connect, then reqCurrentTime +
reqAccountSummary + reqPositions all succeed, green in the logs.

Run (after `docker compose up -d` and filling .env):
    cd backend && . .venv/bin/activate
    python ../scripts/check_ibkr.py
"""
import asyncio
import sys
from pathlib import Path

# Make `app` importable when run from repo root or scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from ib_async import IB  # noqa: E402

from app.config import settings  # noqa: E402


async def main() -> int:
    ib = IB()
    print(f"→ connecting to {settings.ibgw_host}:{settings.ibgw_port} "
          f"(clientId={settings.ib_client_id}, mode={settings.trading_mode})")
    try:
        await ib.connectAsync(
            settings.ibgw_host,
            settings.ibgw_port,
            clientId=settings.ib_client_id,
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        print(f"✗ connect failed: {e!r}")
        print("  checklist: gateway up (`docker compose ps`), paper creds in .env, "
              "port 4002, clientId not already in use.")
        return 1

    # Hard money-safety guard: IBKR paper accounts are numbered "DU…", live "U…".
    # Refuse anything that isn't a paper account, regardless of the configured mode.
    accounts = ib.managedAccounts()
    print(f"  managed accounts: {accounts}")
    if not accounts:
        print("✗ no managed accounts returned — cannot verify paper; aborting.")
        ib.disconnect()
        return 2
    if not all(a.startswith("DU") for a in accounts):
        print(f"‼ NON-PAPER account detected ({accounts}) — Phase 0 must be paper-only. "
              "Aborting before any request.")
        ib.disconnect()
        return 2
    if settings.trading_mode != "paper":
        print(f"‼ TRADING_MODE={settings.trading_mode!r} is not 'paper' — aborting.")
        ib.disconnect()
        return 2
    print(f"✓ connected (PAPER, accounts={accounts})")

    try:
        server_time = await ib.reqCurrentTimeAsync()
        print(f"✓ reqCurrentTime: {server_time}")

        summary = await ib.accountSummaryAsync()
        wanted = {"NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds"}
        rows = {v.tag: f"{v.value} {v.currency}" for v in summary if v.tag in wanted}
        print("✓ reqAccountSummary:")
        for tag in sorted(rows):
            print(f"    {tag:18} {rows[tag]}")

        positions = await ib.reqPositionsAsync()
        print(f"✓ reqPositions: {len(positions)} open position(s)")
        for p in positions:
            print(f"    {p.contract.symbol:6} qty={p.position} avgCost={p.avgCost}")
    except Exception as e:  # noqa: BLE001
        print(f"✗ data request failed: {e!r}")
        return 1
    finally:
        ib.disconnect()

    print("\n✓ Phase 0 PASS — gateway connectivity proven.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
