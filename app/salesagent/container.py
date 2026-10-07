"""Process-wide dependencies, created once per app/worker and closed on shutdown."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .db import make_pool
from .domain.qualification import PriceBook
from .integrations.dataverse import DataverseClient
from .integrations.graph import GraphClient
from .integrations.http import TokenProvider
from .integrations.power_automate import PowerAutomateClient
from .integrations.teams_phone import CallChannel, TeamsPhoneChannel

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    pool: AsyncConnectionPool
    http: httpx.AsyncClient
    price_book: PriceBook
    tokens: TokenProvider | None = None
    graph: GraphClient | None = None
    dataverse: DataverseClient | None = None
    power_automate: PowerAutomateClient | None = None
    channel: CallChannel | None = None
    _closed: bool = field(default=False, repr=False)

    @classmethod
    async def create(cls, settings: Settings, pool_size: int = 10) -> "Container":
        pool = make_pool(settings.database_url.get_secret_value(), max_size=pool_size)
        await pool.open(wait=True, timeout=30)
        http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
        c = cls(
            settings=settings,
            pool=pool,
            http=http,
            price_book=PriceBook(
                tier1=settings.price_arpu_tier1,
                tier2=settings.price_arpu_tier2,
                tier3=settings.price_arpu_tier3,
                term_months=settings.price_term_months,
                min_lines=settings.price_min_lines,
            ),
        )
        if settings.entra_configured:
            c.tokens = TokenProvider(
                settings.azure_tenant_id, settings.azure_client_id, settings.azure_client_secret.get_secret_value()
            )
            if settings.graph_configured:
                c.graph = GraphClient(c.tokens, http)
            if settings.dataverse_configured:
                c.dataverse = DataverseClient(
                    c.tokens, http, settings.dataverse_url, settings.dataverse_prefix, settings.dataverse_table
                )
            if settings.teams_phone_configured:
                c.channel = TeamsPhoneChannel(
                    c.tokens,
                    settings.foundry_project_endpoint,
                    settings.foundry_agent_name,
                    settings.foundry_connection_name,
                    settings.teams_resource_account_id,
                )
        if settings.power_automate_configured:
            c.power_automate = PowerAutomateClient(http, settings.power_automate_webhook_url.get_secret_value())
        log.info(
            "integrations: teams_phone=%s graph=%s dataverse=%s power_automate=%s",
            bool(c.channel), bool(c.graph), bool(c.dataverse), bool(c.power_automate),
        )
        return c

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.channel:
            await self.channel.close()
        if self.tokens:
            await self.tokens.close()
        await self.http.aclose()
        await self.pool.close()
