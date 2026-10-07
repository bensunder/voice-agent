# Setup runbook

Everything runs in the **benwhyaidata.onmicrosoft.com** tenant. The Foundry project and the
Azure Communication Services (ACS) resource **must be in that same Entra tenant**.

Order matters: start the slow items (A1-A3) first, they provision in the background.

| # | Step | Time | Unblocks |
|---|------|------|----------|
| A | Tenant, licences, Teams number | 30 min + provisioning | Teams Phone |
| B | Deploy the stack on srv1507394 | 10 min | everything |
| C | Entra app registration | 10 min | Graph, Dataverse, Foundry calls |
| D | Foundry voice agent + tools | 20 min | the conversation |
| E | Power Platform (Dataverse + Power Automate) | 25 min | CRM record, rep card |
| F | Teams Phone extensibility | 45 min | real phone calls |
| G | Verify with `doctor`, rehearse | 15 min | demo |

---

## A. Tenant, licences, Teams number

1. **admin.microsoft.com** -> Users -> your account -> **Roles**: confirm *Global Administrator*.
2. **Billing -> Purchase services**: start the free trials for
   **Microsoft 365 Business Standard** (the variant *with Teams*) and
   **Teams Phone with Calling Plan**. Assign both to your user.
3. **Teams admin center -> Voice -> Phone numbers -> Add**: request a US **service** number
   (Calling Plan). You will assign it to the resource account in step F.
4. **Power Apps Developer Plan**: go to `https://aka.ms/PowerAppsDevPlan`, sign in with the same
   account. Note the environment URL (Power Platform admin center -> Environments ->
   *your developer environment* -> **Environment URL**, e.g. `https://org1a2b3c.crm.dynamics.com`).

## B. Deploy on srv1507394

```bash
mkdir -p /opt/ai-sales-agent && cd /opt/ai-sales-agent
tar -xzf /path/to/ai-sales-agent.tar.gz --strip-components=1
./deploy.sh --caddy          # builds, starts, installs Caddy blocks, prints status
cat secrets/cockpit_password # cockpit login (user: demo)
```

Open `https://cockpit.whyaidata.com`. All four integration pills are grey until C-F are done;
the cockpit, tools and browser-preview demo already work.

Edit settings: `nano .env` then `./deploy.sh`. Edit a secret: `nano secrets/<name>` then
`docker compose up -d`.

## C. Entra app registration (one app for Graph, Dataverse and Foundry)

1. **Entra admin center -> App registrations -> New registration**: `ai-sales-agent`, single tenant.
2. **Certificates & secrets -> New client secret**. Put the value in `secrets/azure_client_secret`.
3. **API permissions -> Add -> Microsoft Graph -> Application -> `Calendars.ReadWrite`** ->
   **Grant admin consent**.
   - Hardening (recommended after the demo): remove the tenant-wide grant and scope the app to the
     sales reps' mailboxes with Exchange RBAC for Applications:
     ```powershell
     Connect-ExchangeOnline
     New-ServicePrincipal -AppId <APP_ID> -ObjectId <ENTERPRISE_APP_OBJECT_ID> -DisplayName "ai-sales-agent"
     New-ManagementScope -Name "AI Sales Reps" -RecipientRestrictionFilter "MemberOfGroup -eq '<GROUP_DN>'"
     New-ManagementRoleAssignment -App <APP_ID> -Role "Application Calendars.ReadWrite" -CustomResourceScope "AI Sales Reps"
     ```
4. In `.env`: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `SALES_REPS` (use your own licensed
   mailbox as the rep for the demo, e.g.
   `SALES_REPS="benjmainsunder@benwhyaidata.onmicrosoft.com|Ben Sunder|<your Entra object id>"`).

## D. Foundry voice agent

1. Foundry portal -> your project -> **Build -> Agents -> Create -> Voice-based agent**,
   name `sales-qualifier`. Pick a real-time model and a natural US English voice.
2. **Instructions**: paste `agent/instructions.md`.
3. **Inputs** (structured inputs): add `first_name`, `company`, `company_name`,
   `product_interest`, `call_token` (string). Give `call_token` the default value `browser`, so
   the browser preview works without per-call inputs. If the instructions editor uses a
   different placeholder syntax than `{{name}}`, switch the placeholders to it.
4. **Tools -> Add -> OpenAPI**: import `https://sales-api.whyaidata.com/agent/openapi.json`.
   Authentication: **API key**, header `X-API-Key`, value = `cat secrets/tool_api_key`.
5. **Transfer target** (for the warm hand-off): name `sales_specialist`, kind **Teams**, value =
   the rep's Entra user object ID.
6. Test in the **browser preview**: in the cockpit capture a lead, press **Browser preview**,
   then talk to the agent. The cockpit fills in live.
7. Give the service principal from C the **Foundry User** (Azure AI User) role on the project,
   and set in `.env`: `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME=sales-qualifier`.

## E. Power Platform

**Dataverse application user**
1. Power Platform admin center -> Environments -> developer environment -> **Settings ->
   Users + permissions -> Application users -> New app user** -> select `ai-sales-agent` ->
   business unit (root) -> security role **System Administrator** (demo; use a custom role with
   create/write on the table for production).
