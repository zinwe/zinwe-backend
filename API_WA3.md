# WhatsApp campaign paths: the API (wa3 backend, 12 Nov 2026)

Base: `/api/admin/marketing/wa` (staff only, as before). Every example below is a real response from the worktree's
local DB (`seed_100_reviews --sample`), trimmed where it says so. Template ids are that DB's.

Nothing existing changed shape. Every new key is **added**. Slots (old-engine) rows carry the new keys blank.

## 1. The path (the contract)

`WaCampaign.path` = `{"v": 1, "lanes": {"main": [...], "yes": [...], "notnow": [...], "stop": [...]}}`.
Each step is `{"id", "k", ...}`. Ids: 1–24 characters, letters, digits, `-` or `_`, unique across all lanes.

| k | fields (defaults) | meaning |
|---|---|---|
| `start` | | The main lane's first step. It is only there. |
| `send` | `template` (id or `null`), `only_if` (`""` · `"no_pc"` · `"track:privacy"`/`"track:activity"`/`"track:both"`) | The message goes on Today. |
| `wait` | `n` (≥1), `unit` (`hours`/`days`/`weeks`, default `days`), `window` (default `true`) | The time before the next step. `window` = Mon–Sat 10:00–19:00. |
| `reply` | `n`, `unit` | Wait up to n for their answer. |
| `track` | | Staff set the Track on Today; the contact goes on once it is set. |
| `group` | `category` (any stored category except `quick_reply`), `count`, `every` (days), `window` (default `true`) | The next `count` unsent templates of that category, one every `every` days. |
| `goal` | `goal`: `yes` · `signup` · `first_pc` · `paid` | The contact waits here until the goal happens, then goes on. |
| `goto` | `lane`, `step` (a step id, or `"resume"`) | `resume` goes back to the main-lane step they left. |
| `end` | `label`, `note`, `status`: `no_reply` · `opted_out` · `done` (default `done`) | They leave the campaign with that status. |
| `empty` | | The builder's placeholder. It is accepted and dropped on save. |

Any other short key on a step is kept as sent: a string of 200 characters or less, a number, a bool or null, up to 12
keys per step. The board's `lang`, `slot`, `who` and `cond` round-trip.

### How the engine runs it (`z_marketing/wa_paths.py`)
- **Today is by day.** A step is due when its date, after the window, is today or earlier. A contact gets at most one
  message a day. The daily limit counts main-lane messages only.
- **Window.** A message due on a Sunday moves to Monday (`moved_from: "Sun 22 Nov"`). Hour waits also move into
  10:00–19:00. The calendar after the moved message does not move.
- **Main-lane waits count on the campaign calendar.** Day 1 is the first message actually sent. A late Intro message
  does not push the later ones back.
- **Late Intro messages never pile up.** If the next Intro message is also already due, the late one is skipped, as
  the ninety plan does today.
- **Reply-lane waits count from when the message before actually went.**
- **Send steps:**
  - A template this contact already got is skipped, except Quick replies.
  - Engage and Demo video steps take the row for the contact's Track. An Unassigned contact on Engage gets the row
    their words point to. A Demo video waits for the Track.
  - An Offer step sends the live code for the contact's pack, and waits while no code is live.
  - A draft template holds the contact. Today shows the row with `draft: true`, as today.
