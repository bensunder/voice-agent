"""Runtime configuration.

Values come from environment variables; secrets come from Docker secrets mounted
at /run/secrets/<name> (pydantic-settings `secrets_dir`). A secret file always
wins over an environment variable of the same name.

Integrations are optional: when their settings are absent the service still
runs and reports the integration as "not configured" instead of failing. This
lets the stack be deployed and tested before the Microsoft 365 tenant, Teams
Phone number and Power Platform environment are provisioned.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")


@dataclass(frozen=True)
class SalesRep:
    upn: str
    display_name: str
    teams_user_id: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        secrets_dir=_SECRETS_DIR if os.path.isdir(_SECRETS_DIR) else None,
        extra="ignore",
    )

    # --- core -----------------------------------------------------------------
    environment: str = "production"
    log_level: str = "INFO"
    public_cockpit_url: str = "https://cockpit.whyaidata.com"
    public_tool_api_url: str = "https://sales-api.whyaidata.com"
    tenant_key: str = "default"

    database_url: SecretStr = SecretStr("postgresql://salesagent@db:5432/salesagent")

    # --- security -------------------------------------------------------------
    tool_api_key: SecretStr = SecretStr("")
    call_token_secret: SecretStr = SecretStr("")
    call_token_ttl_seconds: int = 3600
    cockpit_username: str = "demo"
    cockpit_password: SecretStr = SecretStr("")

    # --- demo guardrails ------------------------------------------------------
    demo_mode: bool = True
    demo_allowlist: str = ""  # comma-separated E.164 numbers that may be dialed
    browser_session_ttl_seconds: int = 1800

    # --- business rules -------------------------------------------------------
    business_timezone: str = "America/Denver"
    calling_window_start_hour: int = 8
    calling_window_end_hour: int = 21
    meeting_minutes: int = 30
    slot_hold_seconds: int = 180
    offer_days_ahead: int = 5
    workday_start_hour: int = 9
    workday_end_hour: int = 17
    sales_reps: str = ""  # "upn|Display Name|teamsUserObjectId;upn2|Name 2|id2"
    company_name: str = "Acme Wireless"
    product_name: str = "enterprise wireless service"

    # --- pricing (price book) -------------------------------------------------
    price_arpu_tier1: float = 38.0  # 100-499 lines, $/line/month
    price_arpu_tier2: float = 34.0  # 500-1999 lines
    price_arpu_tier3: float = 30.0  # 2000+ lines
    price_term_months: int = 36
    price_min_lines: int = 100

    # --- Microsoft Entra service principal ------------------------------------
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: SecretStr = SecretStr("")

    # --- Foundry voice agent + Teams Phone extensibility ----------------------
    foundry_project_endpoint: str = ""
    foundry_agent_name: str = ""
    foundry_connection_name: str = ""
    teams_resource_account_id: str = ""

    # --- Power Platform -------------------------------------------------------
    dataverse_url: str = ""  # e.g. https://org1234.crm.dynamics.com
    dataverse_table: str = "cr_aicallqualifications"  # entity set name
    dataverse_prefix: str = "cr"
    power_automate_webhook_url: SecretStr = SecretStr("")

    worker_poll_seconds: float = 2.0
    outbox_max_attempts: int = 8

    # --- MAF campaign orchestrator: escalation agent model --------------------
    # Foundry model deployment used by the MAF escalation agent (e.g. gpt-4.1-mini).
    # Uses FOUNDRY_PROJECT_ENDPOINT and the Entra service principal above.
    escalation_model: str = ""
    escalation_max_output_tokens: int = 400
    escalation_temperature: float = 0.2
    escalation_timeout_seconds: float = 20.0
    # Token pricing (USD per 1M tokens) used for the cost ledger and budget checks.
    llm_price_input_per_1m: float = 0.40
    llm_price_cached_input_per_1m: float = 0.10
    llm_price_output_per_1m: float = 1.60
    llm_daily_budget_usd: float = 5.0
    outcome_concurrency: int = 16

    # --- telemetry ------------------------------------------------------------
    otel_service_name: str = "ai-sales-agent"
    # OTLP/HTTP collector, e.g. http://otel-collector:4318 (an OpenTelemetry Collector can
    # forward to Azure Monitor / Application Insights, Grafana, Jaeger, ...)
    otel_exporter_otlp_endpoint: str = ""

    @field_validator("business_timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    # --- derived --------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.business_timezone)

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(n.strip() for n in self.demo_allowlist.split(",") if n.strip())

    @property
    def reps(self) -> list[SalesRep]:
        reps: list[SalesRep] = []
        for chunk in self.sales_reps.split(";"):
            parts = [p.strip() for p in chunk.split("|")]
            if not parts or not parts[0]:
                continue
            reps.append(
                SalesRep(
                    upn=parts[0],
                    display_name=parts[1] if len(parts) > 1 and parts[1] else parts[0],
                    teams_user_id=parts[2] if len(parts) > 2 and parts[2] else None,
                )
            )
        return reps

    @property
    def entra_configured(self) -> bool:
        return bool(
            self.azure_tenant_id and self.azure_client_id and self.azure_client_secret.get_secret_value()
        )

    @property
    def graph_configured(self) -> bool:
        return self.entra_configured and bool(self.reps)

    @property
    def dataverse_configured(self) -> bool:
        return self.entra_configured and bool(self.dataverse_url)

    @property
    def power_automate_configured(self) -> bool:
        return bool(self.power_automate_webhook_url.get_secret_value())

    @property
    def escalation_agent_configured(self) -> bool:
        return self.entra_configured and bool(self.foundry_project_endpoint and self.escalation_model)

    @property
    def teams_phone_configured(self) -> bool:
        return self.entra_configured and all(
            [
                self.foundry_project_endpoint,
                self.foundry_agent_name,
                self.foundry_connection_name,
                self.teams_resource_account_id,
            ]
        )

    def require_secrets(self) -> None:
        """Fail fast at startup when mandatory secrets are missing or weak."""
        missing = []
        if len(self.tool_api_key.get_secret_value()) < 24:
            missing.append("tool_api_key (>=24 chars)")
        if len(self.call_token_secret.get_secret_value()) < 32:
            missing.append("call_token_secret (>=32 chars)")
        if len(self.cockpit_password.get_secret_value()) < 12:
            missing.append("cockpit_password (>=12 chars)")
        if missing:
            raise RuntimeError("Missing or weak secrets: " + ", ".join(missing))


@lru_cache
def get_settings() -> Settings:
    return Settings()


def integration_status(s: Settings) -> dict[str, bool]:
    return {
        "teams_phone": s.teams_phone_configured,
        "graph": s.graph_configured,
        "dataverse": s.dataverse_configured,
        "power_automate": s.power_automate_configured,
        "escalation_agent": s.escalation_agent_configured,
    }


__all__ = ["Settings", "SalesRep", "get_settings", "integration_status"]
