"""Marketing: personas, their target audiences, and one-to-one outreach campaigns that staff approve batch by batch."""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models


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
        ALL = "", "Every YES"
        DATA = "data", "Data safety"
        DAY = "day", "Team's day"
        CHAT = "chat", "Team chat"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.FIRST)
    text = models.TextField(max_length=1000)
    # After-YES messages only: which sequence it belongs to ("" = every YES) and how many days after the YES it is due.
    track = models.CharField(max_length=4, choices=Track.choices, default="", blank=True)
    day = models.PositiveSmallIntegerField(default=0)
    # A picture or video staff attach in WhatsApp (wa_media.py). Never on messages to people who haven't replied.
    media = models.FileField(upload_to="wa-media/", blank=True)
    media_kind = models.CharField(max_length=5, blank=True)  # image or video
    media_name = models.CharField(max_length=160, blank=True)  # the name it was uploaded with
    # The outreach structure (wa_structure.py): creative, intro, problem, solution, reward, cta, details, notes.
    # Empty = a free-text template. When set, `text` is composed from it on save.
    parts = models.JSONField(default=dict, blank=True)
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
    touch3_days = models.PositiveSmallIntegerField(default=7, help_text="Days after the first message.")
    touch4_days = models.PositiveSmallIntegerField(default=60, help_text="Days after the first message.")
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

    campaign = models.ForeignKey(WaCampaign, on_delete=models.SET_NULL, related_name="contacts", null=True, blank=True)
    phone = models.CharField(max_length=16, unique=True)  # E.164: +919810000101
    first_name = models.CharField(max_length=80, blank=True)
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
    track = models.CharField(max_length=4, choices=WaTemplate.Track.choices, default="", blank=True)
    yes_date = models.DateField(null=True, blank=True)
    nurture_day = models.SmallIntegerField(default=-1)
    needs_reply = models.BooleanField(default=False)  # they wrote and nobody has answered yet
    paused_until = models.DateField(null=True, blank=True)
    added_by = models.EmailField(blank=True)
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
        CUSTOM = "custom", "One-off message"
        REPLY = "reply", "Our reply"
        THEIRS = "theirs", "Their message"

    contact = models.ForeignKey(WaContact, on_delete=models.CASCADE, related_name="messages")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    text = models.TextField(max_length=2000)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    by = models.EmailField(blank=True)

    class Meta:
        ordering = ["at", "id"]


class WaBlock(models.Model):
    """A number that asked to stop. Never messaged again, in any campaign, and skipped on every upload."""

    phone = models.CharField(max_length=16, unique=True)
    reason = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
