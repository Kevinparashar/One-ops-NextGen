# OneOps AI Service — Validation Use Cases

> **Purpose**: Detailed use case specifications to validate the OneOps AI service end-to-end.
> **Scope**: Functional behavior only — service component mapping is a separate exercise.
> **Date**: 2026-03-10

---

## UC-1: Ticket Summary

### Overview
Generate a concise, structured summary of any ticket (incident, service request, change, problem) that captures the essential context in seconds. The summary is available both as a UI feature (button) and through AI agent conversation — both paths must produce identical output.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **UI Feature** | "Summarize" button on ticket detail page |
| **Agent Conversation** | User asks "summarize this ticket" or "give me a summary" in AI chat |

### Functional Requirements

**Input**
- Ticket ID (explicit from button context, inferred from conversation context)
- Data required for summary: title, description, all work notes, comments, status transitions, linked CIs, linked tickets, attachments metadata (not content)

**Processing**
- Check if a cached summary exists and is still fresh (no updates since last generation)
- If stale or missing: generate fresh summary
- For the same ticket state, all entry points must return the same summary

**Output Format**
```
**Status**: Open | Priority: P2 | Assigned: Network Team

**What happened**: User in Building-3 reported intermittent Wi-Fi drops
since Monday morning affecting ~20 users on floor 5.

**Key updates**:
- Mar 7: AP-5F-03 identified as faulty, replacement ordered
- Mar 8: Temporary AP deployed, partial restoration
- Mar 9: Replacement AP received, installation scheduled for Mar 10

**Pending**: AP replacement and validation testing

**Linked**: CHG0045123 (AP firmware upgrade last week) — potential root cause
```

**Freshness Rules**
- Summary is considered fresh if no ticket mutations (new comments, status change, reassignment, linked item changes) occurred since last generation
- Freshness TTL: 0 — purely event-driven, not time-based
- On any ticket mutation, mark summary as stale (do not regenerate eagerly)

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 1.1 | First-time summary on a ticket with 3 comments | Generates fresh summary, includes all 3 comments in "Key updates" |
| 1.2 | Click summary button again (no changes) | Returns cached summary instantly (no LLM call) |
| 1.3 | New comment added, then summary requested | Detects staleness, regenerates, new comment appears in summary |
| 1.4 | Ask agent "summarize this ticket" while viewing INC001 | Agent infers ticket ID from context, returns same summary as button |
| 1.5 | Ask agent "summarize INC001" from home page (no ticket context) | Agent extracts ticket ID from message, fetches and summarizes |
| 1.6 | Summary requested on ticket with 200+ work notes | Summary is still concise (max 300 words), prioritizes recent and significant updates |
| 1.7 | Summary on a resolved ticket with known error link | Includes resolution details and linked known error |
| 1.8 | Two users request summary of the same ticket simultaneously | Only one generation occurs; second request waits and gets the same cached result |
| 1.9 | User without view permission on the ticket | Returns permission error, no summary generated |
| 1.10 | Ticket in a language other than English | Summary generated in the same language as the ticket (or configurable) |

### Edge Cases
- Ticket with only a title and no description or comments → summary states "Insufficient information" with what's available
- Ticket with attachments referenced in comments but not readable → summary notes "attachments referenced but not analyzed"
- Linked ticket is in a different tenant → linked reference shown but not summarized

---

## UC-2: Similar Tickets

### Overview
Find and rank tickets that are semantically similar to a given ticket, helping agents identify patterns, reuse resolutions, and detect duplicates before they're manually flagged.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **UI Feature** | "Find Similar" button on ticket detail page |
| **Agent Conversation** | "Are there similar tickets?" or "Find duplicates" in AI chat |

### Functional Requirements

**Input**
- Source ticket: title, description, category, CI, affected service, location
- Search scope: configurable — last 30/60/90 days, same category only, same service only, or all

**Processing**
- Generate embedding of the source ticket's semantic content (title + description + recent updates)
- Vector search against ticket embeddings index
- Post-filter by: status (prefer resolved for resolution reuse, open for duplicate detection), tenant boundary
- Re-rank by composite score: semantic similarity (0.6) + metadata overlap (0.25) + recency (0.15)

