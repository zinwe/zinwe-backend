"""Marketing: personas, their target audiences, and one-to-one outreach campaigns that staff approve batch by batch."""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Persona(models.Model):
    """The base customer profile every target audience starts from. `profile` holds the 12 profiling sections."""

    class Confidence(models.TextChoices):
        GUESS = "Guess", "Guess"
        HEARD_ONCE = "Heard once", "Heard once"
        CONFIRMED = "Confirmed", "Confirmed"

    name = models.CharField(max_length=120)
    confidence = models.CharField(max_length=12, choices=Confidence.choices, default=Confidence.GUESS)
    profile = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class Audience(models.Model):
    """A target audience inside a persona: who and where, their problem, our answer, the offer and the channels."""

    class BusinessType(models.TextChoices):
        B2B = "B2B", "B2B"
        B2C = "B2C", "B2C"
        BOTH = "Both", "Both"

    class Objective(models.TextChoices):
        LEADS = "Leads", "Leads"
        SALES = "Sales", "Sales"
        ENQUIRIES = "Enquiries", "Enquiries"
        AWARENESS = "Brand Awareness", "Brand Awareness"

    persona = models.ForeignKey(Persona, on_delete=models.PROTECT, related_name="audiences")
    name = models.CharField(max_length=120)
    country = models.CharField(max_length=40, default="USA")
    state = models.CharField(max_length=80, blank=True)  # blank: the whole country
    city = models.CharField(max_length=80, blank=True)  # blank: the whole state
    business_category = models.CharField(max_length=120)
    business_segment = models.CharField(max_length=120)
    business_type = models.CharField(max_length=4, choices=BusinessType.choices, default=BusinessType.B2B)
    primary_problem = models.CharField(max_length=240)
    secondary_problem = models.CharField(max_length=240, blank=True)
    product_service = models.CharField(max_length=120)
    how_solves = models.TextField()
    key_benefits = models.JSONField(default=list)
    primary_offer = models.CharField(max_length=240)
    pricing = models.CharField(max_length=240, blank=True)
    objective = models.CharField(max_length=16, choices=Objective.choices, default=Objective.LEADS)
    cta = models.CharField(max_length=120)
    job_titles = models.JSONField(default=list, blank=True)
    decision_makers = models.JSONField(default=list, blank=True)
    industry_keywords = models.JSONField(default=list, blank=True)
    competitors = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    channels = models.JSONField(default=list)  # the first channel is the best channel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class Campaign(models.Model):
    """One email flow for one audience. Nothing goes out until staff launch it and approve each day's batch."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"

    name = models.CharField(max_length=120)
    audience = models.ForeignKey(Audience, on_delete=models.PROTECT, related_name="campaigns")
    goal = models.CharField(max_length=120, blank=True)
    theme = models.CharField(max_length=60, blank=True)
    problem = models.CharField(max_length=240, blank=True)
    offer = models.CharField(max_length=240, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    pause_reason = models.CharField(max_length=240, blank=True)
    daily_limit = models.PositiveSmallIntegerField(default=10)
    pattern = models.CharField(max_length=20, default="full")  # the set of emails it started from: patterns.PATTERNS
    # Launch checks that a person answers for.
    rules_checked_at = models.DateTimeField(null=True, blank=True)
    rules_checked_by = models.EmailField(blank=True)
    test_sent_at = models.DateTimeField(null=True, blank=True)
    emails_read_at = models.DateTimeField(null=True, blank=True)  # someone read them against this audience
    dns_result = models.JSONField(default=dict, blank=True)  # {"spf": bool, "dkim": bool, "dmarc": bool}
    dns_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class EmailTemplate(models.Model):
    """A generic email. A campaign takes a copy, so nothing here changes a campaign, and no campaign edit changes this."""

    class Kind(models.TextChoices):
        COLD = "Cold", "Cold"
        SETUP = "Setup", "Setup"
        TIPS = "Tips", "Tips"
        FINAL = "Final", "Final"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.TIPS)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    step = models.PositiveSmallIntegerField(null=True, blank=True, unique=True)  # its place in the patterns: patterns.PATTERNS
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class CampaignEmail(models.Model):
    """One step of a campaign's flow. Subject and body may use {{FirstName}} and {{Firm}}."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="emails")
    n = models.PositiveSmallIntegerField()
    stage = models.CharField(max_length=12)  # Cold, Setup, Tips, Final
    when = models.CharField(max_length=30)  # as shown to staff: "Day 0", "Week 3"
    week = models.PositiveSmallIntegerField(default=0)  # sets the gap to the next email
    subject = models.CharField(max_length=200)
    body = models.TextField()
    template = models.ForeignKey(EmailTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="uses")

    class Meta:
        ordering = ["campaign_id", "n"]
        constraints = [models.UniqueConstraint(fields=["campaign", "n"], name="unique_email_step")]


