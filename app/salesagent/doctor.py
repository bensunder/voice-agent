"""Check every configured integration end to end, read-only.

    docker compose exec tool-api python -m salesagent.doctor

Exits non-zero if any configured integration fails. Integrations that are not
configured are reported as SKIP.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import httpx

from .config import get_settings
from .container import Container


def line(status: str, name: str, detail: str = "") -> None:
    color = {"OK": "32", "FAIL": "31", "SKIP": "33"}[status]
    print(f"\033[{color}m{status:<4}\033[0m {name:<28} {detail}")


async def main() -> int:
    s = get_settings()
    failures = 0
    try:
        s.require_secrets()
        line("OK", "secrets")
    except RuntimeError as exc:
        line("FAIL", "secrets", str(exc))
        failures += 1

    c = await Container.create(s, pool_size=2)
    try:
        async with c.pool.connection() as conn:
            await conn.execute("SELECT 1")
        line("OK", "postgres")

        if c.tokens:
            try:
                await c.tokens.token("https://graph.microsoft.com/.default")
                line("OK", "entra service principal", s.azure_client_id)
            except Exception as exc:  # noqa: BLE001
                line("FAIL", "entra service principal", str(exc)[:200])
                failures += 1
        else:
            line("SKIP", "entra service principal", "AZURE_TENANT_ID / CLIENT_ID / secret not set")

        if c.graph:
            now = datetime.now(timezone.utc)
            for rep in s.reps:
                try:
                    blocks = await c.graph.busy_blocks(rep.upn, now, now + timedelta(days=2))
                    line("OK", f"graph calendar {rep.upn}", f"{len(blocks)} busy blocks in next 48h")
                except Exception as exc:  # noqa: BLE001
                    line("FAIL", f"graph calendar {rep.upn}", str(exc)[:200])
                    failures += 1
        else:
            line("SKIP", "graph", "no Entra app or SALES_REPS")

        if c.dataverse and c.tokens:
            base = s.dataverse_url.rstrip("/")
            try:
                tok = await c.tokens.token(base + "/.default")
                r = await c.http.get(f"{base}/api/data/v9.2/{s.dataverse_table}?$top=1",
                                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
                if r.status_code == 200:
                    line("OK", "dataverse table", s.dataverse_table)
                else:
                    line("FAIL", "dataverse table", f"HTTP {r.status_code}: {r.text[:200]}")
                    failures += 1
            except Exception as exc:  # noqa: BLE001
                line("FAIL", "dataverse", str(exc)[:200])
                failures += 1
        else:
            line("SKIP", "dataverse", "DATAVERSE_URL not set")

        if c.power_automate:
            url = s.power_automate_webhook_url.get_secret_value()
            ok = url.startswith("https://") and ("logic.azure.com" in url or "powerplatform.com" in url
                                                 or "powerautomate" in url)
            line("OK" if ok else "FAIL", "power automate trigger", "URL looks valid" if ok else "unexpected URL")
            failures += 0 if ok else 1
        else:
            line("SKIP", "power automate", "secrets/power_automate_webhook_url empty")

        if c.channel:
            try:
                await c.channel.get_call("00000000-0000-0000-0000-000000000000")
                line("OK", "foundry telephony", "reachable")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "404" in msg or "not found" in msg.lower():
                    line("OK", "foundry telephony", f"agent '{s.foundry_agent_name}' reachable (probe job not found, as expected)")
                else:
                    line("FAIL", "foundry telephony", msg[:240])
                    failures += 1
        else:
            line("SKIP", "teams phone / foundry", "FOUNDRY_* / TEAMS_RESOURCE_ACCOUNT_ID not set")

        try:
            r = await c.http.get(f"{s.public_tool_api_url}/healthz", timeout=8)
            line("OK" if r.status_code == 200 else "FAIL", "public tool api", f"{s.public_tool_api_url} -> {r.status_code}")
            failures += 0 if r.status_code == 200 else 1
        except httpx.HTTPError as exc:
            line("FAIL", "public tool api", str(exc)[:200])
            failures += 1
    finally:
        await c.close()
    print("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