**Output Format**
```
Found 5 similar tickets (showing top 3):

1. **INC004512** (92% match) — Resolved
   "Wi-Fi connectivity issues in Building-3 Floor 4"
   Resolved: AP replacement + firmware rollback
   Common: Same building, same CI category, same symptoms

2. **INC004498** (87% match) — Resolved
   "Intermittent network drops after AP firmware v3.2 update"
   Resolved: Firmware rolled back to v3.1
   Common: Same firmware version, same timeframe

3. **INC004520** (78% match) — Open
   "Building-3 Floor 6 users reporting slow Wi-Fi"
   ⚠️ Potential duplicate — same building, overlapping timeframe
```

**Duplicate Detection Rules**
- If similarity > 90% AND same CI AND open → flag as "Likely Duplicate" with merge suggestion
- If similarity > 85% AND resolved → flag as "Resolution Available"

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 2.1 | Ticket with clear symptoms and known CI | Returns similar tickets ranked by relevance, top result > 80% match |
| 2.2 | Vague ticket "something is broken" | Returns broader matches with lower confidence scores, warns "limited context" |
| 2.3 | Unique/novel ticket (first of its kind) | Returns "No significantly similar tickets found" with < 50% threshold |
| 2.4 | Ticket similar to one in a different tenant | Does NOT return cross-tenant results |
| 2.5 | 3 open tickets are near-duplicates | All three cross-reference each other with "Likely Duplicate" flag |
| 2.6 | Agent asks "find similar to INC001 from last week only" | Respects time-scoped search, limits to 7-day window |
| 2.7 | Similar ticket exists but user lacks permission | Ticket excluded from results silently (no leak) |
| 2.8 | Source ticket is updated after similarity search | Next search reflects updated content (re-embeds if stale) |

---

## UC-3: Knowledge Base Lookup

### Overview
Search the organization's knowledge base to find articles relevant to a ticket or a user's question, returning ranked results with highlighted excerpts showing why each article is relevant.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **UI Feature** | "Suggest KB" button on ticket detail page |
| **Agent Conversation** | "How do I reset my VPN token?" or "Is there a KB for this?" in AI chat |
| **Auto-Suggest** | New ticket created — background search, shows banner if high-confidence match found |

### Functional Requirements

**Input**
- From UI button: ticket title + description + category as search context
- From conversation: user's natural language question (agent reformulates into search query)
- Filters: article status (published only), language, audience (end-user vs. technician)

**Processing**
- Query formulation: extract key concepts, expand abbreviations (VPN → Virtual Private Network)
- Hybrid search: keyword (BM25) + semantic (vector) with configurable weights
- Filter by: published status, not archived, audience match, language
- Extract relevant excerpts (2-3 sentences) from matched articles
- Rank by: relevance score (0.5) + article freshness (0.2) + usage/rating (0.2) + exact keyword match bonus (0.1)

**Output Format**
```
Found 3 relevant knowledge articles:

1. **KB0012345** — "How to Reset Your VPN Token (RSA SecurID)"
   ⭐ 4.8/5 | Updated: Feb 2026 | Views: 1,240
   > "...open the RSA SecurID app → tap the hamburger menu →
   > select 'Reset Token' → follow the on-screen prompts..."
   🏷️ VPN, RSA, Token Reset, Remote Access

2. **KB0012100** — "VPN Troubleshooting Guide"
   ⭐ 4.5/5 | Updated: Jan 2026 | Views: 3,100
   > "...if your token shows 'Next Token Mode', you'll need to
   > enter two consecutive token codes without PIN..."
   🏷️ VPN, Troubleshooting, Connectivity

3. **KB0011890** — "Remote Access Setup for New Employees"
   ⭐ 4.2/5 | Updated: Dec 2025 | Views: 890
   > "...Section 3 covers VPN token provisioning and first-time
   > activation steps..."
   🏷️ Onboarding, VPN, Remote Access
```