class Suppression(models.Model):
    """Do not email: an address blocked from all outreach, for good."""

    email = models.EmailField(unique=True)
    reason = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Contact(models.Model):
    """A hand-picked person in a campaign, and where they are in its flow."""

    class Status(models.TextChoices):
        NEW = "new", "Ready"
        COLD = "cold", "Cold"
        WAITING = "waiting", "Waiting for reply"
        OPTED = "opted", "Opted in"
        SIGNED = "signed", "Signed up"
        PAUSED = "paused", "Paused"
        SNOOZED = "snoozed", "Snoozed"
        CLOSED = "closed", "Closed"
        FINISHED = "finished", "Finished"
        SUPPRESSED = "suppressed", "Do not email"

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="contacts")
    first_name = models.CharField(max_length=80)
    firm = models.CharField(max_length=160)
    email = models.EmailField()
    city = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    next_email = models.PositiveSmallIntegerField(default=1)  # 0: nothing planned
    next_date = models.DateField(null=True, blank=True)
    last_email = models.PositiveSmallIntegerField(default=0)
    opted_in_at = models.DateField(null=True, blank=True)
    proof = models.CharField(max_length=240, blank=True)  # their words when they said yes
    replied_at = models.DateField(null=True, blank=True)
    replied_step = models.PositiveSmallIntegerField(default=0)  # the last email they had when they first replied
    signed_up_at = models.DateField(null=True, blank=True)
    installed = models.BooleanField(default=False)
    reason = models.CharField(max_length=160, blank=True)
    prior = models.JSONField(null=True, blank=True)  # where the flow stood before a question paused it
    token = models.CharField(max_length=64, unique=True, default=new_token, editable=False)
    added_by = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["campaign", "email"], name="unique_contact_per_campaign")]


class ContactEvent(models.Model):
    """The contact's history, as staff read it."""

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    text = models.CharField(max_length=240)
    actor = models.EmailField(blank=True)

    class Meta:
        ordering = ["at", "id"]


class OutreachSend(models.Model):
    """One approved email to one contact. `pending` means a person approved it; only these rows are ever sent.

    A row left in `sending` by a crash is marked failed, never sent again by itself: a second cold email is
    worse than a missing one, and the contact simply stays due for the next approved batch."""

    class Status(models.TextChoices):
        PENDING = "pending", "Approved"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="sends")
    n = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True)
    approved_by = models.EmailField()
    approved_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["approved_at", "id"]
        constraints = [models.UniqueConstraint(fields=["contact", "n"], name="one_send_per_step")]


def sender_address() -> str:
    """The bare address outreach comes from."""
    value = settings.ZINWE_OUTREACH_FROM
    return value.split("<")[-1].rstrip(">").strip().lower()


# --- WhatsApp outreach ---------------------------------------------------------------------------------------------
# A person sends every message from the Zinwe WhatsApp Business app: Zinwe drafts, opens the chat with the text filled
# in, and records what was sent and what came back. Nothing here sends a WhatsApp message by itself.


class WaTag(models.Model):
    """A label staff put on templates (CA firms, Festival...): their own, nothing in the flow reads it."""

    # The fixed palette, in the order new tags take it.
    COLORS = ["#24508f", "#f26a0a", "#17663f", "#c0331c", "#7a4fb5", "#0f766e", "#9c5b2e", "#54656f"]

    name = models.CharField(max_length=60)
    color = models.CharField(max_length=7, choices=[(c, c) for c in COLORS], default=COLORS[0])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.UniqueConstraint(Lower("name"), name="wa_tag_name_ci")]

    def __str__(self) -> str:
        return self.name


