# Demo script (about 8 minutes)

**Before:** cockpit open full-screen, Teams open on a second screen (rep chat + calendar), your
phone on the allowlist, **Reset demo** pressed, `doctor` all OK.

## 1. The problem (30s)
"A company has 50,000 leads. Reps can't call them all, so most go cold. This is an AI employee on
the company's own Teams Phone number that calls, qualifies, books and hands off, with every action
recorded in Power Platform."

## 2. Capture the lead (30s)
Fill the form as **John Rivera, Contoso Logistics**, your mobile, tick consent, **Capture lead**.
Point out the **compliance gate**: consent, opt-out list, calling window, demo allowlist.

## 3. The call (3 min)
Press **Call via Teams Phone**. Your phone rings from the Teams service number.
(If the Teams number is still provisioning, press **Browser preview** and talk in the Foundry
preview instead: same agent, same tools, same cockpit.)

Play John:
- "Sure." -> AI disclosure + recording notice (first sentence, always).
- "About 600 lines." / "Verizon." / "Renews in about four months." / "I make the call." /
  "Cost, and coverage at our warehouses." / "Probably this quarter."
- Watch the cockpit: slots fill in with John's own words as evidence; the score climbs to
  **HOT**, the **estimated opportunity** appears.
- Ask "Am I talking to a robot?" -> it answers truthfully.
- Accept a meeting time -> **Teams meeting booked (Graph)** appears; show it on the rep's
  calendar in Teams with the join link.

## 4. The hand-off (1 min)
- Teams chat: the **Power Automate card** with score, lines, carrier, renewal, value, summary and
  next action.
- Dataverse: the **AI Call Qualification** row (make.powerapps.com -> Tables).

## 5. Why it's real (2 min)
- **Score and dollar value are deterministic** (versioned rubric + price book); the LLM only
  extracts facts, each with its evidence quote.
- **Every action is audited** (right-hand timeline) and **delivered through an outbox** with
  retries.
- **Safety:** say "please stop calling me" on a second run -> instant opt-out, future calls
  blocked by the gate.
- **Scale path:** Foundry call jobs with retry policies, ACS concurrency, pacing by rep capacity.

## Fallbacks
| If | Do |
|---|---|
| Teams number not ready | Browser preview (step 3) |
| Graph/Power Automate not ready | Cockpit shows the booking and briefing; show the integration pills |
| Network failure | Recorded backup video |