**Auto-Suggest Rules**
- Only show auto-suggest banner if top result confidence > 80%
- Do not auto-suggest on tickets already linked to a KB article
- Auto-suggest runs once on ticket creation, not on every update

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 3.1 | Ticket about "VPN not connecting" | Returns VPN troubleshooting articles ranked by relevance |
| 3.2 | User asks agent "how do I map a network drive?" | Agent reformulates query, returns relevant KB articles |
| 3.3 | Query matches no published articles | Returns "No matching articles found. Consider creating one." |
| 3.4 | KB article exists but is in "Draft" status | Excluded from results |
| 3.5 | Multiple articles partially relevant | Returns all with distinct excerpts showing different relevant sections |
| 3.6 | User is end-user, technician-only articles exist | Technician articles excluded from end-user results |
| 3.7 | Ticket in Spanish, KB has Spanish and English articles | Prefers Spanish articles, falls back to English with note |
| 3.8 | New ticket auto-triggers KB search, high match found | Banner appears: "KB0012345 may help resolve this issue" |
| 3.9 | Same KB search repeated within 5 minutes, no KB changes | Returns cached results |
| 3.10 | Article was helpful → user clicks "Link to Ticket" | Article linked to ticket, article usage count incremented |

---

## UC-4: Sentiment Detection

### Overview
Analyze the emotional tone of customer communications on a ticket to detect frustration, urgency, or satisfaction — enabling proactive intervention before escalation.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **Auto-Trigger** | New customer comment/reply added to ticket — sentiment analyzed automatically |
| **Agent Conversation** | "What's the customer sentiment?" or "Is the customer frustrated?" in AI chat |
| **UI Indicator** | Sentiment badge on ticket list and detail page — reads stored result, no new analysis |

### Functional Requirements

**Input**
- Individual comment text (for per-comment analysis)
- Full conversation thread (for overall trajectory analysis)
- Comment metadata: author role (customer vs. agent), timestamp, channel (email/portal/chat)

**Processing**
- Per-comment classification: frustrated / concerned / neutral / satisfied / appreciative
- Confidence score: 0.0 – 1.0
- Key phrases extraction: the specific words/sentences that drove the classification
- Trajectory analysis: sentiment trend over the ticket lifecycle (improving / stable / deteriorating)
- Only analyze customer-authored comments (skip agent/system notes)

**Output Format**

*Per-comment (stored internally):*
```json
{
  "comment_id": "COM-9823",
  "sentiment": "frustrated",
  "confidence": 0.91,
  "key_phrases": [
    "this is the third time I'm reporting this",
    "nobody seems to care",
    "I need this fixed TODAY"
  ],
  "escalation_risk": "high"
}
```

*Ticket-level (shown in UI and to agent):*
```
Sentiment: 😤 Frustrated (high confidence)
Trend: Deteriorating ↘ (was neutral → concerned → frustrated over 3 days)

Key signals:
- "third time reporting" — repeated issue frustration
- "nobody seems to care" — perceived neglect
- "need this fixed TODAY" — urgency escalation

⚠️ Recommendation: Prioritize response. Acknowledge previous contacts.
   Consider manager reach-out.
```

**Alert Rules**
- If sentiment = frustrated AND confidence > 0.85 → trigger "Attention Needed" flag on ticket
- If trajectory = deteriorating over 3+ comments → notify assigned group lead
- If sentiment = appreciative after resolution → candidate for CSAT survey

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 4.1 | Customer writes "Thank you for the quick fix!" | Classified as "appreciative", confidence > 0.9 |
| 4.2 | Customer writes "This is unacceptable, I've been waiting 3 days" | Classified as "frustrated", key phrases extracted, escalation risk = high |
| 4.3 | Customer writes "OK, I'll wait" | Classified as "neutral" or "concerned" — ambiguous, lower confidence |
| 4.4 | Agent adds a work note "Contacted vendor" | Skipped — not a customer comment |
| 4.5 | Three comments: neutral → concerned → frustrated | Trajectory shows "deteriorating", triggers group lead notification |
| 4.6 | Sarcastic comment "Great, another day without email" | Detected as frustrated/sarcastic, not misclassified as positive |
| 4.7 | Comment in non-English language | Sentiment analyzed in original language, classification in English |
| 4.8 | High-volume ticket (50+ comments) | Only customer comments analyzed; trajectory uses last 10 for trend |
| 4.9 | Agent asks "how is the customer feeling about INC001?" | Returns latest sentiment + trajectory in conversational format |
| 4.10 | Customer sentiment improves after resolution | Badge updates to "satisfied", ticket flagged as CSAT survey candidate |

