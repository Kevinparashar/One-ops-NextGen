"""Deterministic ITSM dev/test data generator.

Grows `data/itsm/*.json` to development-grade volume **without discarding the
existing handcrafted records** — they carry realistic narratives and are kept
verbatim. New records are appended up to per-table targets.

Properties:
  * Deterministic — `random.seed` fixed, so re-running is reproducible.
  * Idempotent — a table already at/above target gets zero new rows.
  * Referentially consistent — every generated reference (user, CI, problem,
    change, catalog item) resolves to a real row **in the same tenant**.
  * Generated in dependency order; problem/KB cross-references back-filled.

Run:  .venv/bin/python database/seed/generate_itsm_data.py
A final validation pass fails loudly if any reference dangles.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

# Repeated literals → constants (sonar S1192).
_CONTAINER_PLATFORM = "Container Platform"
_CORE_DATABASE = "Core Database"
_ITSM_PLATFORM = "ITSM Platform"
_KNOWLEDGE_BASE = "Knowledge Base"
_MAIL_SERVICE = "Mail Service"

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "itsm"   # database/seed/ -> repo root
SEED = 20260521
random.seed(SEED)

# Per-table target totals (existing handcrafted + generated). Catalog and
# onboarding are reference tables — kept realistic, not padded to 100.
TARGETS = {
    "sys_user": 120, "cmdb_ci": 120, "asset": 120, "incident": 160,
    "problem": 100, "change": 110, "request": 130, "kb_knowledge": 110,
    "catalog_item": 30, "onboarding_template": 12,
}

TENANTS = ["T001", "T002", "T003"]
TENANT_WEIGHTS = [0.55, 0.27, 0.18]

TENANT_META = {
    "T001": {"domain": "northwind.example.com",
             "locations": ["Mumbai", "Bangalore HQ", "Ahmedabad", "Mumbai-DC"],
             "groups": ["GRP-NETOPS", "GRP-APPS", "GRP-DBA", "GRP-PLATFORM",
                        "GRP-SECOPS", "GRP-CMDB", "GRP-KNOWLEDGE", "GRP-ASSET",
                        "GRP-SERVICEDESK"]},
    "T002": {"domain": "harborlight.example.com",
             "locations": ["Hong Kong", "Tokyo", "Frankfurt-DC", "HongKong-DC"],
             "groups": ["GRP-T002-APPS", "GRP-T002-INFRA", "GRP-T002-NETOPS"]},
    "T003": {"domain": "techventures.com",
             "locations": ["Shanghai", "Lagos", "New York", "DC2"],
             "groups": ["GRP-T003-PLATFORM", "GRP-T003-ENG"]},
}

FIRST = ["Aarav", "Diya", "Vikram", "Ananya", "Rohan", "Priya", "Karan", "Meera",
         "Wei", "Mei", "Jun", "Ling", "Hiro", "Yuki", "Aisha", "Tunde", "Ngozi",
         "Kwame", "Liam", "Emma", "Noah", "Olivia", "Lucas", "Sofia", "Rui",
         "Chen", "Fatima", "Omar", "Sven", "Elena"]
LAST = ["Sharma", "Patel", "Iyer", "Nair", "Reddy", "Gupta", "Mehta", "Rao",
        "Wong", "Chan", "Lim", "Tan", "Sato", "Kimura", "Okonkwo", "Adeyemi",
        "Mensah", "Cohen", "Smith", "Brown", "Mueller", "Rossi", "Zhang", "Liu",
        "Khan", "Ahmed", "Larsson", "Novak"]

ROLES = ["employee", "viewer", "service_desk_agent", "network_engineer",
         "application_support", "database_admin", "security_engineer",
         "cmdb_admin", "knowledge_manager", "problem_manager", "change_manager",
         "asset_manager", "cloud_engineer", "it_director"]
DEPTS = ["IT Operations", "Engineering", "Infrastructure", "Applications",
         "Security", "Platform", "Knowledge", "ITSM", "Configuration",
         "Finance", "HR", "Sales", "Marketing", "Legal", "Compliance"]

CATEGORIES = ["network", "application", "database", "email", "endpoint",
              "security", "platform", "cmdb", "integration", "itsm", "knowledge"]

# category -> (title, description, subcategory, service_name)
INCIDENT_TPL = {
    "network": [("VPN tunnel drops intermittently", "User reports the VPN session resets repeatedly, interrupting work.", "vpn", "Corporate VPN"),
                ("Office Wi-Fi unreachable on one floor", "Access points on a floor stopped responding; users cannot connect.", "wifi", "Campus Wi-Fi"),
                ("High packet loss to data centre", "Latency and packet loss spike on the DC uplink during peak hours.", "routing", "DC Connectivity")],
    "application": [("CRM page returns 500 errors", "The CRM dashboard intermittently fails to load with a server error.", "crm", "CRM Platform"),
                    ("Login fails after deployment", "Users cannot sign in following the latest application release.", "auth", "Identity Service"),
                    ("Report export times out", "Large report exports never complete and time out.", "reporting", "Analytics App")],
    "database": [("Query latency degraded on primary", "Read queries are slow; the primary database CPU is saturated.", "performance", _CORE_DATABASE),
                 ("Replication lag on read replica", "The read replica is minutes behind the primary.", "replication", _CORE_DATABASE),
                 ("Connection pool exhausted", "The application exhausts the DB connection pool under load.", "connections", _CORE_DATABASE)],
    "email": [("Outbound mail delayed", "Outgoing email is queued for hours before delivery.", "smtp", _MAIL_SERVICE),
              ("Phishing reports spike", "Multiple users report a suspicious email campaign.", "spam", _MAIL_SERVICE),
              ("Calendar invites not syncing", "Calendar invitations fail to propagate to attendees.", "calendar", _MAIL_SERVICE)],
    "endpoint": [("Laptop fails to boot after update", "A laptop will not boot following a managed OS update.", "os", "Endpoint Management"),
                 ("Disk encryption prompt loops", "The endpoint is stuck on the disk-encryption prompt.", "encryption", "Endpoint Management"),
                 ("Antivirus agent not reporting", "The endpoint antivirus agent stopped reporting to the console.", "av", "Endpoint Security")],
    "security": [("Suspicious login from new geo", "An account shows a sign-in from an unexpected location.", "iam", "Identity Security"),
                 ("Expired TLS certificate on service", "A public endpoint serves an expired TLS certificate.", "tls", "PKI"),
                 ("Privileged access review overdue", "A scheduled privileged-access review has not been completed.", "access", "IAM")],
    "platform": [("Kubernetes pods in CrashLoopBackOff", "Several pods restart continuously after a config change.", "kubernetes", _CONTAINER_PLATFORM),
                 ("Autoscaler not adding nodes", "The cluster autoscaler fails to add nodes under load.", "autoscaling", _CONTAINER_PLATFORM),
                 ("Ingress controller returns 502", "The ingress controller intermittently returns 502 errors.", "ingress", _CONTAINER_PLATFORM)],
    "cmdb": [("CI relationships out of date", "CMDB relationships do not match the deployed topology.", "data-quality", "CMDB"),
             ("Duplicate CI records detected", "Discovery created duplicate CI entries for one host.", "discovery", "CMDB"),
             ("Discovery scan missing a subnet", "A subnet is absent from the latest discovery scan.", "discovery", "CMDB")],
    "integration": [("Webhook deliveries failing", "Outbound webhooks return errors and are not retried.", "webhook", "Integration Hub"),
                    ("API gateway rate-limiting internal calls", "Internal service calls hit the public rate limit.", "gateway", "API Gateway"),
                    ("Scheduled sync job stuck", "A nightly data-sync job is stuck and never completes.", "etl", "Integration Hub")],
    "itsm": [("Ticket notifications not sent", "Assignment notifications are not delivered to agents.", "notifications", _ITSM_PLATFORM),
             ("SLA timers not pausing", "SLA timers do not pause when a ticket is on hold.", "sla", _ITSM_PLATFORM),
             ("Approval step skipped on request", "A request advanced past an approval step incorrectly.", "workflow", _ITSM_PLATFORM)],
    "knowledge": [("KB search returns stale results", "Knowledge search surfaces retired articles.", "search", _KNOWLEDGE_BASE),
                  ("Article images not loading", "Images embedded in KB articles fail to render.", "content", _KNOWLEDGE_BASE),
                  ("KB feedback widget broken", "The helpful/not-helpful widget does not record votes.", "feedback", _KNOWLEDGE_BASE)],
}

PROBLEM_TPL = {
    c: [(f"Recurring {c} degradation under load",
         f"Repeated {c} incidents share a common failure pattern during peak load.",
         f"Resource contention in the {c} layer not bounded by current limits.",
         f"Apply conservative limits and shed non-critical {c} load.")]
    for c in CATEGORIES
}

KB_TPL = {
    "network": [("Resolve VPN disconnects when roaming", "Fix VPN tunnel drops during AP roaming.", "Update the VPN client profile to the latest version and enable persistent keepalive. Confirm the firewall keepalive interval matches the client. Reconnect and verify the tunnel is stable across roaming events.", ["vpn", "wifi", "roaming"]),
                ("Diagnose Wi-Fi dead zones", "Steps to diagnose and fix Wi-Fi coverage gaps.", "Map signal strength per floor, check AP placement and channel overlap, and reseat or replace unresponsive access points. Validate coverage after changes.", ["wifi", "coverage", "ap"])],
    "application": [("Recover from a failed deployment", "Roll back and recover after a bad release.", "Identify the failing release, roll back to the last known-good version, clear caches, and re-run smoke checks. Communicate status to affected users.", ["deployment", "rollback"]),
                    ("Fix CRM 500 errors", "Triage and resolve CRM server errors.", "Check application logs for the failing request, verify database connectivity, and restart unhealthy app instances. Confirm the dashboard loads.", ["crm", "errors"])],
    "database": [("Reduce database query latency", "Tune slow queries on the primary database.", "Identify slow queries from pg_stat_statements, add or correct indexes, and review connection-pool sizing. Re-measure latency after each change.", ["database", "performance"]),
                 ("Handle replication lag", "Bring a lagging read replica back in sync.", "Check replica I/O and CPU, pause heavy read traffic, and let the replica catch up. Investigate long-running transactions on the primary.", ["database", "replication"])],
    "email": [("Clear an outbound mail queue", "Resolve delayed outgoing email.", "Inspect the mail queue, identify the blocking destination, and verify DNS and reputation. Release the queue once delivery resumes.", ["email", "smtp"])],
    "endpoint": [("Repair an endpoint that fails to boot", "Recover a laptop stuck after an update.", "Boot into recovery, roll back the failed update, and verify disk encryption state. Re-enrol the device if needed.", ["endpoint", "boot"])],
    "security": [("Respond to a suspicious login", "Contain and review an anomalous sign-in.", "Lock the account, review recent sessions and tokens, force a credential reset, and enable step-up MFA. Document the timeline.", ["security", "iam"]),
                 ("Replace an expired TLS certificate", "Restore service after a certificate expiry.", "Issue a new certificate, deploy it to all endpoints, and reload services. Verify the chain and add an expiry alert.", ["security", "tls"])],
    "platform": [("Recover pods from CrashLoopBackOff", "Fix continuously restarting Kubernetes pods.", "Inspect pod logs and events, correct the failing config or resource limits, and roll the deployment. Confirm pods reach a ready state.", ["kubernetes", "platform"])],
    "cmdb": [("Clean up duplicate CI records", "Merge duplicate configuration items.", "Identify duplicates from discovery, choose the authoritative CI, re-point relationships, and retire the duplicate. Re-run discovery to confirm.", ["cmdb", "data-quality"])],
    "integration": [("Recover failed webhook deliveries", "Restore outbound webhook delivery.", "Check the endpoint health, inspect failed payloads, and replay the dead-letter queue. Add retry with backoff.", ["integration", "webhook"])],
    "itsm": [("Fix missing ticket notifications", "Restore assignment notifications.", "Verify the notification channel configuration, check the outbound queue, and re-test assignment. Confirm agents receive alerts.", ["itsm", "notifications"])],
    "knowledge": [("Improve knowledge search relevance", "Tune KB search to surface current articles.", "Re-index the knowledge base, exclude retired articles, and review tag coverage. Validate search results for common queries.", ["knowledge", "search"])],
}

CI_TYPES = ["server", "database", "application", "network", "endpoint",
            "platform", "security", "itsm"]
ASSET_HW = [("ThinkPad T14", "Lenovo"), ("MacBook Pro 14", "Apple"),
            ("Dell Latitude 7440", "Dell"), ("PA-3220 Firewall", "PaloAlto"),
            ("Catalyst 9300 Switch", "Cisco"), ("PowerEdge R760", "Dell")]
ASSET_SW = [("Office 365 E3", "Microsoft"), ("Kong Gateway Enterprise 3.4", "Kong"),
            ("Datadog Pro", "Datadog"), ("Jira Software", "Atlassian"),
            ("CrowdStrike Falcon", "CrowdStrike")]


def base_dt() -> datetime:
    return datetime(2026, 1, 1) + timedelta(
        days=random.randint(0, 134), hours=random.randint(0, 23),
        minutes=random.choice([0, 15, 30, 45]))


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pick_tenant() -> str:
    return random.choices(TENANTS, weights=TENANT_WEIGHTS, k=1)[0]


def load(name: str) -> list[dict]:
    return json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def save(name: str, rows: list[dict]) -> None:
    (DATA_DIR / f"{name}.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def next_numeric(rows: list[dict], key: str) -> int:
    """A collision-free start number for new ids: next 10k block after the
    current max numeric suffix."""
    mx = 0
    for r in rows:
        digits = "".join(ch for ch in str(r.get(key, "")) if ch.isdigit())
        if digits:
            mx = max(mx, int(digits))
    return ((mx // 10_000) + 1) * 10_000 + 1


def by_tenant(rows: list[dict], tenant: str, key: str) -> list[str]:
    return [r[key] for r in rows if r.get("tenant_id") == tenant]


# ── generators ───────────────────────────────────────────────────────────


def gen_users(rows: list[dict]) -> list[dict]:
    n = next_numeric(rows, "user_id")
    need = TARGETS["sys_user"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        fn, ln = random.choice(FIRST), random.choice(LAST)
        uid = f"USR{n + i:05d}"
        existing = by_tenant(rows, t, "user_id")
        rows.append({
            "tenant_id": t, "user_id": uid,
            "name": f"{fn} {ln}",
            "email": f"{fn}.{ln}.{n + i}@{TENANT_META[t]['domain']}".lower(),
            "role": random.choice(ROLES), "department": random.choice(DEPTS),
            "location": random.choice(TENANT_META[t]["locations"]),
            "manager_id": random.choice(existing) if existing else None,
            "vip": random.random() < 0.08, "locale": "en", "is_active": True,
        })
    return rows


def gen_cis(rows: list[dict], users: list[dict]) -> list[dict]:
    n = next_numeric(rows, "ci_id")
    need = TARGETS["cmdb_ci"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        ct = random.choice(CI_TYPES)
        cid = f"CI{n + i:07d}"
        peers = by_tenant(rows, t, "ci_id")
        owners = by_tenant(users, t, "user_id")
        rels = []
        if peers:
            rels.append({"type": random.choice(["depends_on", "runs_on", "connected_to"]),
                         "target_ci_id": random.choice(peers)})
        rows.append({
            "tenant_id": t, "ci_id": cid,
            "ci_name": f"{ct}-{t.lower()}-{n + i:04d}", "ci_type": ct,
            "environment": random.choice(["production", "production", "development"]),
            "status": "active", "owner": random.choice(owners) if owners else None,
            "location": random.choice(TENANT_META[t]["locations"]),
            "criticality": random.choice(["low", "medium", "high", "critical"]),
            "relationships": rels,
            "attributes": {"vendor": random.choice(["Cisco", "Dell", "AWS", "Kong", "PaloAlto"]),
                           "ip_address": f"10.{random.randint(1,250)}.{random.randint(0,250)}.{random.randint(2,250)}"},
        })
    return rows


def gen_assets(rows: list[dict], users: list[dict], cis: list[dict]) -> list[dict]:
    n = next_numeric(rows, "asset_id")
    need = TARGETS["asset"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        hw = random.random() < 0.65
        model, vendor = random.choice(ASSET_HW if hw else ASSET_SW)
        owners = by_tenant(users, t, "user_id")
        peers = by_tenant(cis, t, "ci_id")
        rows.append({
            "tenant_id": t, "asset_id": f"AST{n + i:07d}",
            "asset_name": f"{model} #{n + i:04d}",
            "asset_class": "hardware" if hw else "software",
            "subtype": "laptop" if hw else "subscription",
            "model": model, "vendor": vendor,
            "serial_number": f"SN-{t}-{n + i:06d}",
            "assigned_to": random.choice(owners) if owners and random.random() < 0.8 else None,
            "linked_ci": random.choice(peers) if peers and random.random() < 0.6 else None,
            "location": random.choice(TENANT_META[t]["locations"]), "status": "in_use",
            "purchase_date": f"202{random.randint(3,5)}-{random.randint(1,12):02d}-01",
            "warranty_expiry": f"2027-{random.randint(1,12):02d}-01",
        })
    return rows


def gen_problems(rows: list[dict], users: list[dict]) -> list[dict]:
    n = next_numeric(rows, "problem_id")
    need = TARGETS["problem"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        c = random.choice(CATEGORIES)
        title, desc, root, work = PROBLEM_TPL[c][0]
        owners = by_tenant(users, t, "user_id")
        dt = base_dt()
        ke = random.random() < 0.4
        rows.append({
            "tenant_id": t, "problem_id": f"PBM{n + i:07d}",
            "title": f"{title} ({c}-{n + i:04d})", "description": desc,
            "status": random.choice(["investigating", "root_cause_identified",
                                      "known_error", "resolved"]),
            "priority": random.choice(["P1", "P2", "P3"]), "category": c,
            "root_cause": root, "workaround": work, "known_error": ke,
            "related_incidents": [], "related_changes": [],
            "owner": random.choice(owners) if owners else None,
            "created_at": iso(dt), "updated_at": iso(dt + timedelta(days=random.randint(1, 20))),
        })
    return rows


def gen_changes(rows: list[dict], users: list[dict], cis: list[dict],
                problems: list[dict]) -> list[dict]:
    n = next_numeric(rows, "change_id")
    need = TARGETS["change"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        users_t = by_tenant(users, t, "user_id")
        cis_t = by_tenant(cis, t, "ci_id")
        probs_t = by_tenant(problems, t, "problem_id")
        dt = base_dt()
        rel_prob = random.choice(probs_t) if probs_t and random.random() < 0.4 else None
        rows.append({
            "tenant_id": t, "change_id": f"CHG{n + i:07d}",
            "title": f"Planned change {n + i:04d} for {t}",
            "description": "Scheduled maintenance change to address a known issue.",
            "state": random.choice(["scheduled", "in_progress", "closed"]),
            "change_type": random.choice(["normal", "standard", "emergency"]),
            "risk_level": random.choice(["low", "medium", "high"]),
            "impact": random.choice(["low", "medium", "high"]),
            "approval_status": "approved",
            "approved_by": random.sample(users_t, min(len(users_t), 1)) if users_t else [],
            "requested_by": random.choice(users_t) if users_t else None,
            "assigned_to": random.choice(users_t) if users_t else None,
            "assignment_group": random.choice(TENANT_META[t]["groups"]),
            "affected_ci": random.sample(cis_t, min(len(cis_t), random.randint(1, 2))) if cis_t else [],
            "related_problem": rel_prob,
            "planned_start": iso(dt), "planned_end": iso(dt + timedelta(hours=2)),
            "actual_start": iso(dt + timedelta(minutes=5)), "actual_end": None,
            "created_at": iso(dt - timedelta(hours=4)), "updated_at": iso(dt + timedelta(minutes=5)),
        })
    return rows


def gen_incidents(rows: list[dict], users: list[dict], cis: list[dict],
                  problems: list[dict], changes: list[dict]) -> list[dict]:
    n = next_numeric(rows, "incident_id")
    need = TARGETS["incident"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        c = random.choice(CATEGORIES)
        title, desc, subcat, svc = random.choice(INCIDENT_TPL[c])
        users_t = by_tenant(users, t, "user_id")
        cis_t = by_tenant(cis, t, "ci_id")
        probs_t = by_tenant(problems, t, "problem_id")
        chgs_t = by_tenant(changes, t, "change_id")
        dt = base_dt()
        status = random.choice(["open", "in_progress", "resolved"])
        prio = random.choice(["P1", "P2", "P3", "P4"])
        notes = [{"note_id": f"WN-{n+i}-{j}", "author": random.choice(users_t) if users_t else None,
                  "author_role": "agent", "is_public": False,
                  "timestamp": iso(dt + timedelta(hours=j + 1)),
                  "text": f"Investigation update {j + 1}: continuing triage on the {c} issue."}
                 for j in range(random.randint(0, 3))]
        rows.append({
            "tenant_id": t, "incident_id": f"INC{n + i:07d}",
            "title": f"{title} ({n + i:04d})", "description": desc, "status": status,
            "priority": prio, "severity": random.choice(["low", "medium", "high", "critical"]),
            "impact": random.choice(["low", "medium", "high"]),
            "urgency": random.choice(["low", "medium", "high"]),
            "category": c, "subcategory": subcat, "service_name": svc,
            "reported_by": random.choice(users_t) if users_t else None,
            "assigned_to": random.choice(users_t) if users_t else None,
            "assignment_group": random.choice(TENANT_META[t]["groups"]),
            "ci_id": random.choice(cis_t) if cis_t else None,
            "linked_ci_ids": random.sample(cis_t, min(len(cis_t), random.randint(1, 2))) if cis_t else [],
            "related_problem": random.choice(probs_t) if probs_t and random.random() < 0.35 else None,
            "related_change": random.choice(chgs_t) if chgs_t and random.random() < 0.2 else None,
            "attachments": [], "work_notes": notes, "comments": [],
            "sla_due": iso(dt + timedelta(days=2)),
            "sla_breached": random.random() < 0.15,
            "created_at": iso(dt),
            "updated_at": iso(dt + timedelta(hours=random.randint(1, 48))),
            "resolved_at": iso(dt + timedelta(hours=random.randint(4, 72))) if status == "resolved" else None,
        })
    return rows


def gen_requests(rows: list[dict], users: list[dict], cis: list[dict],
                 catalog: list[dict]) -> list[dict]:
    n = next_numeric(rows, "request_id")
    need = TARGETS["request"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        users_t = by_tenant(users, t, "user_id")
        cis_t = by_tenant(cis, t, "ci_id")
        cat_t = by_tenant(catalog, t, "catalog_item_id")
        dt = base_dt()
        status = random.choice(["open", "approved", "in_progress", "fulfilled"])
        rows.append({
            "tenant_id": t, "request_id": f"SR{n + i:07d}",
            "title": f"Service request {n + i:04d}",
            "description": "User-submitted service request awaiting fulfilment.",
            "status": status,
            "stage": random.choice(["approval", "fulfillment", "procurement", "closed"]),
            "priority": random.choice(["P2", "P3", "P4"]),
            "category": random.choice(CATEGORIES + ["hardware", "software", "access", "onboarding"]),
            "catalog_item_id": random.choice(cat_t) if cat_t and random.random() < 0.6 else None,
            "requested_for": random.choice(users_t) if users_t else None,
            "requested_by": random.choice(users_t) if users_t else None,
            "approved_by": random.sample(users_t, min(len(users_t), 1)) if users_t else [],
            "assigned_to": random.choice(users_t) if users_t else None,
            "assignment_group": random.choice(TENANT_META[t]["groups"]),
            "ci_id": random.choice(cis_t) if cis_t and random.random() < 0.4 else None,
            "sla_due": iso(dt + timedelta(days=3)), "sla_breached": random.random() < 0.12,
            "comments": [], "created_at": iso(dt),
            "updated_at": iso(dt + timedelta(hours=random.randint(1, 60))),
            "fulfilled_at": iso(dt + timedelta(days=2)) if status == "fulfilled" else None,
        })
    return rows


def gen_kb(rows: list[dict], users: list[dict], cis: list[dict]) -> list[dict]:
    n = next_numeric(rows, "kb_id")
    need = TARGETS["kb_knowledge"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        c = random.choice(list(KB_TPL))
        title, summary, content, tags = random.choice(KB_TPL[c])
        users_t = by_tenant(users, t, "user_id")
        cis_t = by_tenant(cis, t, "ci_id")
        dt = base_dt()
        rows.append({
            "tenant_id": t, "kb_id": f"KB{n + i:07d}",
            "title": f"{title} [{n + i:04d}]", "summary": summary, "content": content,
            "category": c, "tags": tags,
            "state": random.choice(["published", "published", "published", "draft", "retired"]),
            "audience": random.choice(["all", "end_user", "technician"]),
            "created_by": random.choice(users_t) if users_t else None,
            "created_at": iso(dt), "updated_at": iso(dt + timedelta(days=random.randint(0, 30))),
            "views": random.randint(0, 3000), "helpful_votes": random.randint(0, 600),
            "related_ci_ids": random.sample(cis_t, min(len(cis_t), random.randint(0, 2))) if cis_t else [],
            "related_incidents": [],
        })
    return rows


def gen_catalog(rows: list[dict]) -> list[dict]:
    n = max(1, len(rows))
    need = TARGETS["catalog_item"] - len(rows)
    names = ["Monitor", "Docking Station", "Headset", "Mobile Phone", "Tablet",
             "Software License", "Database Access", "VPN Access", "Cloud Sandbox",
             "Keyboard", "External SSD", "Conference Mic", "GPU Workstation"]
    for i in range(need):
        t = pick_tenant()
        nm = random.choice(names)
        rows.append({
            "tenant_id": t,
            "catalog_item_id": f"CAT_{t}_{nm.upper().replace(' ', '_')}_{n + i}",
            "name": f"{nm} request", "description": f"Standard provisioning of: {nm}.",
            "category": random.choice(["hardware", "software", "access"]),
            "owner_group": random.choice(TENANT_META[t]["groups"]),
            "estimated_total_minutes": random.choice([60, 120, 240, 480, 1440]),
            "tasks": [{"task_id": "T1", "name": f"Provision {nm}", "type": "manual",
                       "owner_group": random.choice(TENANT_META[t]["groups"]),
                       "depends_on": [], "estimated_minutes": 60}],
        })
    return rows


def gen_onboarding(rows: list[dict], catalog: list[dict]) -> list[dict]:
    n = max(1, len(rows))
    need = TARGETS["onboarding_template"] - len(rows)
    for i in range(need):
        t = pick_tenant()
        dept = random.choice(["Engineering", "Sales", "Finance", "Support"])
        cat_t = by_tenant(catalog, t, "catalog_item_id")
        rows.append({
            "tenant_id": t, "template_id": f"ONB_{t}_{dept.upper()}_{n + i}",
            "name": f"{dept} onboarding ({t})",
            "description": f"Standard onboarding workflow for {dept} hires.",
            "department": dept,
            "default_catalog_item_id": random.choice(cat_t) if cat_t else None,
            "required_inputs": ["employee_name", "start_date", "manager_user_id", "location"],
            "tasks": [{"task_id": "T1", "name": "Create accounts", "type": "automated",
                       "owner_group": random.choice(TENANT_META[t]["groups"]),
                       "depends_on": [], "estimated_minutes": 10}],
        })
    return rows


# ── referential back-fill + validation ───────────────────────────────────


def backfill(problems, incidents, changes, kb):
    for p in problems:
        pid = p["problem_id"]
        p["related_incidents"] = sorted({i["incident_id"] for i in incidents
                                         if i.get("related_problem") == pid})[:6]
        p["related_changes"] = sorted({c["change_id"] for c in changes
                                       if c.get("related_problem") == pid})[:4]
    inc_by_tenant: dict[str, list[str]] = {}
    for i in incidents:
        inc_by_tenant.setdefault(i["tenant_id"], []).append(i["incident_id"])
    for a in kb:
        pool = inc_by_tenant.get(a["tenant_id"], [])
        if pool and not a.get("related_incidents"):
            k = min(len(pool), random.randint(0, 4))
            a["related_incidents"] = sorted(random.sample(pool, k))


def validate(tables: dict[str, list[dict]]) -> None:
    users = {(r["tenant_id"], r["user_id"]) for r in tables["sys_user"]}
    cis = {(r["tenant_id"], r["ci_id"]) for r in tables["cmdb_ci"]}
    probs = {(r["tenant_id"], r["problem_id"]) for r in tables["problem"]}
    chgs = {(r["tenant_id"], r["change_id"]) for r in tables["change"]}
    incs = {(r["tenant_id"], r["incident_id"]) for r in tables["incident"]}
    cats = {(r["tenant_id"], r["catalog_item_id"]) for r in tables["catalog_item"]}
    errors: list[str] = []

    def chk(cond, msg):
        if not cond:
            errors.append(msg)

    for i in tables["incident"]:
        t = i["tenant_id"]
        for f in ("reported_by", "assigned_to"):
            if i.get(f):
                chk((t, i[f]) in users, f"incident {i['incident_id']}.{f}={i[f]} dangling")
        if i.get("ci_id"):
            chk((t, i["ci_id"]) in cis, f"incident {i['incident_id']}.ci_id dangling")
        if i.get("related_problem"):
            chk((t, i["related_problem"]) in probs, f"incident {i['incident_id']}.related_problem dangling")
        if i.get("related_change"):
            chk((t, i["related_change"]) in chgs, f"incident {i['incident_id']}.related_change dangling")
        for c in i.get("linked_ci_ids", []):
            chk((t, c) in cis, f"incident {i['incident_id']}.linked_ci {c} dangling")
    for c in tables["change"]:
        t = c["tenant_id"]
        if c.get("related_problem"):
            chk((t, c["related_problem"]) in probs, f"change {c['change_id']}.related_problem dangling")
        for ci in c.get("affected_ci", []):
            chk((t, ci) in cis, f"change {c['change_id']}.affected_ci {ci} dangling")
    for r in tables["request"]:
        t = r["tenant_id"]
        if r.get("catalog_item_id"):
            chk((t, r["catalog_item_id"]) in cats, f"request {r['request_id']}.catalog_item_id dangling")
    for p in tables["problem"]:
        for i in p.get("related_incidents", []):
            chk((p["tenant_id"], i) in incs, f"problem {p['problem_id']}.related_incident {i} dangling")
    for a in tables["kb_knowledge"]:
        for i in a.get("related_incidents", []):
            chk((a["tenant_id"], i) in incs, f"kb {a['kb_id']}.related_incident {i} dangling")

    if errors:
        raise SystemExit("REFERENTIAL VALIDATION FAILED:\n  " + "\n  ".join(errors[:30]))
    print("referential validation: PASS")


def main() -> None:
    t = {name: load(name) for name in TARGETS}
    before = {k: len(v) for k, v in t.items()}

    t["sys_user"] = gen_users(t["sys_user"])
    t["cmdb_ci"] = gen_cis(t["cmdb_ci"], t["sys_user"])
    t["asset"] = gen_assets(t["asset"], t["sys_user"], t["cmdb_ci"])
    t["catalog_item"] = gen_catalog(t["catalog_item"])
    t["onboarding_template"] = gen_onboarding(t["onboarding_template"], t["catalog_item"])
    t["problem"] = gen_problems(t["problem"], t["sys_user"])
    t["change"] = gen_changes(t["change"], t["sys_user"], t["cmdb_ci"], t["problem"])
    t["incident"] = gen_incidents(t["incident"], t["sys_user"], t["cmdb_ci"],
                                  t["problem"], t["change"])
    t["request"] = gen_requests(t["request"], t["sys_user"], t["cmdb_ci"],
                                t["catalog_item"])
    t["kb_knowledge"] = gen_kb(t["kb_knowledge"], t["sys_user"], t["cmdb_ci"])

    backfill(t["problem"], t["incident"], t["change"], t["kb_knowledge"])
    validate(t)

    for name, rows in t.items():
        save(name, rows)
        tn = {}
        for r in rows:
            tn[r["tenant_id"]] = tn.get(r["tenant_id"], 0) + 1
        print(f"  {name:22s} {before[name]:4d} -> {len(rows):4d}   tenants={tn}")
    print("done.")


if __name__ == "__main__":
    main()
