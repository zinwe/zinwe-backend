"""The Zinwe base persona (migration 0004). Everything here is a starting guess until real conversations confirm it.

Left blank on purpose where we have no fact: staff fill those in. Prices are never written here, they come from /plans.
"""
from __future__ import annotations

NAME = "Zinwe base persona"

PROFILE = {
    "business": {
        "brand": "Zinwe (by SKYTECHNO, New Delhi)",
        "product": "Zinwe",
        "website": "https://zinwe.io",
        "industry": "Cybersecurity: data protection and activity monitoring software",
        "model": "B2B",
        "description": (
            "A small agent on each employee's Windows PC watches data leaving the company (USB drives, large transfers, "
            "email attachments), remote-access tools, new software and work hours. The owner gets plain-English alerts "
            "and sets rules through a guided setup."
        ),
        "value": "Know what is happening to your company's data, in plain English, with no IT department needed.",
        "pricing": "Free forever plan to start. Paid plans differ by number of PCs and history. Read current prices on zinwe.io/plans.",
        "market": "India and the USA, equal priority",
        "competitors": "EmpMonitor, Teramind, ActivTrak, Time Doctor, Hubstaff, Controlio, Endpoint Protector, doing nothing",
    },
    "icp_b2b": {
        "industry": "CA and accounting firms, digital agencies, clinics, small IT and BPO shops, traders and export houses, design studios",
        "type": "Owner-run small business or startup with no IT department",
        "size": "5–50 people",
        "revenue": "",
        "employees": "5–50",
        "location": "India and the USA",
        "decider": "The owner, founder or managing partner",
        "designation": "Owner, Founder, Director, Managing Partner",
        "authority": "Decides alone and can try a free tool the same day. The office manager often runs the setup.",
        "tech": "Windows PCs, shared drives, email, WhatsApp or group chats, basic antivirus",
        "maturity": "Established enough to have staff and client data, too small to hire an IT person",
    },
    "icp_b2c": {},
    "psychology": {
        "primary": "Client lists and files can leave the company without the owner knowing",
        "secondary": "Nobody knows what software or remote tools are on the office PCs",
        "frustrations": "Security tools are made for IT teams. Logs are hard to read. Instructions get lost in WhatsApp groups and calls.",
        "outcomes": "Peace of mind, proof of what left, and a calmer team day",
        "goals": "Protect client trust and keep the business running without hiring IT",
        "motivations": "An employee leaving, a client asking how their data is protected, the team growing",
        "triggers": "Worry after someone senior resigns, a scare at a nearby business, a stranger asking for AnyDesk",
        "behaviour": "Searches a specific worry, reads a short article, tries free on one PC before talking to anyone",
        "brands": "Tools that are simple, honest and made for small teams",
        "trust": "Free plan, the team can see the widget and agrees to a notice, no screen recording, plain answers from the founder",
    },
    "pains": [
        {
            "pain": "An employee leaves with the client list on a USB drive",
            "solution": "USB alerts, plus allow, read-only or block for each drive",
            "benefit": "Know what left, and when, in plain English",
        },
        {
            "pain": "A stranger asks staff to run AnyDesk",
            "solution": "Alerts when remote tools like AnyDesk or TeamViewer are in use",
            "benefit": "Catch a scam call early and ask a calm question",
        },
        {
            "pain": "Nobody knows what software is on each PC",
            "solution": "Alerts when new software appears on a PC",
            "benefit": "A current picture with no IT person",
        },
        {
            "pain": "Hard to see who started, who is on a break and who has logged out",
            "solution": "Log in, break and log out from the PC widget, with break approval",
            "benefit": "See the day per person without asking",
        },
        {
            "pain": "Instructions get lost in group chats and calls",
            "solution": "Owner-to-employee chat on the PC widget with ready-made notes",
            "benefit": "Know a message was delivered and seen",
        },
    ],
    "journey": {
        "awareness": "Blog articles, LinkedIn posts, search on a specific worry, a referral from a CA or IT vendor",
        "interest": "A real everyday scenario, explained in plain English",
        "consideration": "Doing nothing, a local IT vendor, antivirus, or monitoring tools made for bigger teams",
        "intent": "Someone senior resigns, a client asks about data, or the team grows",
        "purchase": "The free PCs run out, or they need longer history",
        "retention": "Useful alerts, the team's day and chat become part of the routine",
        "referral": "A first useful alert that saved a problem, shared with other owners or their CA",
    },
    "channels": [
        {"channel": "Meta Ads", "strategy": "Small business owners by interests and behaviours, one problem per ad", "objective": "Lead generation"},
        {"channel": "Google Ads", "strategy": "Searches for small business data safety and USB control", "objective": "High-intent leads"},
        {"channel": "LinkedIn", "strategy": "Owners, founders and partners of firms with 5–50 people", "objective": "B2B acquisition"},
        {"channel": "Email", "strategy": "Named decision makers in hand-checked, segmented lists", "objective": "Outreach"},
        {"channel": "Instagram", "strategy": "Everyday office scenarios and the IT Is OK story", "objective": "Awareness"},
        {"channel": "SEO", "strategy": "Plain-English articles for problem searches", "objective": "Organic acquisition"},
        {"channel": "WhatsApp", "strategy": "Only people who said yes, for setup help and short updates", "objective": "Conversion"},
    ],
    "keywords": {
        "primary": "cybersecurity for small business, data security for small businesses",
        "secondary": "data loss prevention for small business, best dlp solution for small business, sme cyber security",
        "competitor": "Teramind alternative, ActivTrak alternative, EmpMonitor alternative",
        "industry": "cybersecurity for smb, cyber security services for small business",
        "problem": "stop employees copying files to USB, AnyDesk scam, what software is on my office PCs",
        "product": "user activity monitoring software, employee pc monitoring software, staff monitoring tools",
        "intent": "Problem searches first. Product searches are smaller and cost more (zinwe-marketing keyword tabs).",
        "interests": "Small business owners, entrepreneurship, accounting firms, agencies, office management",
    },
    "email": {
        "titles": "Owner, Founder, Managing Partner, Director, Practice Owner",
        "industries": "CA and CPA firms, digital agencies, clinics, small IT and BPO shops, traders, design studios",
        "size": "5–50 people",
        "location": "India and the USA",
        "pain": "Client files leaving with staff",
        "objective": "Get a reply, then a free signup and one PC installed",
        "variables": "{{FirstName}}, {{Firm}}",
        "offer": "Free forever plan, try it on one PC",
        "cta": "Try it on one PC",
        "followup": "At most 2 cold emails, then stop if there is no reply. After a yes: setup emails, then one tip a week.",
    },
    "ads": {
        "objective": "Free plan signups",
        "audience": "Owners and founders of businesses with 5–50 people",
        "age": "",
        "geo": "India and the USA, run as separate campaigns",
        "interests": "Small business, entrepreneurship, accounting, agencies, office management",
        "behaviours": "Small business page admins, business decision makers",
        "custom": "Website visitors who accepted cookies, confirmed newsletter subscribers",
        "lookalike": "Not yet. Wait for enough real signups.",
        "retargeting": "Visitors to /plans and blog readers who have not signed up",
        "exclusion": "Current customers, firms with their own IT team, offices with only Mac or Linux",
        "creative": "Black and white with an orange accent, one everyday office scene, 3–6 big words",
        "message": "Know what left your company, in plain English. No IT department needed.",
        "offer": "Free forever plan",
        "cta": "Start free",
    },
    "segments": [
        {"segment": "Cold audience", "traits": "Has not heard of Zinwe", "approach": "Awareness: name one everyday problem"},
        {"segment": "Warm audience", "traits": "Reads the blog or newsletter", "approach": "Education: how-tos and one-PC tests"},
        {"segment": "Hot audience", "traits": "Visited /plans or signed up and has not installed", "approach": "Conversion: setup help"},
        {"segment": "Existing customers", "traits": "At least one PC enrolled", "approach": "Retention: tips and new features"},
        {"segment": "High-value customers", "traits": "Team growing past the current plan", "approach": "Upselling: more PCs and longer history"},
    ],
    "market": {
        "direct": "EmpMonitor, Teramind, ActivTrak, Controlio, Kickidler, Endpoint Protector, ManageEngine Device Control Plus",
        "indirect": "Time Doctor, Hubstaff, DeskTime, Seqrite, a local IT vendor, doing nothing",
        "audience": "Larger teams with an IT or HR owner who reads the reports",
        "positioning": "Mostly reports and controls for technical buyers",
        "pricing": "",
        "channels": "",
        "gaps": "Data safety and the team's day in one simple tool for owners with no IT person",
        "advantage": "Plain-English alerts, guided setup, a Free forever plan, consent-first by design",
    },
    "definition": {
        "customer": (
            "The owner of a 5–50 person business in India or the USA, with no IT department, on Windows PCs. They hold "
            "client or customer data, decide alone, and worry about what leaves when people move on. They want peace of "
            "mind and proof, not another technical tool."
        ),
        "message": "Know what's happening to your company's data, in plain English. IT Is OK.",
        "goal": "Lead",
        "strategy": (
            "Reach owners through search, LinkedIn and hand-picked email. Engage with one everyday problem per segment. "
            "Qualify by asking them to try one PC on the Free forever plan. Convert when they need more PCs or history."
        ),
    },
}

# What the first audience (migration 0002) needs in the new form. Only fills blanks.
FIRST_AUDIENCE = {
    "state": "Texas",
    "business_segment": "Firms of 5–15 people",
    "business_type": "B2B",
    "product_service": "Zinwe",
    "how_solves": (
        "Zinwe alerts the owner in plain English when client files are copied to a USB drive, and lets them set each "
        "drive to allow, read-only or block."
    ),
    "key_benefits": ["Know when client files leave on a USB drive", "Simple USB rule with no IT person", "Try it on one PC for free"],
    "pricing": "Free forever plan. Paid prices on zinwe.io/plans.",
    "objective": "Leads",
    "cta": "Try it on one PC",
    "job_titles": ["Managing Partner", "Owner"],
    "competitors": [],
}