---

## UC-5: Ticket Triage & Route

### Overview
Automatically classify, prioritize, and route incoming tickets to the correct assignment group — reducing manual triage time from minutes to seconds while maintaining accuracy through a confidence-based fallback to human triage.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **Auto-Trigger** | New ticket created (any channel: portal, email, chat, API) |
| **Manual Re-Triage** | Agent clicks "Re-Triage" button on a misrouted ticket |
| **Agent Conversation** | "Where should this ticket go?" or "Re-triage this" in AI chat |

### Functional Requirements

**Input**
- Ticket: title, description, category (if pre-selected), urgency/impact (if provided)
- Context: requester's department, location, VIP status, recent tickets
- CMDB: affected CI (if identified), service map

**Processing Pipeline**
```
Step 1: Classification
  - Category (Hardware/Software/Network/Access/...)
  - Sub-category (Laptop/Desktop/Mobile/Printer/...)
  - Confidence score per classification

Step 2: Priority Calculation
  - Impact assessment (users affected, CI criticality, VIP flag)
  - Urgency assessment (keywords, sentiment, business context)
  - Priority matrix: Impact × Urgency → P1/P2/P3/P4

Step 3: Duplicate Check
  - Quick similarity scan against last 48 hours of open tickets
  - If > 90% match found → flag as potential duplicate, link, pause routing

Step 4: Assignment
  - Match category + sub-category + CI → assignment group
  - Within group: round-robin, least-loaded, or skill-based (configurable)
  - If confidence < threshold → route to "Triage Queue" for human review

Step 5: Enrichment
  - Auto-link CI if identified from description
  - Tag with detected keywords
  - Set initial SLA clock based on priority
```

**Confidence Thresholds**
| Confidence | Action |
|-----------|--------|
| ≥ 0.90 | Auto-assign, no human review |
| 0.70 – 0.89 | Auto-assign with "AI-Triaged" flag, human can override |
| 0.50 – 0.69 | Route to Triage Queue with AI recommendation shown |
| < 0.50 | Route to Triage Queue, no recommendation |

**Output**
```
Triage Result (confidence: 0.92 — auto-assigned):
  Category: Network → Wireless
  Priority: P2 (Impact: Medium — ~20 users, Urgency: High — productivity blocked)
  Assignment: Network Operations Team → John Smith (least loaded)
  CI Linked: AP-5F-03 (Building 3, Floor 5)
  Tags: #wifi #connectivity #building3
  Duplicate Check: No duplicates found
  SLA: 8-hour resolution target started
```

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 5.1 | Clear ticket: "Laptop screen cracked, need replacement" | Category: Hardware → Laptop, Priority: P3, routed to Desktop Support |
| 5.2 | Vague ticket: "Something is wrong with my computer" | Low confidence, routes to Triage Queue with best-guess recommendation |
| 5.3 | VIP submits: "Email not syncing on phone" | VIP flag boosts urgency, Priority: P2 instead of P3 |
| 5.4 | Duplicate: same user, same issue, submitted 2 hours ago | Flagged as duplicate, linked to original, routing paused |
| 5.5 | Ticket mentions specific CI: "Printer on 3rd floor not working" | CI auto-linked from CMDB, routed to Printer Support |
| 5.6 | Email-submitted ticket with poor formatting | Parser extracts key information despite formatting, triages normally |
| 5.7 | Agent clicks "Re-Triage" after adding more details | Re-runs pipeline with enriched data, confidence improves, re-assigns |
| 5.8 | Triage result has 0.75 confidence | Auto-assigns but with "AI-Triaged" flag visible to the assigned agent |
| 5.9 | Category maps to a group with no available agents | Routes to group anyway but triggers "No agents available" alert |
| 5.10 | 100 tickets flood in within 5 minutes (outage scenario) | Detects burst pattern, triages individually but also triggers correlation (see M5 in future) |
| 5.11 | Agent asks "where should this ticket go?" | Shows recommendation in chat without auto-applying; waits for confirmation |

