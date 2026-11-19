"""Follow-up agent (after the cold email): reply -> calendar -> propose times -> (counter-proposal loop) -> book -> CRM.

The SAME six-field state object is snapshotted at every checkpoint, so a reviewer can watch it fill in:

    reply_status      no_reply | interested | not_interested | unclear
    calendar_checked  bool        (reset to False when the prospect counter-proposes: the loop must re-check)
    times_proposed    list[str]   (the slots we offered)
    reply_round       int         (counter-proposal rounds; hard cap -> human handoff)
    meeting_status    unscheduled | pending_confirmation | confirmed | declined | needs_human
    crm_logged        bool

Tools (numbered as in the workflow): 1 send_email, 2 check_calendar, 3 send_email (proposing times),
4 book_meeting, 5 update_crm. Classifying the reply is a branch, not a tool call.

Calendar and CRM are DEMO systems (deterministic, in-memory/SQLite): no real calendar or CRM is connected in this
build, and the UI labels them as such. Emails produced here go only to the demo inbox like every other email.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field

MAX_REPLY_ROUNDS = 2          # counter-proposal loop cap; beyond this a human takes over
TZ_LABEL = "IST"
WORK_HOURS = (time(10, 0), time(14, 0), time(11, 0), time(15, 0), time(16, 0))   # preference order


class FollowupState(BaseModel):
    reply_status: Literal["no_reply", "interested", "not_interested", "unclear"] = "no_reply"
    calendar_checked: bool = False
    times_proposed: list[str] = Field(default_factory=list)
    reply_round: int = 0
    meeting_status: Literal["unscheduled", "pending_confirmation", "confirmed", "declined",
                            "needs_human"] = "unscheduled"
    crm_logged: bool = False


FIELDS = list(FollowupState.model_fields)


@dataclass
class Checkpoint:
    n: int
    title: str
    subtitle: str
    tool: Optional[str]                 # None = branch (no tool call)
    state: dict
    changed: list[str]
    detail: str = ""


@dataclass
class FollowupRun:
    scenario: str
    checkpoints: list[Checkpoint] = field(default_factory=list)
    emails: list[dict] = field(default_factory=list)
    calendar: dict = field(default_factory=dict)
    crm_record: Optional[dict] = None
    classified_reply: Optional[dict] = None
    booked_slot: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def final_state(self) -> dict:
        return self.checkpoints[-1].state if self.checkpoints else FollowupState().model_dump()


# ------------------------------------------------------------------ demo calendar
def _fmt(dt: datetime) -> str:
    h = dt.strftime("%I").lstrip("0")
    m = "" if dt.minute == 0 else dt.strftime(":%M")
    return f"{dt.strftime('%b')} {dt.day} {h}{m}{dt.strftime('%p').lower()}"


class DemoCalendar:
    """Deterministic demo calendar: recurring busy blocks + a few per-company busy slots (seeded by domain)."""

    def __init__(self, domain: str, today: date):
        self.today = today
        self.domain = domain
        seed = int(hashlib.sha256(domain.encode()).hexdigest(), 16)
        self.busy: set[datetime] = set()
        for i in range(1, 15):
            d = today + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            self.busy.add(datetime.combine(d, time(9, 0)))        # daily stand-up
            self.busy.add(datetime.combine(d, time(13, 0)))       # lunch
            if (seed >> i) & 1:                                    # company-specific conflicts
                self.busy.add(datetime.combine(d, WORK_HOURS[(seed >> (i + 3)) % len(WORK_HOURS)]))
        self.booked: list[dict] = []

    def is_free(self, dt: datetime) -> bool:
        return dt.weekday() < 5 and dt not in self.busy and all(b["start"] != dt for b in self.booked)

    def free_slots(self, n: int = 2) -> list[datetime]:
        """Next n free slots, at most one per business day, starting tomorrow."""
        out: list[datetime] = []
        d = self.today + timedelta(days=1)
        while len(out) < n and d <= self.today + timedelta(days=14):
            if d.weekday() < 5:
                # alternate afternoon / morning first, so the two offers give the prospect a real choice
                order = (time(14, 0), time(10, 0), time(15, 0), time(11, 0), time(16, 0)) if len(out) % 2 == 0 \
                    else WORK_HOURS
                for t in order:
                    dt = datetime.combine(d, t)
                    if self.is_free(dt):
                        out.append(dt)
                        break
            d += timedelta(days=1)
        return out

    def book(self, dt: datetime, title: str) -> dict:
        ev = {"start": dt, "end": dt + timedelta(minutes=15), "title": title,
              "event_id": "demo-" + hashlib.sha1(f"{self.domain}{dt}".encode()).hexdigest()[:10]}
        self.booked.append(ev)
        return ev


# ------------------------------------------------------------------ reply classification (branch, no tool call)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NOT_INTERESTED_RE = re.compile(r"\b(not interested|no thanks|no, thanks|unsubscribe|remove me|not a priority|"
                               r"not right now|stop emailing|we('re| are) (all )?set|already (use|have|working with))",
                               re.I)
COUNTER_RE = re.compile(r"\b(instead|how about|what about|can we do|could we do|works better|rather|"
                        r"another time|different time|not available|can't make|cannot make)\b", re.I)
ACCEPT_RE = re.compile(r"\b(yes|sure|sounds good|interested|happy to|works for me|that works|let'?s (do|talk|chat)|"
                       r"book it|first (one|slot|option)|second (one|slot|option)|perfect|great)\b", re.I)
TIME_RE = re.compile(r"\b(" + "|".join(WEEKDAYS) + r"|tomorrow)\b(?:[^0-9]{0,12}(\d{1,2})(?::(\d{2}))?\s*(am|pm))?",
                     re.I)


def parse_proposed_time(text: str, today: date) -> Optional[datetime]:
    m = TIME_RE.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word == "tomorrow":
        d = today + timedelta(days=1)
    else:
        target = WEEKDAYS.index(word)
        ahead = (target - today.weekday()) % 7 or 7
        d = today + timedelta(days=ahead)
    hour = int(m.group(2)) if m.group(2) else 11
    minute = int(m.group(3)) if m.group(3) else 0
    ampm = (m.group(4) or "am").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime.combine(d, time(hour, minute))


def classify_reply(text: str, today: date) -> dict:
    """Rule-based (deterministic, auditable). Returns {"intent", "proposed_time", "evidence"}."""
    t = (text or "").strip()
    if not t:
        return {"intent": "no_reply", "proposed_time": None, "evidence": "empty reply"}
    m = NOT_INTERESTED_RE.search(t)
    if m:
        return {"intent": "not_interested", "proposed_time": None, "evidence": m.group(0)}
    proposed = parse_proposed_time(t, today)
    c = COUNTER_RE.search(t)
    if proposed and (c or not ACCEPT_RE.search(t)):
        return {"intent": "counter_propose", "proposed_time": proposed,
                "evidence": (c.group(0) if c else "a specific time") + f" -> {_fmt(proposed)}"}
    a = ACCEPT_RE.search(t)
    if a:
        slot = 1 if re.search(r"second|2nd|latter", t, re.I) else 0
        return {"intent": "accept", "proposed_time": None, "slot_index": slot, "evidence": a.group(0)}
    return {"intent": "unclear", "proposed_time": None, "evidence": "no clear intent found"}


# ------------------------------------------------------------------ the agent
SCENARIOS = {
    "accepts": "Prospect is interested and accepts the first proposed time",
    "counter_proposes": "Prospect is interested, then counter-proposes another time (loop back to the calendar)",
    "not_interested": "Prospect replies that they are not interested",
    "no_reply": "Prospect does not reply",
}


class FollowupAgent:
    def __init__(self, domain: str, company: str, contact_name: str, contact_title: str, today: date,
                 sender: str = "Vikram"):
        self.domain, self.company, self.contact, self.title = domain, company, contact_name, contact_title
        self.first = (contact_name or "there").split()[0]
        self.today = today
        self.sender = sender
        self.cal = DemoCalendar(domain, today)
        self.state = FollowupState()
        self.run: Optional[FollowupRun] = None
        self._slots: list[datetime] = []

    # -- checkpoint helper: snapshot the six fields and record what changed
    def _checkpoint(self, title: str, subtitle: str, tool: Optional[str], detail: str = "") -> None:
        prev = self.run.checkpoints[-1].state if self.run.checkpoints else None
        snap = self.state.model_dump()
        changed = [f for f in FIELDS if prev is None or prev[f] != snap[f]] if prev else []
        self.run.checkpoints.append(Checkpoint(len(self.run.checkpoints) + 1, title, subtitle, tool, snap,
                                               changed, detail))

    # -- tools
    def tool_send_email(self, kind: str, subject: str, body: str) -> dict:
        email = {"kind": kind, "to": "demo inbox only", "intended_recipient": f"{self.contact}, {self.title}",
                 "subject": subject, "body": body}
        self.run.emails.append(email)
        return email

    def tool_check_calendar(self, specific: Optional[datetime] = None) -> dict:
        self.state.calendar_checked = True
        if specific is not None:
            free = self.cal.is_free(specific)
            res = {"checked": _fmt(specific) + f" {TZ_LABEL}", "free": free}
        else:
            self._slots = self.cal.free_slots(2)
            res = {"free_slots": [_fmt(s) + f" {TZ_LABEL}" for s in self._slots]}
        self.run.calendar.setdefault("checks", []).append(res)
        return res

    def tool_book_meeting(self, dt: datetime) -> dict:
        ev = self.cal.book(dt, f"Sprinto x {self.company}: 15-minute intro ({self.contact})")
        self.state.meeting_status = "confirmed"
        self.run.booked_slot = _fmt(dt) + f" {TZ_LABEL}"
        self.run.calendar["booked"] = {"event_id": ev["event_id"], "start": self.run.booked_slot, "minutes": 15,
                                       "title": ev["title"]}
        return ev

    def tool_update_crm(self, outcome: str) -> dict:
        self.state.crm_logged = True
        rec = {"system": "demo CRM", "company": self.company, "domain": self.domain, "contact": self.contact,
               "title": self.title, "stage": {"confirmed": "Meeting booked", "declined": "Closed - not interested",
                                              "needs_human": "Needs human follow-up"}.get(self.state.meeting_status,
                                                                                        "Contacted"),
               "outcome": outcome, "meeting": self.run.booked_slot, "reply_rounds": self.state.reply_round,
               "logged_at": datetime.combine(self.today, time(18, 0)).isoformat()}
        self.run.crm_record = rec
        return rec

    # -- emails (templated, short, demo inbox)
    def _propose_body(self, slots: list[datetime]) -> str:
        opts = " or ".join(_fmt(s) + f" {TZ_LABEL}" for s in slots)
        return (f"Hi {self.first},\n\nThanks for getting back to me. Would {opts} work for a 15-minute call? "
                f"Happy to fit around your calendar if neither suits.\n\nBest,\n{self.sender}")

    def _confirm_body(self, slot: str) -> str:
        return (f"Hi {self.first},\n\nConfirmed for {slot}. I'll send a calendar invite with a short agenda: your "
                f"audit timeline, where evidence collection sits today, and whether Sprinto fits.\n\nBest,\n"
                f"{self.sender}")

    # -- the workflow
    def execute(self, scenario: str = "accepts", reply_text: Optional[str] = None,
                counter_text: Optional[str] = None) -> FollowupRun:
        self.run = FollowupRun(scenario=scenario)
        # 1. cold email already sent by the pipeline (Sequence 1, Tool 1)
        self.tool_send_email("cold_email", f"(the approved cold email to {self.first})", "(see the draft above)")
        self._checkpoint("After Send Email", "Sequence 1, Tool 1", "send_email",
                         "Cold email delivered to the demo inbox; the agent waits for a reply.")

        # 2. prospect reply -> branch (no tool call)
        if reply_text is not None:
            cls = classify_reply(reply_text, self.today)
            self.run.classified_reply = {k: (_fmt(v) + f" {TZ_LABEL}" if isinstance(v, datetime) else v)
                                         for k, v in cls.items()}
            intent = cls["intent"]
            scenario = {"accept": "accepts", "counter_propose": "accepts", "not_interested": "not_interested",
                        "no_reply": "no_reply", "unclear": "unclear"}[intent]
            self.run.scenario = f"reply text -> {intent}"
        else:
            intent = None

        if scenario == "no_reply":
            self.state.meeting_status = "unscheduled"
            self.tool_update_crm("No reply yet; follow-up nudge due in 3 business days")
            self._checkpoint("No reply (after the wait window)", "Tool 5: log and schedule a nudge", "update_crm",
                             "No reply: logged in the CRM with a nudge reminder; nothing else is sent automatically.")
            return self.run
        if scenario == "not_interested":
            self.state.reply_status = "not_interested"
            self._checkpoint('After prospect replies "Not interested"', "branch fires, no tool call", None,
                             "Branch: stop the sequence. No calendar check, no further email.")
            self.state.meeting_status = "declined"
            self.tool_update_crm("Prospect not interested")
            self._checkpoint("After Update CRM", "Tool 5", "update_crm", "Closed in the CRM; no further contact.")
            return self.run
        if scenario == "unclear":
            self.state.reply_status = "unclear"
            self.state.meeting_status = "needs_human"
            self._checkpoint("After prospect replies (intent unclear)", "branch fires, no tool call", None,
                             "The reply is ambiguous, so a human reads it. The agent does not guess.")
            self.tool_update_crm("Reply needs a human read")
            self._checkpoint("After Update CRM", "Tool 5", "update_crm", "Routed to the human review queue.")
            return self.run

        self.state.reply_status = "interested"
        self._checkpoint('After prospect replies "Interested"', "branch fires, no tool call", None,
                         "Branch: interested, so the scheduling sequence starts.")

        # 3. check calendar (Sequence 2, Tool 2)
        res = self.tool_check_calendar()
        self._checkpoint("After Check Calendar", "Sequence 2, Tool 2", "check_calendar",
                         "Free slots found: " + ", ".join(res["free_slots"]))

        # 4. send email proposing times (Tool 3)
        self.state.times_proposed = [_fmt(s) for s in self._slots]
        self.state.meeting_status = "pending_confirmation"
        self.tool_send_email("propose_times", f"Times for a quick call, {self.first}", self._propose_body(self._slots))
        self._checkpoint("After Send Email, proposing times", "Tool 3", "send_email",
                         "Two slots offered; waiting for the prospect to pick one.")

        # 5. counter-proposal loop (back to Check Calendar), capped
        chosen: Optional[datetime] = None
        counter_time: Optional[datetime] = None
        if intent == "counter_propose":
            counter_time = cls["proposed_time"]
        elif scenario == "counter_proposes":
            if counter_text:
                c = classify_reply(counter_text, self.today)
                self.run.classified_reply = {k: (_fmt(v) + f" {TZ_LABEL}" if isinstance(v, datetime) else v)
                                             for k, v in c.items()}
                counter_time = c.get("proposed_time")
            if counter_time is None:   # default demo counter: the next business day after our last offer, 11am
                d = self._slots[-1].date() + timedelta(days=1)
                while d.weekday() >= 5:
                    d += timedelta(days=1)
                counter_time = datetime.combine(d, time(11, 0))
        while counter_time is not None:
            self.state.reply_round += 1
            self.state.calendar_checked = False          # the new time has not been checked yet
            self._checkpoint("If prospect counter-proposes", "loop back to Check Calendar", None,
                             f"Prospect asked for {_fmt(counter_time)} {TZ_LABEL}; re-checking the calendar.")
            if self.state.reply_round > MAX_REPLY_ROUNDS:
                self.state.meeting_status = "needs_human"
                self.tool_update_crm("Too many reschedules; handed to a human")
                self._checkpoint("After Update CRM", "Tool 5", "update_crm",
                                 f"Loop cap ({MAX_REPLY_ROUNDS} rounds) reached: a human takes over scheduling.")
                return self.run
            check = self.tool_check_calendar(counter_time)
            if check["free"]:
                chosen = counter_time
                counter_time = None
            else:                                        # their time clashes: offer the next free slots again
                self._slots = self.cal.free_slots(2)
                self.state.times_proposed = [_fmt(s) for s in self._slots]
                self.tool_send_email("propose_times", f"Another option, {self.first}", self._propose_body(self._slots))
                self._checkpoint("After Send Email, proposing new times", "Tool 3 (their time clashed)", "send_email",
                                 f"{_fmt(check and counter_time)} {TZ_LABEL} is busy; offered new slots.")
                counter_time = None if scenario != "counter_proposes" else None
                chosen = self._slots[0]                  # demo: prospect accepts the first new slot
        if chosen is None:
            idx = (cls.get("slot_index", 0) if intent == "accept" else 0)
            chosen = self._slots[min(idx, len(self._slots) - 1)]

        # 6. book meeting (Tool 4) - the booking re-confirms availability
        self.state.calendar_checked = True
        self.tool_book_meeting(chosen)
        self.tool_send_email("confirmation", f"Confirmed: {self.run.booked_slot}", self._confirm_body(self.run.booked_slot))
        self._checkpoint("After Book Meeting", "Tool 4", "book_meeting",
                         f"Booked {self.run.booked_slot} (15 min) on the demo calendar; confirmation drafted.")

        # 7. update CRM (Tool 5)
        self.tool_update_crm("Meeting booked")
        self._checkpoint("After Update CRM", "Tool 5", "update_crm", "Deal stage set to 'Meeting booked'.")
        return self.run


def run_followup(outcome: dict, scenario: str = "accepts", reply_text: Optional[str] = None,
                 counter_text: Optional[str] = None, today: Optional[date] = None, sender: str = "Vikram") -> dict:
    """Entry point used by the API. `outcome` is a pipeline outcome (needs a chosen contact)."""
    contact = ((outcome.get("contact") or {}).get("chosen")) or {}
    if not contact.get("name"):
        raise ValueError("this company has no chosen contact, so there is nobody to follow up with")
    agent = FollowupAgent(outcome.get("domain", ""), outcome.get("company_name") or outcome.get("domain", ""),
                          contact.get("name", ""), contact.get("position") or "", today or date.today(), sender)
    run = agent.execute(scenario, reply_text, counter_text)
    return {
        "scenario": run.scenario, "fields": FIELDS,
        "checkpoints": [cp.__dict__ for cp in run.checkpoints],
        "final_state": run.final_state, "emails": run.emails, "calendar": run.calendar,
        "crm_record": run.crm_record, "classified_reply": run.classified_reply, "booked_slot": run.booked_slot,
        "systems": {"calendar": "demo calendar (no real calendar connected)",
                    "crm": "demo CRM (no real CRM connected)", "email": "demo inbox only"},
        "max_reply_rounds": MAX_REPLY_ROUNDS,
    }