2. Set `DATAVERSE_URL` in `.env`, run `./deploy.sh`, then create the table:
   ```bash
   docker compose run --rm tool-api python -m salesagent.provision_dataverse
   ```
   Copy the printed `DATAVERSE_PREFIX` and `DATAVERSE_TABLE` into `.env` and `./deploy.sh` again.

**Power Automate rep card** - see `power-automate/README.md` (10 minutes). Put the trigger URL in
`secrets/power_automate_webhook_url` and run `docker compose up -d`.

## F. Teams Phone extensibility (Teams number -> ACS -> Foundry)

Follows Microsoft's *Teams Phone extensibility quickstart*.

1. **Azure portal -> Create -> Communication Services** (same tenant). Note its **endpoint**
   (`https://<name>.communication.azure.com`), **Immutable Resource ID** (Overview) and ARM ID.
   No ACS phone number is needed - the number belongs to Teams.
2. **App registration + bot** for the resource account (quickstart step 2):
   ```powershell
   Connect-AzAccount
   Register-AzResourceProvider -ProviderNamespace Microsoft.BotService
   New-AzBotService -ResourceGroupName <rg> -Name "ai-sales-teams-phone" -ApplicationId <BOT_APP_ID> -Location "global" -Sku S1 -Description "AI sales Teams Phone"
   ```
   (Use a separate app registration for `<BOT_APP_ID>`; set the bot messaging endpoint to
   `https://eventgrid.azure.net` as the quickstart requires.)
3. **Resource account** (Teams PowerShell):
   ```powershell
   Connect-MicrosoftTeams
   New-CsOnlineApplicationInstance -UserPrincipalName ai-sales@benwhyaidata.onmicrosoft.com -ApplicationId <BOT_APP_ID> -DisplayName "Acme AI Sales"
   Set-CsOnlineApplicationInstance -Identity ai-sales@benwhyaidata.onmicrosoft.com -ApplicationId <BOT_APP_ID> -AcsResourceId <ACS_IMMUTABLE_RESOURCE_ID>
   Sync-CsOnlineApplicationInstance -ObjectId <RESOURCE_ACCOUNT_OBJECT_ID> -ApplicationId <BOT_APP_ID>
   ```
4. **Licences**: assign *Microsoft Teams Phone Resource Account* **and** a Calling Plan to the
   resource account (Calling Plan is needed for outbound PSTN).
5. **Number**:
   ```powershell
   Set-CsPhoneNumberAssignment -Identity ai-sales@benwhyaidata.onmicrosoft.com -PhoneNumber +1XXXXXXXXXX -PhoneNumberType CallingPlan
   ```
6. **Server consent** for ACS:
   ```bash
   az rest --method put \
     --url "https://<acs-name>.communication.azure.com/access/teamsExtension/tenants/<TENANT_ID>/assignments/<RESOURCE_ACCOUNT_OBJECT_ID>?api-version=2025-06-30" \
     --resource "https://communication.azure.com" \
     --body '{"principalType":"teamsResourceAccount"}'
   ```
7. **Foundry project connection**: Settings -> Project connections -> New ->
   category **AzureCommunicationServices**, target = ACS endpoint, metadata `ResourceId` = ACS
   ARM ID, auth = project managed identity (grant it **Contributor** on the ACS resource).
   Note the connection name.
8. `.env`: `FOUNDRY_CONNECTION_NAME=<connection name>`,
   `TEAMS_RESOURCE_ACCOUNT_ID=<resource account object id GUID, no 28:orgid: prefix>`,
   `DEMO_ALLOWLIST=<your mobile in E.164>`, then `./deploy.sh`.
9. Optional (inbound): Foundry -> agent -> **Channels -> Phone numbers -> Add -> Microsoft
   Teams**, automatic secure delivery. Not required for outbound call jobs.

Propagation of the resource account, number and consent can take hours. Until it completes,
use the browser preview (step D6) - every other part of the system is identical.

## G. Verify and rehearse

```bash
docker compose exec tool-api python -m salesagent.doctor
```
Every configured integration should be **OK**. Then run the demo script in `docs/DEMO.md`
twice, pressing **Reset demo** between runs.

## Troubleshooting

| Symptom | Check |
|---|---|
| Call blocked `DEMO_NOT_ALLOWLISTED` | `DEMO_ALLOWLIST` in `.env` contains the number in E.164 |
| Call blocked `OUTSIDE_CALLING_WINDOW` | `CALLING_WINDOW_*` hours (lead's local time) |
| `PLACE_CALL_FAILED` | Foundry endpoint / agent name / connection name; SP has Foundry User role |
| Call job `failed` with `outbound_connection_unavailable` | ACS connection permissions, consent (F6) |
| Agent says it cannot reach tools | `curl https://sales-api.whyaidata.com/healthz`; API key in the Foundry tool |
| Booking returns `BOOKING_FAILED` | Graph consent for `Calendars.ReadWrite`; rep UPN has a mailbox |
| Cockpit pill grey | the integration's `.env` values / secret file are empty |
| Logs | `docker compose logs -f tool-api worker` |