---

## UC-6: Conversational Ticket Creation

### Overview
Enable users to create fully-formed tickets through natural conversation with the AI agent, where the agent asks clarifying questions, progressively builds the ticket, and submits on user confirmation — making ticket creation feel like talking to a help desk, not filling a form.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **Chat Initiation** | User opens AI chat and describes an issue |
| **Incomplete Form Rescue** | User abandons a half-filled form, switches to chat — agent picks up where they left off |
| **Voice/Teams Integration** | User describes issue via Teams bot or voice |

### Conversation Flow

```
Turn 1 — User describes issue:
  "My laptop keeps freezing when I open Excel with large files"

Turn 2 — Agent acknowledges + asks targeted questions:
  "I understand your laptop freezes when working with large Excel files.
   Let me gather a few details:
   1. What laptop model are you using? (I see you have a ThinkPad T14
      on record — is that the one?)
   2. How large are the files roughly? (e.g., MB range)
   3. When did this start happening?"

Turn 3 — User responds (possibly partial):
  "Yes ThinkPad T14. Files are 50-100MB. Started this week."

Turn 4 — Agent fills in remaining gaps:
  "Got it. A couple more questions:
   1. Does it freeze completely (hard freeze, need to reboot) or
      temporarily (hangs for a few seconds then recovers)?
   2. Any recent software updates or changes you're aware of?"

Turn 5 — User responds:
  "Hard freeze, have to hold power button. No updates I know of."

Turn 6 — Agent shows ticket preview:
  "Here's what I've prepared:

   📋 **New Incident**
   Title: Laptop hard freeze when opening large Excel files
   Category: Hardware → Laptop
   Priority: P3 (single user, productivity impact)
   Description: User reports ThinkPad T14 hard-freezes (requires
   forced reboot) when opening Excel files in the 50-100MB range.
   Issue started this week. No known recent updates or changes.
   CI: ThinkPad T14 (Asset: LPT-04521)

   Shall I submit this? You can also ask me to change anything."

Turn 7 — User confirms or adjusts:
  "Make it P2, this is blocking my quarterly report"

Turn 8 — Agent adjusts and submits:
  "Updated to P2. Ticket INC005678 has been created and assigned
   to Desktop Support. You'll receive updates via email.
   Is there anything else I can help with?"
```

### Functional Requirements

**Conversation Intelligence**
- Slot-filling approach: agent maintains a ticket schema and tracks which fields are filled/empty
- Auto-fill from user profile: name, email, department, location, manager, known assets
- Smart questioning: only ask what's missing, don't re-ask what's already known or inferrable
- Allow user to provide information in any order, any format
- Handle corrections mid-conversation: "Actually it's not the ThinkPad, it's my desktop"

**Ticket Fields Managed**
| Field | Source | Required |
|-------|--------|----------|
| Title | Generated from conversation | Yes |
| Description | Synthesized from all user messages | Yes |
| Category / Sub-category | AI-classified from symptoms | Yes |
| Priority | Calculated, user-overridable | Yes |
| CI / Asset | From user profile + conversation | If identifiable |
| Requester | From authenticated session | Yes (auto) |
| Location | From user profile, confirmed in conversation if ambiguous | If relevant |
| Attachments | User can share screenshots/files in chat | Optional |

