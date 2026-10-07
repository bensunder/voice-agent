# Power Automate: rep notification card

The worker posts a briefing to this flow when a lead is qualified, a meeting is booked, or a
live transfer is approved. The flow posts an adaptive card to the rep in Teams.

1. **make.powerautomate.com** -> switch to your **developer environment** -> **Create ->
   Instant cloud flow** -> skip -> add trigger **When an HTTP request is received**.
   - *Who can trigger the flow*: **Anyone** (the URL contains a signature; it is stored as a
     secret on the server).
   - *Request Body JSON Schema*: paste `trigger-schema.json`.
2. Add action **Microsoft Teams -> Post card in a chat or channel**:
   - Post as: **Flow bot**
   - Post in: **Chat with Flow bot**
   - Recipient: `rep_upn` from the dynamic content (or your own address for the demo)
   - Adaptive Card: paste `rep-card.json`. The `@{triggerBody()?['...']}` expressions bind the
     payload fields.
3. Add action **Response**: status code `202`.
4. **Save**. Copy the trigger's **HTTP URL** into `secrets/power_automate_webhook_url` on the
   server and run `docker compose up -d`.

Test without a call:
```bash
curl -X POST "$(cat secrets/power_automate_webhook_url)" -H 'Content-Type: application/json' \
  -d '{"title":"Test card","contact":"John Rivera","company":"Contoso","phone":"+18015550123","score":87,"band":"HOT","lines":600,"carrier":"Verizon","renewal":"4 months","role":"decision maker","pain_points":"cost","estimated_value":"$734,400","meeting":"Thursday 10:30 AM","summary":"Test","next_action":"Prepare proposal","reason_codes":"LINES_500PLUS","briefing_url":"https://cockpit.whyaidata.com","rep_upn":"you@benwhyaidata.onmicrosoft.com"}'
```