- **Group.** Right after a message, its first item waits `every` days. Right after a wait, it goes at once.
- **Lanes:**
  - Interested or Signed up (or a Track set after a YES) moves the contact to `yes`. Not now moves them to `notnow`.
    These moves happen once: a second YES does not restart the lane.
  - **STOP / band karo / mat bhejo ends everything at once.** The contact sits at the STOP lane's End, and no message
    in the STOP lane is ever queued (the path's `issues` notes this).
- **reply step:**
  - An answer in a reply lane moves the contact to the next step.
  - An answer in the main lane holds them at the step until staff sort the reply (YES, Not now or STOP).
  - No answer by the end of the wait moves them to the next step.
- **Paused or draft campaigns** queue nothing. Replies still show on Today.
- **Positions are lazy.** A contact added to a path campaign (by upload, by hand, or by moving) starts at Start the
  first time Today looks at them.

## 2. Campaigns

### `GET /campaigns/`
These keys are added to each row: `engine`, `path_summary` (for a slots campaign, the path it would convert to),
`contacts`, `sent`, `replies`, `reply_rate`, `yes`, `paid`, `day` and `path`.
- `day`: the campaign's day (Day 1 = its first message to anyone), or `null`.
- `path`: the full path on a path campaign, or `null` on slots.

```json
{"id": 3, "name": "Tax season (example)", "status": "draft", "plan": "ninety", "engine": "path",
 "path_summary": {"steps": 12, "days": 85, "lanes": 3},
 "contacts": 1, "sent": 1, "replies": 0, "reply_rate": 0, "yes": 0, "paid": 0, "day": 1,
 "path": {"v": 1, "lanes": {"...": "..."}}, "stats": {"contacts": 1, "...": "..."}}
```
- `sent` counts the messages we sent.
- `replies` counts the people who wrote back.
- `reply_rate` = replies as a % of the people we messaged.
- `path_summary.steps` counts every step except Start.
- `path_summary.days` is the main lane's last planned message day.
- `path_summary.lanes` counts the lanes that have steps.

### `GET /campaigns/presets/`
Returns `[{key, name, path, summary}]` for `100_reviews`, `short` and `blank`. The paths are built from the current
templates, so a template that doesn't exist yet gives `null` and shows up as an issue.
```json
[{"key": "100_reviews", "name": "100 Reviews · 90 days", "summary": {"steps": 28, "days": 88, "lanes": 4},
  "path": {"v": 1, "lanes": {
    "main": [{"id": "s0", "k": "start"}, {"id": "m1", "k": "send", "template": 49, "only_if": ""},
             {"id": "m2", "k": "wait", "n": 3, "unit": "days", "window": true}, "... m3-m7 ...",
             {"id": "m8", "k": "group", "category": "outreach", "count": 26, "every": 3, "window": true},
             {"id": "m9", "k": "end", "label": "No reply by day 90", "note": "...", "status": "no_reply"}],
    "yes": [{"id": "y1", "k": "send", "template": 79, "only_if": ""}, {"id": "y2", "k": "reply", "n": 2, "unit": "days"},
            {"id": "y3", "k": "track"}, "... y4 demo, y5 wait 1d, y6 group demo_video 5×1d, y7 setup, y8 wait 3d,",
            "y9 setup 2 only_if no_pc, y10 offer, y11 goal paid, y12 wait 14d, y13 testimonial,",
            {"id": "y14", "k": "end", "label": "Done", "status": "done"}],
    "notnow": [{"id": "n1", "k": "send", "template": 131, "only_if": ""}, {"id": "n2", "k": "wait", "n": 30, "unit": "days", "window": true},
               {"id": "n3", "k": "goto", "lane": "main", "step": "resume"}],
    "stop": [{"id": "t1", "k": "send", "template": 128, "only_if": ""},
             {"id": "t2", "k": "end", "label": "Opted out", "note": "This number never gets a message again, in any campaign.", "status": "opted_out"}]}}},
 {"key": "short", "name": "Short · 3 messages in 8 days", "...": "..."},
 {"key": "blank", "name": "Blank", "summary": {"steps": 0, "days": 0, "lanes": 0},
  "path": {"v": 1, "lanes": {"main": [{"id": "s0", "k": "start"}], "yes": [], "notnow": [], "stop": []}}}]
```
Two things differ from the board:
- The `100_reviews` YES lane ends with `y14 end` (every lane must end in End, Goal or Go to).
- Blank lanes are `[]`, not `[{k: "empty"}]`.

### `POST /campaigns/` with `preset` (or `path`)
`{"name", "preset": "100_reviews"|"short"|"blank"}`, or `{"name", "path": {...}}`, plus any of the older fields
(`daily_limit`, `language`, `goal`, `audience`, `first`...). These are validated exactly as before.
- `engine` becomes `"path"`.
- `first` is optional: when it's missing, it is the path's first message (else the first Intro row).
- `plan` defaults to `ninety` for `100_reviews` and `touches` otherwise. The engine doesn't read `plan`. It is a hint
  for the old Today screen.
- Without `preset` or `path`, the POST makes an old-style slots campaign, as before.

```json
POST {"name": "Tax season (example)", "preset": "100_reviews", "daily_limit": 12, "language": "hi"}
201 {"id": 3, "name": "Tax season (example)", "status": "draft", "daily_limit": 12, "first": 49, "language": "hi",
     "plan": "ninety", "engine": "path", "path_summary": {"steps": 28, "days": 88, "lanes": 4},
     "contacts": 0, "sent": 0, "replies": 0, "reply_rate": 0, "yes": 0, "paid": 0, "day": null, "path": {"...": "..."}, "...": "..."}
400 {"detail": "Pick a ready path: 100_reviews, short or blank.", "path": ["Pick a ready path: 100_reviews, short or blank."]}
```

### `POST /campaigns/<id>/start/` (existing)
On a path campaign, Start also needs a path with no errors and no draft templates:
```json
400 {"detail": "Fix the path first: Intro · Value · day 7 is a draft. Write its English and Hinglish in Templates first: contacts wait here until then.",
     "issues": [{"level": "warn", "lane": "main", "step_id": "m5", "step": "m5", "text": "...", "message": "..."}]}
```

### `GET /campaigns/<id>/path/`
```json
{"campaign": 3, "engine": "path", "status": "draft", "read_only": false,
 "path": {"v": 1, "lanes": {"...": "..."}},
 "stats": {"s0": {"here": 0, "sent": 0, "replies": 0, "reply_rate": 0},
           "m1": {"here": 0, "sent": 1, "replies": 0, "reply_rate": 0},
           "m2": {"here": 1, "sent": 0, "replies": 0, "reply_rate": 0}, "...": "every step id"},
 "issues": [{"level": "warn", "lane": "main", "step_id": "m5", "step": "m5",
             "text": "Intro · Value · day 7 is a draft. Write its English and Hinglish in Templates first: contacts wait here until then.",
             "message": "(same as text)"},
            {"level": "info", "lane": "stop", "step_id": "t1", "step": "t1",
             "text": "STOP ends every campaign for the number at once, so this message is never queued.", "message": "..."}],
 "summary": {"steps": 28, "days": 88, "lanes": 4}}
```
- `stats` per step:
  - `here` = people at the step now. People who haven't started count at Start. Stopped people count at the STOP
    lane's End.
  - `sent` = messages this step sent.
  - `replies` = of those, the ones answered within 3 days.
  - `reply_rate` = replies as a % of sent.
- `issues`: `{level: error|warn|info, lane, step_id, step, text, message}`.
  - `step` = `step_id` and `message` = `text` (the UI can read either).
  - `error` refuses a save on a running campaign and stops Start.
  - A draft (`warn`) stops Start only.
- On a **slots campaign**, the response is the path it would convert to, with `"engine": "slots"`,
  `"read_only": true` and `"stats": {}`.

### `PUT /campaigns/<id>/path/` with `{"path": {...}}`
- **Always 400**: the shape is wrong, a kind is unknown, an id is used twice, a template id doesn't exist, or a wait,
  count or every is not above 0. Example:
  ```json
  400 {"detail": "Intro path, step 2: the id s0 is used twice. Every step needs its own id.",
       "errors": ["Intro path, step 2: the id s0 is used twice. Every step needs its own id."]}
  ```
- **Draft**: saved, with its `issues`. A draft can be built step by step, and an empty lane is allowed.
- **Live or paused**: refused when there is any error-level issue:
  - a lane doesn't end in end, goal or goto;
  - a lane is empty;
  - a send has no template;
  - a template is archived;
  - a goto has no target.
  ```json
  400 {"detail": "A running campaign needs a complete path: The YES path has no steps yet. Add at least an End.", "issues": ["..."]}
  ```
- On a save, contacts at a removed step move to the next step: the step after the last step before it that is still
  there. That can be a step put in its place.
  - Contacts at a step that is still there keep their place and their due day. A changed wait applies only to people
    who haven't reached it.
  - A step that kept its id but changed kind starts again for the people at it.
  - "Back to the Intro path" pointers follow the same rule.
- 200 = the GET body plus `"moved": n`.
- **A slots campaign that is live, paused or has sent messages refuses PUT** (400: convert it with
  `manage.py wa_paths --convert`). A slots draft with no messages switches to `engine: "path"` on its first PUT.

### `GET /campaigns/<id>/schedule/?contact=<id>`
Every message from the contact's step on, if each goes on its due day (one a day) and nobody replies. It uses the
real window. Without `?contact`, it is someone who joins today.
- `date` is `"YYYY-MM-DD"`.
- `day` is Day N of the main lane, or the days since they entered a reply lane.
- `moved_from` is `"Sun 15 Nov"` or `null`.

The list stops at a hold (Track, goal, an answer) or an end.
```json
[{"step_id": "m1", "lane": "main", "day": 1, "date": "2026-11-12", "moved_from": null, "template_id": 49,
  "template": "Intro · Opener · day 1", "label": "Day 1 · Opener"},
 {"step_id": "m5", "lane": "main", "day": 5, "date": "2026-11-16", "moved_from": "Sun 15 Nov", "template_id": 51,
  "template": "Intro · Value · day 7", "label": "Day 5 · Value"},
 {"step_id": "m8", "lane": "main", "day": 10, "date": "2026-11-21", "moved_from": null, "template_id": 53,
  "template": "Intro · Problem · day 13", "label": "Day 10 · Problem"}]
```
In this example m3 and m4 had just been removed, so Day 4 is gone and Sunday 15 moved to Monday 16. `?contact=x`
gives 400 `{"contact": ["Pick a contact."]}`. On a slots campaign the list is for the path it would convert to.

## 3. Today, contacts and analytics

### `GET /today/`: queue rows
These keys are added to every row: `step_id`, `step_label` and `lane`. They are `""` on slots rows. A path row is
always `kind: "flow"`, with `template_id` set, or `note: "set_track"` and `template_id: null` (the same as a ninety
row).
```json
{"kind": "flow", "template": "Intro · Opener · day 1", "template_id": 49, "label": "Intro · Opener · day 1",
 "category": "outreach", "draft": false, "note": "", "version": "",
 "step_id": "m1", "step_label": "Day 1 · Opener", "lane": "main", "contact": {"id": 1, "...": "..."}}
```
Slots row (unchanged, plus the blank keys):
```json
{"kind": "flow", "template": "Demo video · Feature clip · Team Activity Monitoring · step 2", "template_id": 104,
 "label": "Demo video · Feature clip · Team Activity Monitoring · step 2", "step_id": "", "step_label": "", "lane": "", "...": "..."}
```
- `step_label`: `"Day N · <type>"` in the main lane (the plan's day for that message), or `"YES · <type>"`,
  `"Not now · <type>"` in a reply lane.
- `label`: for an older (non-category) template on a path it is the template's name, so the current Today screen
  passes `template` on Mark as sent.
- Order: reply-lane rows first (no daily limit), then main-lane rows by due date, within the daily limit.

**Actions are unchanged** (`/contacts/<id>/draft/`, `/sent/`, `/reply/`, `/act/`). On a path contact:
- `draft {kind: "flow"}` (or any old cold kind) with no template drafts the path's due message.
- `sent` with the step's template (or its version B or twin, or with no template for `kind: "flow"`) completes the
  step. Any other template is recorded and doesn't move the path.
- A Quick reply sent from Replies completes a Quick-reply step (for example Not now's `n1`).
- `act interested|signed|notnow|stop` and a STOP reply move the lane.

The old sequence fields (`step`, `next_date`, `out_day`, `engage_step`...) are still written on a path send, so
`--revert` finds the contact where the slots code expects them.

### Contact JSON (`/contacts/`, `/contacts/<id>/`, `contact` in Today rows)
Added keys: `engine`, `lane`, `step_id`, `step_label` and `next_due` (`"YYYY-MM-DD"` or `null`; on slots campaigns it
is `next_date`). `lane` and `step_id` are read only.
```json
{"id": 1, "campaign": 3, "engine": "path", "lane": "main", "step_id": "m2", "step_label": "Intro · Wait 3 days",
 "next_due": "2026-11-16", "plan": "ninety", "...": "..."}
```

### `GET /analytics/steps/?campaign=<id>`
```json
{"campaign": 3, "name": "Tax season (example)", "engine": "path", "steps": [
  {"step_id": "s0", "lane": "main", "k": "start", "label": "Start", "here": 0, "sent": 0, "replies": 0, "reply_rate": 0},
  {"step_id": "m1", "lane": "main", "k": "send", "label": "Day 1 · Opener", "here": 0, "sent": 1, "replies": 0, "reply_rate": 0},
  {"step_id": "m2", "lane": "main", "k": "wait", "label": "Wait 3 days", "here": 1, "sent": 0, "replies": 0, "reply_rate": 0},
  {"step_id": "m5", "lane": "main", "k": "send", "label": "Day 4 · Value", "here": 0, "sent": 0, "replies": 0, "reply_rate": 0}]}
```
- The steps are listed in path order: main, yes, notnow, stop.
- Without `?campaign` the response is a 400.
- On a slots campaign, it lists the steps of the path it would convert to, with zeros.
- After `--convert`, earlier messages are tagged with the step that sends that template now.
- wa4 (12 Nov 2026): every step also has `went_on` (messages after which the contact took the step the message is
  for: said YES after an Intro, set a Track after Engage, paid after an Offer). Optional `&period=all|30|7`: with 30
  or 7, `sent`, `replies`, `reply_rate` and `went_on` count only the messages sent in that period; `here` is always
  now. The response adds `"period"`. A bad period is a 400.

### `GET /analytics/view/?campaign=<id>|all&period=all|30|7` (wa4, the Analytics tab)
Both parameters are optional (`all` and `all`). Counting rules are in `z_marketing/wa_analytics.py`; in short:
people = those whose first message from the campaign went out in the period (what they did next counts to today);
a reply = they wrote within 3 days of one of our messages; the splits by template, step, slot and code count the
messages sent in the period. `GET /analytics/` is unchanged. Example from the local DB (fake sample data, lists cut
to one row):
```json
{"campaign": 3, "period": "7", "since": "2026-11-06", "today": "2026-11-12",
 "kpis": {"people": 25, "messages": 88, "replied": 8, "yes": 4, "track": 2, "first_pc": 2, "paid": 2, "paid_pcs": 20, "stopped": 0},
 "funnel": [{"key": "messaged", "label": "Messaged", "people": 25}, {"key": "replied", "label": "Replied", "people": 8},
            {"key": "yes", "label": "Said YES", "people": 4}, {"key": "track", "label": "Track set", "people": 2},
            {"key": "first_pc", "label": "First PC", "people": 2}, {"key": "paid", "label": "Paid", "people": 2}],
 "days": [{"date": "2026-11-12", "sent": 7, "replies": 0}],
 "campaigns": [{"id": 3, "name": "Diwali to Republic Day (sample)", "status": "live", "engine": "path", "people": 25, "replied": 8}],
 "steps": [{"step_id": "m1", "lane": "main", "k": "send", "label": "Day 1 · Opener", "title": "Opener", "day": 1, "here": 14,
            "sent": 25, "replies": 7, "reply_rate": 28, "went_on": 4}],
 "templates": [{"id": 80, "name": "Intro · Question · day 10", "category": "outreach", "type_name": "Question", "version": "A",
                "version_of": null, "archived": false, "sends": 8, "replies": 3, "reply_rate": 38, "went_on": 1, "languages": ["en", "hi"]}],
 "segments": [{"segment": "S1 · CA firms", "cities": ["Jaipur", "Delhi"], "people": 12, "replied": 5, "yes": 3, "paid": 2}],
 "languages": [{"language": "hi", "label": "Hinglish", "people": 19, "messages": 38, "replied": 7, "yes": 3, "stopped": 0}],
 "slots": [{"slot": "10-12", "label": "10–12", "name": "Morning", "sent": 31, "replied": 11, "same_day": 4, "first_reply_secs": 74362}],
 "codes": [{"code": "ZWTESTCODE1", "percent_off": 15, "status": "live", "pack": "Startup pack", "stage": 1, "event": false,
            "starts_on": "2026-11-01", "ends_on": "2027-02-12", "shown_to": 1, "replied": 0, "used": 1, "pcs_paid": 10}]}
```
- `kpis.paid_pcs`: the PCs the plan behind each paid person's pack covers (Startup pack = `startups`, Team pack =
  `teams`, `Plan.max_devices`). A WhatsApp contact isn't linked to a company, so this is the pack, not a device count.
- `steps`: message steps only (`send`, `group`), in path order; people waiting after a message count in its `here`.
  `null` for all campaigns and for a campaign on the old slots engine.
- `slots`: India time 10–12, 12–14, 14–17, 17–19, plus `other` only when something went out outside them.
  `same_day` = replied the same India date; `first_reply_secs` averages over the messages that got a reply.
- `codes`: every code a WhatsApp template carries, plus WhatsApp-channel codes. `shown_to` and `replied` are people who
  got a message carrying it (in this campaign and period); `used` = the code's pending or paid uses in the period from
  any channel (a use belongs to a company, not a contact); `pcs_paid` = `max_devices` of the plans of the paid uses.
- `?period=` other than `all`, `30`, `7`, or a `campaign` that isn't a number or `all`, is a 400; an unknown id is a 404.

## 4. Converting the old campaigns: `manage.py wa_paths`
```
manage.py wa_paths --check [--campaign <id>] [-v 2]    read-only; exits 1 if there is any difference
manage.py wa_paths --convert [--campaign <id>]          switches only the campaigns with 0 differences
manage.py wa_paths --revert <id>                        back on slots (the path and positions are kept, unused)
```
- **ninety** converts to the 100 Reviews path as it runs today:
  - The Intro rows by day: the first four as steps, the rest as a group when they are three days apart, otherwise
    every one as a step. No Mon–Sat window, so every due day stays what it is.
  - YES: Engage ×2 (lean on their words) → set the Track → Engage ×4 → Demo videos ×6 → Setup 1 → 3 days → Setup 2 if
    no PC → Offer → goal first PC → 14 days → Testimonial → End.
  - Not now: quick reply → 30 days → back to the Intro path.
  - STOP: End.
- **touches** converts to the Short path made of the campaign's own messages: first → wait for an answer (followup
  days) → follow-up → wait for an answer → touch 3 → [touch 4] → End (no reply). YES: the after-YES messages by day,
  `only_if: "track:<t>"`. Not now: End.
- For each contact, the command compares the next message and its due day under both engines. One message a day, the
  daily limit, needs_reply and the 30-day pause are gates both engines share, so they are not compared.
- A paused or draft ninety campaign with YES contacts counts as a difference: slots still sends their YES path, and a
  path campaign doesn't.

## 5. Settings: `GET` / `PUT /api/admin/marketing/wa/settings/` (wa4, 12 Nov 2026)
Staff only. One row (`WaSettings` pk 1, migration `0027_wa_settings`); until staff save it, GET returns the defaults,
which are what the code did before. PUT takes any of the fields (the rest stay) and returns the saved settings, or a
400 `{field: message}` with everything to fix; nothing is half-saved. Live campaigns follow a change from their next
send. Logic and the list of where each field is used: `z_marketing/wa_settings.py`.
```json
{"send_days": [0, 1, 2, 3, 4, 5], "send_from": "10:00", "send_to": "19:00", "skip_holidays": false,
 "new_per_day": 15, "per_number_day": 120, "per_person_day": 1, "min_intro_wait": 1, "max_days": 90,
 "slots": ["10-12", "12-14", "14-17", "17-19"], "stop_words": ["STOP", "unsubscribe", "band karo", "mat bhejo"],
 "stop_pause_pct": 0, "language": "en", "switch_en": false, "claude_read": true, "claude_draft": true,
 "claude_track": true, "wa_number": "", "wa_name": "", "discount_max": 40, "updated_at": null, "updated_by": ""}
```
- `send_days`: 0 = Monday … 6 = Sunday, at least one. `send_from` < `send_to`, "HH:MM", India time.
- `new_per_day` 1–50, `per_number_day` 1–500, `per_person_day` 1 | 2, `min_intro_wait` 1 | 2 | 3 days,
  `max_days` 30 | 60 | 90, `stop_pause_pct` 0 (never) | 2 | 3 | 5, `discount_max` 1–50, `language` en | hi | ab.
- `stop_words`: STOP always stays; at most 30, each under 40 letters; repeats (any case) are dropped.
- `slots`, `switch_en`, `claude_read`, `claude_track`, `wa_number`, `wa_name` are stored for the UI; the engine
  doesn't read them yet.

## 6. Today (wa4, 12 Nov 2026): Claude's read, the choices and send slots

Every key below is **added**; nothing changed shape. Code: `z_marketing/wa_today.py`.

### `WaCampaign.send_slot` (migration 0027)
`"10"` (10–12, the default) · `"12"` (12–14) · `"14"` (14–17) · `"17"` (17–19), India time, Mon–Sat. In `GET/PATCH
/campaigns/` as `send_slot`; anything else is a 400. A path `send` step may carry its own `"slot"` (the builder already
round-trips it), which wins for that step.

### `GET /today/`
- Each **queue** row adds `slot` and `after_yes`:
  - `after_yes: true` = a step for someone who already replied (YES path, Not now path, after-YES, Set the Track). These
    go any time in the window, so `slot` is `""`.
  - Otherwise `slot` is the step's own slot or the campaign's `send_slot`.
- Top level adds:
  - `slots`: `{now, now_label, open, sunday, next, next_label, next_at, list: [{key, label, start}]}`. Before 10:00
    `now` is `"10"`; after 17:00 it is `"17"`, so whatever is left counts as due now.
  - `day`: `{sent, limit, replies, yes}`. `sent` = messages counted against the daily limit today (`sent_today`),
    `limit` = the live campaigns' daily limits added up, `replies` = people who wrote today, `yes` = people who said
    YES today.
  - `sent_list`: every message marked as sent today, newest first:
    `{at, contact, name, what, replied_at, blocked}` (`replied_at` = their first message after it today, or null).
- Each **reply** row adds:
  - `suggest`: `{kind, track, why, source}`. `kind`: `yes` · `question` · `notnow` · `stop` · `track`. `track` only
    when kind is `track` (`privacy` · `activity` · `both`, or `""` when unclear). `source`: `rules` or `claude`.
  - `sorts`: the buttons, in order: `["yes", "question", "notnow", "stop"]`, or `["privacy", "activity", "both"]` when
    they already said YES and the Track is Unassigned (ninety or a path contact in the YES lane).
  - `effect`: one line per sort, worked out by running the engine on a copy of the contact (nothing is saved).
  - `answers`: the template each answer starts from (id, or null: draft by `kind` instead). Drafts are never picked.

```json
{"id": 5, "firm": "Mehta & Co CA", "...": "...",
 "suggest": {"kind": "yes", "track": "", "why": "asks for the video and names the problem", "source": "rules"},
 "sorts": ["yes", "question", "notnow", "stop"],
 "effect": {"yes": "YES moves Rohit to the YES path: Engage · Need discovery goes now, then Zinwe waits up to 2 days for the answer.",
            "question": "A question doesn't move them. Rohit stays on Intro; the next message still goes on Thu 19 Nov.",
            "notnow": "Not now moves them to the Not now path: this reply, a 30-day wait, then back to Intro at the next message.",
            "stop": "STOP ends every campaign for this number. Send the confirmation, then the number is blocked for good."},
 "answers": {"yes": 79, "question": null, "notnow": 131, "stop": 128}}
```

**Confirming** is the existing calls, in this order: `act` (`interested` / `notnow` / `track` with the Track in
`note`; nothing for a question), then `sent {kind: "reply", text, template: answers[sort]}`. STOP: `sent` the
confirmation, then `act stop`.

**Claude.** Rules first (English and Hinglish keywords, `wa_flow.suggest_track`). With `ANTHROPIC_API_KEY` set, Today
asks Claude (`ZINWE_WA_MODEL`, low effort, JSON schema output) about up to 4 new replies per load, in parallel. The
read is kept on the message (`WaMessage.read`, migration 0027), so each message is read once; a failed call keeps the
rules' read and isn't retried. Claude never sends anything.

### `POST /contacts/<id>/draft/` adds `track`
`{kind, template, track: "privacy"}` drafts as if the Unassigned Track were set, so Today can show the first Demo video
of the Track staff are about to confirm. Nothing is saved.

## 7. The Contacts tab (wa4, 12 Nov 2026: `z_marketing/wa_contact_view.py`)

Read only: nothing here moves a contact or sends a message. Path campaigns are walked with a simulation (never saved);
slots campaigns get what their own rules say, and a blank where nothing is known.

### Contact JSON: added keys
`where`, `need`, `next`, `last_message`, `language` on `/contacts/`, `/contacts/<id>/` and `contact` in Today rows.
```json
{"where": {"key": "intro", "label": "Intro · day 34", "sub": "No reply yet"},
 "need": "",
 "next": {"state": "due", "label": "Intro · Problem · day 34", "when": "2026-11-12", "slot": "", "send_kind": "flow"},
 "last_message": {"text": "Namaste Verma & Verma CA team, ...", "at": "2026-11-10T05:35:00+00:00", "direction": "out", "by": "admin@zinwe.local"},
 "language": "hi"}
```
- `where.key` (one per contact, in this order): `stop` (STOP or opted out) · `locked` (not opted in) · `paid` ·
  `yes` (said YES) · `notnow` · `replied` (wrote back, not sorted into YES, Not now or STOP) · `intro` (in a campaign)
  · `none` (no campaign).
- `need`: `reply` (a reply waiting) · `track` (said YES on a campaign with a Track, Track not set) · `optin` · `""`.
- `next.state`: `due` (today) · `planned` (on `when`) · `hold` (waits: `label` says for what, e.g. "Set the Track",
  "Goal · Paid") · `reply` (answer on Today) · `paused` (`when` = the contact's pause end, or `null` for a paused or
  draft campaign) · `never` (stopped) · `locked` · `done` (the path ended) · `none` (no campaign).
  `slot` is the step's `slot` when the path has one, else `""`. `send_kind` is the `kind` for `/draft/` and `/sent/`
  when the next message is a template (`flow`, `first`, `followup`, `touch3`, `touch4`, `nurture`), else `""`.
- `last_message.direction`: `in` (they wrote) or `out` (we sent). `null` before any message.
- `language`: `en` or `hi`, the language they get cold messages in (`whatsapp.language_for`).

### `GET /contacts/counts/`
Counts for the whole list (the menu), by the same rules as the keys above.
```json
{"all": 16, "added_week": 11, "need": 6, "need_by": {"reply": 4, "track": 1, "optin": 1},
 "where": {"intro": 6, "replied": 1, "yes": 3, "notnow": 1, "paid": 1, "stop": 2, "locked": 1, "none": 1},
 "tracks": {"privacy": 0, "activity": 2, "both": 0, "unset": 1},
 "segments": [{"segment": "S1", "count": 5, "cities": ["Jaipur", "Delhi"]}],
 "campaigns": {"5": 9, "2": 3, "1": 3}, "no_campaign": 1, "languages": {"en": 13, "hi": 3}}
```
`tracks.unset` = said YES, Track not set (the `track` need, whatever else they need).

### `GET /contacts/<id>/path/`
One contact's path for the contact panel: what went (`done`), where they are (`now`), the next messages (`next`, up to
8, the rest in `more`). Dates are `YYYY-MM-DD` or `null`.
```json
{"campaign": 5, "campaign_name": "Diwali to Republic Day", "campaign_status": "live", "engine": "path", "day": 2,
 "next": {"state": "planned", "label": "Intro · Problem · day 4", "when": "2026-11-19", "slot": "", "send_kind": "flow"},
 "steps": [{"state": "done", "label": "Added to Diwali to Republic Day", "date": "2026-11-16", "note": "", "kind": "event"},
           {"state": "done", "label": "Intro · Opener · day 1", "date": "2026-11-16", "note": ""},
           {"state": "now", "label": "Wait 3 days", "date": null, "note": ""},
           {"state": "next", "label": "Intro · Problem · day 4", "date": "2026-11-19", "note": ""},
           {"state": "next", "label": "Intro · Value · day 7", "date": "2026-11-23", "note": "moved from Sun 22 Nov"}],
 "more": {"count": 21, "last_date": "2027-02-10", "last_day": 88}}
```
- `kind`: `message` (one of our messages, sent or coming), `event` (added, Said YES, the Track, the first PC, paid) or
  `step` (where they are: a wait, a hold, the end).
- Done rows: our campaign messages and quick replies (one-offs and plain replies are in the conversation), Said YES,
  the Track, First PC set up, Goal · Paid.
- Now: the wait they are in, what they wait for ("Set the Track", "Sort their reply on Today"), "Blocked in every
  campaign" (note = the block's reason), "Waiting for their opt-in", or the first coming message.
- A slots contact's next messages come from the path its campaign would convert to (as `/schedule/` does).