**Guardrails**
- Maximum 8 turns before showing preview (avoid interrogation feel)
- If user says "just create it" at any point → submit with whatever is available, fill gaps with defaults
- Never create duplicate if user already has an open ticket for the same issue — warn and link instead

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 6.1 | User describes clear issue in first message | Agent asks 2-3 targeted questions, shows preview by turn 4 |
| 6.2 | User gives one-word responses | Agent adapts, asks yes/no questions, doesn't repeat |
| 6.3 | User provides all details upfront in a paragraph | Agent extracts all fields, shows preview immediately (turn 2) |
| 6.4 | User corrects a field mid-conversation | Agent updates the field, acknowledges correction, continues |
| 6.5 | User says "just create it" after first message | Creates ticket with available info + defaults, no further questions |
| 6.6 | User has an open ticket for same issue | Agent detects and says "You already have INC005600 for this. Want to add a comment instead?" |
| 6.7 | User abandons form with 3 fields filled, opens chat | Agent picks up: "I see you started a ticket about X. Let me help you finish it." |
| 6.8 | User wants to create a ticket for someone else | Agent asks for the affected user, creates on their behalf, sets requester correctly |
| 6.9 | User shares a screenshot in chat | Agent acknowledges attachment, attempts to extract info from image if possible |
| 6.10 | User switches topic mid-conversation | Agent parks the first ticket draft, addresses new topic, offers to resume later |
| 6.11 | Ticket created but triage changes the category | Triage runs post-creation, updates are transparent; user sees "Your ticket has been assigned to X" |

---

## UC-7: Resolution Suggestion

### Overview
Provide agents with actionable resolution suggestions for a ticket by analyzing similar resolved tickets, extracting proven resolution steps, checking what's already been tried, and ranking suggestions by relevance and success rate.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **UI Feature** | "Suggest Resolution" button on ticket detail page |
| **Agent Conversation** | "How should I fix this?" or "What's the resolution?" in AI chat |
| **Auto-Suggest** | Ticket assigned to agent, idle for > configured threshold — nudge with suggestions |

### Functional Requirements

**Input**
- Current ticket: title, description, category, CI, all work notes and comments
- Search scope: resolved tickets (last 6 months), KB articles, known errors

**Processing Pipeline**
```
Step 1: Understand Current Ticket
  - Extract symptoms, affected systems, error messages
  - Identify what has already been tried (from work notes)

Step 2: Find Resolution Sources
  - Similar resolved tickets (vector similarity on symptoms)
  - KB articles matching the issue
  - Known error records for the affected CI/service
  - Vendor documentation (if indexed)

Step 3: Extract Resolution Steps
  - From each source, extract the actual resolution actions taken
  - Normalize into step-by-step instructions
  - De-duplicate across sources

Step 4: Filter Already-Tried
  - Compare extracted resolutions against work notes
  - Mark resolutions that overlap with already-tried steps
  - Flag partial overlaps ("You tried X but not the follow-up step Y")

Step 5: Rank and Present
  - Score by: success rate (0.4) + similarity to current ticket (0.3) +
    recency (0.15) + source authority (0.15)
  - Return top 3-5 suggestions
```

**Output Format**
```
3 resolution suggestions for INC005678:

1. 🏆 **Increase Excel memory allocation + update Office** (87% success rate)
   Source: INC004890, INC005012, KB0013400
   Steps:
   a. Check current RAM utilization (Task Manager → Performance)
   b. Clear Excel temp files: %appdata%\Microsoft\Excel\
   c. Increase Excel memory: File → Options → Advanced → set to 2048MB
   d. Update Office to latest build (current: 16.0.17328, latest: 16.0.17726)
   e. Test with the problematic file
   ℹ️ You've already checked RAM (work note Mar 8) — skip step (a)

2. **Replace RAM module** (72% success rate)
   Source: INC004650
   Steps:
   a. Run memory diagnostic (mdsched.exe)
   b. If errors found → schedule RAM replacement
   ⚠️ More invasive — try suggestion 1 first

3. **Re-image laptop** (95% success rate but last resort)
   Source: INC003200
   Steps:
   a. Back up user data
   b. Re-image with standard SOE
   c. Restore user data and test
   ⚠️ Nuclear option — only if suggestions 1 and 2 fail
```

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 7.1 | Common issue with many resolved precedents | Returns 3+ suggestions ranked by success rate |
| 7.2 | Agent already tried the top suggestion (noted in work log) | That suggestion is de-prioritized or marked "Already Tried" |
| 7.3 | Unique issue with no similar resolved tickets | Returns "No similar resolutions found" with fallback to KB search |
| 7.4 | Known error exists for the CI | Known error workaround shown as top suggestion with "Known Error" badge |
| 7.5 | Agent asks "how should I fix this?" in chat | Conversational response with same ranked suggestions |
| 7.6 | Multiple resolution paths with trade-offs | Each path annotated with effort level and invasiveness |
| 7.7 | Resolution involves steps outside agent's permission | Flags steps requiring elevated access or change request |
| 7.8 | Ticket has been open 5 hours with no work notes | Auto-nudge appears: "Here are some suggestions to get started" |
| 7.9 | Agent follows suggestion and resolves ticket | Feedback loop: resolution linked to suggestion, success rate updated |
| 7.10 | Agent marks suggestion as "not helpful" | Feedback recorded, affects future ranking for similar tickets |