class WaTemplate(models.Model):
    """A WhatsApp message draft. {{FirstName}}, {{Firm}} (or {{Business}}) and {{City}} are filled in per contact."""

    class Kind(models.TextChoices):
        FIRST = "first", "First message"
        FOLLOWUP = "followup", "Follow-up"
        TOUCH3 = "touch3", "No reply: polite close"
        TOUCH4 = "touch4", "No reply: extra message"
        INTERESTED = "interested", "Reply: interested"
        QUESTION = "question", "Reply: question"
        NOTNOW = "notnow", "Reply: not now"
        NURTURE = "nurture", "After YES"

    class Track(models.TextChoices):
        """What the contact wants (100 Reviews campaign, owner 10 Nov 2026). On a contact "" = Unassigned; on an
        older after-YES template "" = every YES. The old Team chat track is retired (its templates are archived)."""

        NONE = "", "Unassigned"
        PRIVACY = "privacy", "Data Privacy"
        ACTIVITY = "activity", "Team Activity Monitoring"
        BOTH = "both", "Both Pack"

    class Category(models.TextChoices):
        """The campaign's message categories (wa_flow.py). "" = an older template (kind decides)."""

        NONE = "", "Older template"
        OUTREACH = "outreach", "Intro"
        ENGAGE = "engage", "Engage"
        DEMO_VIDEO = "demo_video", "Demo video"
        SETUP = "setup", "Setup"
        DEMO_CALL = "demo_call", "Demo call"
        OFFER = "offer", "Offer & discount"
        TESTIMONIAL = "testimonial", "Testimonial"
        QUICK = "quick_reply", "Quick reply"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.FIRST)
    # Blank only for a campaign draft (a category row whose copy isn't written yet): drafts are never sent.
    text = models.TextField(max_length=1000, blank=True)
    # After-YES messages only: which sequence it belongs to ("" = every YES) and how many days after the YES it is due.
    # Category rows: only Engage and Demo video carry a Track.
    track = models.CharField(max_length=8, choices=Track.choices, default="", blank=True)
    category = models.CharField(max_length=12, choices=Category.choices, default="", blank=True, db_index=True)
    type_name = models.CharField(max_length=60, blank=True)  # the category's type: Need discovery, Booking link...
    stage = models.PositiveSmallIntegerField(default=0)  # Intro and Offer: stage 1-3 of the 90 days; 0 = none
    # Intro: the day 1-90 it is due. Engage and Demo video: step 1-6 within the Track. Setup, Testimonial: 1-2.
    step = models.PositiveSmallIntegerField(default=0)
    text_hi = models.TextField(max_length=1000, blank=True)  # category rows: the Hinglish text on the same row
    coupon = models.ForeignKey("z_auth.Coupon", on_delete=models.SET_NULL, null=True, blank=True, related_name="wa_templates")
    event = models.BooleanField(default=False)  # Offer: a festival code, picked by staff, never by the queue
    archived = models.BooleanField(default=False)  # kept for its history, never sent again
    day = models.PositiveSmallIntegerField(default=0)
    # A picture or video staff attach in WhatsApp (wa_media.py). Never on messages to people who haven't replied.
    media = models.FileField(upload_to="wa-media/", blank=True)
    media_kind = models.CharField(max_length=5, blank=True)  # image or video
    media_name = models.CharField(max_length=160, blank=True)  # the name it was uploaded with
    class Language(models.TextChoices):
        EN = "en", "English"
        HI = "hi", "Hinglish"

    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    # The same message in the other language (set both ways), so a campaign can send either and compare.
    twin = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # The outreach structure (wa_structure.py): creative, intro, problem, solution, reward, cta, details, notes.
    # Empty = a free-text template. When set, `text` is composed from it on save.
    parts = models.JSONField(default=dict, blank=True)
    tags = models.ManyToManyField(WaTag, blank=True, related_name="templates")
    # Set on a version B row (A/B test, wa_templates.py): same category, type and step as A, never its own card.
    # While A has an un-archived B, odd-numbered contacts get B; Keep ends the test and archives B.
    version_of = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="versions")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class WaCampaign(models.Model):
    """
    One message sequence to one target audience, for opted-in numbers. Someone who never replies gets the first message,
    one follow-up (a tip) and a polite close (touch 3), then nothing. Touch 4 exists but stays off for outreach.
    Three kinds of reply.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        LIVE = "live", "Live"
        PAUSED = "paused", "Paused"

    class Goal(models.TextChoices):
        CONVERSATIONS = "Conversations", "Conversations"
        SIGNUPS = "Free signups", "Free signups"
        INSTALLS = "Installs", "Installs"
        PAID = "Paid plans", "Paid plans"

    name = models.CharField(max_length=120)
    audience = models.ForeignKey(Audience, on_delete=models.PROTECT, related_name="wa_campaigns", null=True, blank=True)
    goal = models.CharField(max_length=16, choices=Goal.choices, default=Goal.CONVERSATIONS)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    daily_limit = models.PositiveSmallIntegerField(default=15)
    followup_days = models.PositiveSmallIntegerField(default=3)
    first = models.ForeignKey(WaTemplate, on_delete=models.PROTECT, related_name="+")
    followup = models.ForeignKey(WaTemplate, on_delete=models.SET_NULL, related_name="+", null=True, blank=True)
    interested = models.ForeignKey(WaTemplate, on_delete=models.SET_NULL, related_name="+", null=True, blank=True)
    question = models.ForeignKey(WaTemplate, on_delete=models.SET_NULL, related_name="+", null=True, blank=True)
    notnow = models.ForeignKey(WaTemplate, on_delete=models.SET_NULL, related_name="+", null=True, blank=True)
    touch3 = models.ForeignKey(WaTemplate, on_delete=models.SET_NULL, related_name="+", null=True, blank=True)
    touch4 = models.ForeignKey(WaTemplate, on_delete=models.SET_NULL, related_name="+", null=True, blank=True)
    class Language(models.TextChoices):
        EN = "en", "English"
        HI = "hi", "Hinglish"
        SPLIT = "ab", "Half English, half Hinglish"

    # Which language the cold messages go out in. "ab" splits contacts evenly so the two can be compared (Analytics).
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    class Plan(models.TextChoices):
        TOUCHES = "touches", "First message + up to 3 touches"
        NINETY = "ninety", "90 days: Intro templates by day, then the YES path"

    # ninety: the 100 Reviews model (wa_flow.py); `first` is still required but only names the campaign's opener.
    plan = models.CharField(max_length=8, choices=Plan.choices, default=Plan.TOUCHES)
    touch3_days = models.PositiveSmallIntegerField(default=7, help_text="Days after the first message.")
    touch4_days = models.PositiveSmallIntegerField(default=60, help_text="Days after the first message.")

    class Engine(models.TextChoices):
        """slots: the touches / ninety code above (wa_flow.py, whatsapp.py), unchanged. path: the step-by-step path in
        `path` (wa_paths.py). Existing campaigns stay on slots until `manage.py wa_paths --convert` switches them."""

        SLOTS = "slots", "Slots (the old engine)"
        PATH = "path", "Step-by-step path"

    engine = models.CharField(max_length=5, choices=Engine.choices, default=Engine.SLOTS)
    # {"v": 1, "lanes": {"main": [...], "yes": [...], "notnow": [...], "stop": [...]}}: wa_path_model.py
    path = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return self.name


class WaContact(models.Model):
    """A business that may get WhatsApp messages. One row per phone number, across every campaign."""

    class Status(models.TextChoices):
        READY = "ready", "Ready"
        MESSAGED = "messaged", "Messaged"
        REPLIED = "replied", "Replied"
        INTERESTED = "interested", "Interested"
        SIGNED = "signed", "Signed up"
        NOTNOW = "notnow", "Not now"
        STOP = "stop", "Do not message"

    class CampaignStatus(models.TextChoices):
        NONE = "", "Not started"
        MESSAGED = "messaged", "Messaged"
        REPLIED = "replied", "Replied"
        YES = "yes", "YES"
        ENGAGED = "engaged", "Engaged"
        TRACK = "track_assigned", "Track assigned"
        DEMO = "demo_video_sent", "Demo video sent"
        SETUP = "setup", "Setup"
        DEMO_CALL = "demo_call", "Demo call"
        OFFER = "offer", "Offer"
        PAID = "paid", "Paid"
        NO_REPLY = "no_reply", "No reply"
        OPTED_OUT = "opted_out", "Opted out"

    class Pack(models.TextChoices):
        NONE = "", "Not picked"
        STARTUP = "startup", "Startup pack"
        TEAM = "team", "Team pack"

    class Testimonial(models.TextChoices):
        NONE = "", "Not asked"
        WRITTEN_ASKED = "written_asked", "Written: asked"
        WRITTEN = "written", "Written: got it"
        VIDEO_ASKED = "video_asked", "Video: asked"
        VIDEO = "video", "Video: got it"

    campaign = models.ForeignKey(WaCampaign, on_delete=models.SET_NULL, related_name="contacts", null=True, blank=True)
    phone = models.CharField(max_length=16, unique=True)  # E.164: +919810000101
    first_name = models.CharField(max_length=80, blank=True)
    owner_name = models.CharField(max_length=120, blank=True)  # who decides, when it isn't the contact: {{OwnerName}}
    firm = models.CharField(max_length=160)
    segment = models.CharField(max_length=120)
    country = models.CharField(max_length=40)
    city = models.CharField(max_length=80)
    source = models.CharField(max_length=160)  # where the number was found
    info = models.JSONField(default=dict, blank=True)  # Useful Info: website, team_size, notes...
    # Locked until they opt in: Zinwe never drafts, queues or records a send for a contact who hasn't (owner, 3 Nov 2026).
    opted_in = models.BooleanField(default=True)
    opt_in_note = models.CharField(max_length=200, blank=True)  # how they agreed, in staff's words
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.READY, db_index=True)
    step = models.PositiveSmallIntegerField(default=0)  # 0 nothing sent, 1 first, 2 follow-up, 3 touch 3, 4 touch 4
    next_date = models.DateField(null=True, blank=True)  # when the next no-reply message is due
    first_sent = models.DateField(null=True, blank=True)  # touches 3 and 4 count their days from here
    # After YES: which sequence they are on, when they said YES, and the day of the last after-YES message sent.
    # Track: what they want, set by staff from their Engage answer, never at upload. Independent of segment.
    track = models.CharField(max_length=8, choices=WaTemplate.Track.choices, default="", blank=True)
    yes_date = models.DateField(null=True, blank=True)
    nurture_day = models.SmallIntegerField(default=-1)
    # The language of their first message (en or hi), kept so Analytics can compare reply rates.
    lang = models.CharField(max_length=2, blank=True, default="")
    needs_reply = models.BooleanField(default=False)  # they wrote and nobody has answered yet
    # The 90-day campaign (wa_flow.py). campaign_status is the spec's path; Day and Stage come from first_sent.
    campaign_status = models.CharField(max_length=16, choices=CampaignStatus.choices, default="", blank=True, db_index=True)
    out_day = models.PositiveSmallIntegerField(default=0)  # the Intro day last sent (0 = none)
    engage_step = models.PositiveSmallIntegerField(default=0)
    demo_step = models.PositiveSmallIntegerField(default=0)
    setup_step = models.PositiveSmallIntegerField(default=0)
    demo_call_sent = models.BooleanField(default=False)
    offer_sent = models.DateField(null=True, blank=True)
    setup_error = models.BooleanField(default=False)  # staff saw setup stop at an error: the Demo call goes next
    using_since = models.DateField(null=True, blank=True)  # first PC connected: the Testimonial is due 14 days later
    pack = models.CharField(max_length=8, choices=Pack.choices, default="", blank=True)
    testimonial = models.CharField(max_length=14, choices=Testimonial.choices, default="", blank=True)
    paused_until = models.DateField(null=True, blank=True)
    added_by = models.EmailField(blank=True)
    # Where the contact is on a "path" campaign's steps (wa_paths.py); blank on slots campaigns and before they start.
    lane = models.CharField(max_length=8, blank=True, default="")  # main, yes, notnow, stop
    step_id = models.CharField(max_length=24, blank=True, default="")
    step_due = models.DateTimeField(null=True, blank=True)  # the planned time of the step's next action (before the window)
    step_entered = models.DateTimeField(null=True, blank=True)
    # The engine's bookkeeping: the campaign the position belongs to, the group cursor, where they left the main lane...
    path_state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.firm} ({self.phone})"


class WaMessage(models.Model):
    """One message in a WhatsApp conversation, as a person recorded it: what we sent, or what they wrote."""

    class Kind(models.TextChoices):
        FIRST = "first", "First message"
        FOLLOWUP = "followup", "Follow-up"
        TOUCH = "touch", "No-reply touch"
        NURTURE = "nurture", "After-YES message"
        FLOW = "flow", "Campaign message"
        CUSTOM = "custom", "One-off message"
        REPLY = "reply", "Our reply"
        THEIRS = "theirs", "Their message"

    contact = models.ForeignKey(WaContact, on_delete=models.CASCADE, related_name="messages")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # The template it came from (category rows), for each template's performance numbers.
    template = models.ForeignKey(WaTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="sends")
    text = models.TextField(max_length=2000)
    # The language a templated message went out in (en or hi); "" for older rows and their messages.
    language = models.CharField(max_length=2, blank=True, default="")
    # Path campaigns (wa_paths.py): the campaign and the step this message completed, for the numbers per step.
    campaign = models.ForeignKey(WaCampaign, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    step_id = models.CharField(max_length=24, blank=True, default="")
    lane = models.CharField(max_length=8, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    by = models.EmailField(blank=True)

    class Meta:
        ordering = ["at", "id"]


class WaBlock(models.Model):
    """A number that asked to stop. Never messaged again, in any campaign, and skipped on every upload."""

    phone = models.CharField(max_length=16, unique=True)
    reason = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