---

## UC-8: Catalog Item Fulfillment with AI Workflow

### Overview
Orchestrate the end-to-end fulfillment of a service catalog request using AI to decompose the request into tasks, execute automated steps, handle failures gracefully, track progress against SLAs, and keep the requester informed — turning a multi-day, multi-team process into a coordinated, partially-automated workflow.

### Entry Points

| Entry Point | Trigger |
|-------------|---------|
| **Catalog Submission** | User submits a catalog request (e.g., "New Employee Onboarding") via portal |
| **Agent Conversation** | "Onboard new employee John Smith starting March 15" in AI chat |

### Example: New Employee Onboarding

**Request Data**
```
Catalog Item: New Employee Onboarding
Employee: John Smith
Start Date: March 15, 2026
Department: Engineering
Role: Senior Developer
Manager: Jane Doe
Location: Building 2, Floor 3
```

### Orchestration Flow

```
Phase 1: Decomposition (AI-driven)
┌──────────────────────────────────────────────────┐
│ AI analyzes catalog item template + request data  │
│ Generates fulfillment plan:                       │
│                                                   │
│ ├─ Parallel Group A (no dependencies):            │
│ │  ├─ Task 1: Create AD account (automated)       │
│ │  ├─ Task 2: Create email/calendar (automated)   │
│ │  └─ Task 3: Order laptop (manual → procurement) │
│ │                                                  │
│ ├─ Parallel Group B (depends on AD account):      │
│ │  ├─ Task 4: Provision VPN access (automated)    │
│ │  ├─ Task 5: Add to GitHub org (automated)       │
│ │  └─ Task 6: License: IDE + Jira (automated)     │
│ │                                                  │
│ ├─ Sequential (depends on all above):             │
│ │  ├─ Task 7: Create badge request (manual → sec) │
│ │  └─ Task 8: Assign desk/workspace (manual → FM) │
│ │                                                  │
│ └─ Final:                                          │
│    └─ Task 9: Send welcome kit email (automated)   │
└──────────────────────────────────────────────────┘

Phase 2: Execution
  - Automated tasks execute via integration endpoints
  - Manual tasks dispatched to respective teams with SLAs
  - AI monitors progress, handles events

Phase 3: Exception Handling
  - On failure: AI evaluates severity and executes fallback
  - Example: "Laptop model X out of stock"
    → AI checks alternatives in inventory
    → Substitutes equivalent model
    → Notifies requester and manager of substitution
    → Adjusts timeline if needed

Phase 4: Completion
  - All tasks done → AI generates onboarding summary
  - Welcome email sent with all provisioned access details
  - Catalog request marked "Fulfilled"
```

### Functional Requirements

**Decomposition Intelligence**
- Parse catalog item template to identify fulfillment steps
- Determine dependencies between steps (what can run in parallel)
- Identify which steps are automatable vs. manual
- Estimate duration per step based on historical data
- Generate overall fulfillment timeline

**Execution Management**
- Execute automated tasks via integration APIs (AD, email, licensing platforms)
- Create and assign manual tasks to appropriate teams
- Track individual task SLAs and overall request SLA
- Re-calculate timeline dynamically as tasks complete or delay

**Exception Handling Rules**
| Exception | AI Response |
|-----------|-------------|
| Automated task fails (transient) | Retry up to 3 times with exponential backoff |
| Automated task fails (permanent) | Create manual fallback task, notify team, adjust timeline |
| Resource unavailable (e.g., laptop out of stock) | Search alternatives, propose substitution, await approval |
| Manual task breaching SLA | Escalate to team lead, notify requester with updated ETA |
| Dependency blocked (upstream task failed) | Pause dependent tasks, surface blocker, suggest unblock path |
| Requester cancels mid-fulfillment | Initiate rollback for completed automated tasks, cancel pending tasks |

**Requester Communication**
```
Milestone notifications sent at:
  ✅ Request received, fulfillment plan created
  ✅ AD account created (you can now sign in)
  ✅ Email provisioned (john.smith@company.com)
  ⚠️ Laptop model substituted (ThinkPad T14 → T14s, same specs)
  ✅ All access provisioned
  ✅ Desk assigned: Building 2, Floor 3, Desk 42
  🎉 Onboarding complete! Welcome kit sent to your email.
```

### Validation Scenarios

| # | Scenario | Expected Behavior |
|---|----------|-------------------|
| 8.1 | Standard onboarding, all tasks succeed | All 9 tasks complete, welcome email sent, request fulfilled within SLA |
| 8.2 | AD account creation fails (API timeout) | Retries 3 times, succeeds on retry 2, dependent tasks wait then resume |
| 8.3 | Laptop out of stock | AI finds alternative, sends substitution approval to manager, proceeds on approval |
| 8.4 | Manual task (badge) takes longer than SLA | Escalation to security team lead at 80% SLA, requester notified at 100% SLA breach |
| 8.5 | User cancels request after 4 tasks are done | Automated provisioning rolled back (AD disabled, email deprovisioned), manual tasks cancelled with notes |
| 8.6 | Requester asks agent "What's the status of my onboarding request?" | Agent returns real-time status: 6/9 tasks complete, 2 in progress, 1 pending, ETA |
| 8.7 | Two onboarding requests submitted for same person | Duplicate detected, second request blocked with link to first |
| 8.8 | Catalog item has no template (first-time item) | AI generates best-guess plan from item description, routes to catalog owner for approval before execution |
| 8.9 | Integration endpoint is down for 2+ hours | Affected tasks marked "Blocked — Integration Down", unrelated tasks continue, ops team notified |
| 8.10 | Partial rollback: email created but GitHub add failed | Email kept (useful), GitHub retried, no unnecessary rollback of successful steps |
| 8.11 | Complex request: 20+ tasks, mixed auto/manual | Orchestration handles correctly, progress dashboard shows Gantt-like view |
| 8.12 | Manager asks "status of all onboarding requests this week?" | Agent aggregates across requests, shows summary table |

---

## Cross-Cutting Validation Notes

These apply across all use cases and will be mapped to specific service components in the next phase:

1. **Single capability, multiple entry points** — Each AI feature is one capability regardless of how it is triggered (UI button, agent conversation, auto-trigger, API). All entry points must produce identical results for the same input. When a user triggers a feature via chat, the agent must invoke the same underlying capability that the UI button uses — not a separate implementation. This ensures consistency in output, caching, permissions, and audit logging across all entry points.
2. **Permission enforcement** — Every AI operation must respect the user's RBAC permissions; no data leakage
3. **Tenant isolation** — Multi-tenant queries must never cross tenant boundaries
4. **Audit trail** — Every AI action (generation, classification, assignment) is logged with inputs, outputs, model used, and latency
5. **Graceful degradation** — If the AI service is down or slow, the platform must remain functional with features gracefully disabled
6. **Feedback loop** — Users can rate AI outputs (helpful/not helpful), feeding back into model improvement
7. **Idempotency** — Retrying the same AI request (e.g., network timeout) must not create duplicates or side effects
8. **Cost awareness** — Each AI call's token usage and cost is trackable per tenant, per use case
