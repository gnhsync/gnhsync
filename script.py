# -*- coding: utf-8 -*-

import datetime
import zoneinfo

# Interpret exported timestamps with the pinned tzdata package, not OS data.
zoneinfo.reset_tzpath(())

import icalendar
import copy
import dateutil
import unicodedata
import os
import os.path
import urllib.request
import json
import uuid
from pathlib import Path


def occurrence_key(value):
    if isinstance(value, datetime.datetime):
        assert value.tzinfo is not None, "Floating recurrence times are unsupported"
        return value.astimezone(datetime.timezone.utc)
    return value


# Compatibility policy for the observed source schedule and calendar UI.
# Decode exported timestamps with current data, then convert by instant.
CALENDAR_TZFILES = {
    "America/Edmonton": ".recurrence-tzdata/tzdata/zoneinfo/America/Edmonton",
}
# Existing GNH masters are assumed to retain the earlier Edmonton rules.
# New masters can use newer rules, so stop for review before applying this
# policy to them. Keep this baseline fixed; do not replace it with now().
MASTER_CREATED_CUTOFF = datetime.datetime(
    2026, 9, 23, 5, 20, 42, tzinfo=datetime.timezone.utc
)
calendar_timezones = {}
for name, filename in CALENDAR_TZFILES.items():
    with (Path(__file__).parent / filename).open("rb") as timezone_file:
        calendar_timezones[name] = zoneinfo.ZoneInfo.from_file(timezone_file, key=name)

docs = json.loads(os.environ["DOCS_JSON"])

now_utc = datetime.datetime.now(datetime.timezone.utc)
window_start = now_utc - datetime.timedelta(days=365)
window_end = now_utc + datetime.timedelta(days=365)

OUTPUT_DIR = os.path.join("public", os.environ["PUBLISH_SLUG"])
ARTIFACTS_DIR = "artifacts"
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(ARTIFACTS_DIR, exist_ok=True)

with open(os.path.join(ARTIFACTS_DIR, "env.json"), "w") as f:
    json.dump(dict(os.environ), f, indent=4)

master_path = os.path.join(ARTIFACTS_DIR, "master.ics")
urllib.request.urlretrieve(os.environ["MASTER_URL"], master_path)
with open(master_path, "rb") as f: cal = icalendar.Calendar.from_ical(f.read())

recurrence_info = {}

timezones = [copy.deepcopy(vtz) for vtz in cal.walk("VTIMEZONE")]
xwr = cal["X-WR-TIMEZONE"]

for comp in cal.walk("VEVENT"):
    for k in ("RDATE", "DURATION", "EXRULE"): assert k not in comp
    assert comp["STATUS"] == "CONFIRMED"
    uid = comp.decoded("UID")
    dtstart = comp.decoded('DTSTART')

    if uid not in recurrence_info: recurrence_info[uid] = [None, {}]

    if "RECURRENCE-ID" in comp:
        recurid = occurrence_key(comp.decoded("RECURRENCE-ID"))
        d = recurrence_info[uid][1]
        assert recurid not in d
        d[recurid] = comp
    else:
        created = comp.decoded("CREATED")
        if created > MASTER_CREATED_CUTOFF:
            raise ValueError(
                f"Master {uid.decode()} created at {created.isoformat()} is newer than "
                f"the verified cutoff {MASTER_CREATED_CUTOFF.isoformat()}; "
                "review its timezone rules before applying the earlier-rule policy."
            )
        assert recurrence_info[uid][0] is None
        recurrence_info[uid][0] = comp

expanded_instances = []

for uid, l in recurrence_info.items():
    master = l[0]
    assert master is not None
    d = l[1]
    dtstart_master = master.decoded('DTSTART')

    if master.rrules:
        if not isinstance(dtstart_master, datetime.datetime):
            print(master)
            continue
        assert len(master.rrules) == 1
        rrule_prop = master.rrules[0]
        rule_timezone = calendar_timezones.get(
            str(master["DTSTART"].params.get("TZID")), dtstart_master.tzinfo
        )
        rule_start = dtstart_master.astimezone(rule_timezone)
        rule = dateutil.rrule.rrulestr(
            rrule_prop.to_ical().decode(), dtstart=rule_start,
        )
        first = next(iter(rule), None)
        assert first is None or first == rule_start, f"DTSTART does not match RRULE for {uid.decode()}"
        rs = dateutil.rrule.rruleset()
        rs.rrule(rule)
        for exdate in master.exdates: rs.exdate(exdate)
        occs = rs.between(rule_start, window_end, inc=True)
    else: occs = [dtstart_master]

    for occ_local in occs:
        if isinstance(occ_local, datetime.datetime):
            occ_local = occ_local.astimezone(dtstart_master.tzinfo)
        ovr = d.pop(occurrence_key(occ_local), None)
        if ovr is None:
            occ_ev = copy.deepcopy(master)
            occ_ev.DTSTART = occ_local
            end = master.decoded('DTEND')
            duration = occurrence_key(end) - occurrence_key(dtstart_master)
            new_end = occurrence_key(occ_local) + duration
            occ_ev.DTEND = (new_end.astimezone(end.tzinfo)
                            if isinstance(end, datetime.datetime) else new_end)
        else: occ_ev = copy.deepcopy(ovr)

        for k in ("RRULE", "RECURRENCE-ID", "EXDATE"):
            if k in occ_ev: del occ_ev[k]

        occ_ev["UID"] = f"{uid.decode()}-{uuid.uuid4().hex}"
        expanded_instances.append(occ_ev)

    assert not d, f"Unmatched recurrence IDs for {uid.decode()}: {list(d)}"

doc_cals = {d: icalendar.Calendar() for d in docs}
for d, c in doc_cals.items():
    c.add("prodid", "-//shifts//")
    c.add("version", "2.0")
    c.add("x-wr-timezone", xwr)
    c.add("x-wr-calname", "GNH " + d)
    for vtz in timezones: c.add_component(vtz)

for ev in expanded_instances:
    dt = ev.decoded("DTSTART")
    if isinstance(dt, datetime.datetime):
        dt = dt.astimezone(calendar_timezones.get(str(xwr), dt.tzinfo))
    title = ev.get("SUMMARY", "")
    title_norm = ''.join([
        c for c in unicodedata.normalize('NFKD', title.lower())
        if not unicodedata.combining(c)
    ])

    nev = copy.deepcopy(ev)

    # Serialize affected timed events as UTC; the abbreviated Google
    # VTIMEZONE does not describe historical winter offsets. Keep local
    # `dt` above for the existing doctor title/date conversion below.
    if str(ev["DTSTART"].params.get("TZID")) in calendar_timezones:
        nev.DTSTART = occurrence_key(dt)
        nev.DTEND = occurrence_key(ev.decoded("DTEND"))

    for d in docs:
        if title_norm.startswith(d):
            if isinstance(dt, datetime.datetime):
                tstr = dt.strftime("%-I:%M %p")
                nev["SUMMARY"] = f"{tstr} GNH {title}"

                day = dt.date()
                nev.DTSTART = day
                nev.DTEND = day + datetime.timedelta(days=1)
            doc_cals[d].add_component(nev)
            break
    else:
        _dt = dt.date() if isinstance(dt, datetime.datetime) else dt
        if _dt > window_start.date():
            print(dt, title)
            for d in docs: doc_cals[d].add_component(nev)

for doc, c in doc_cals.items():
    with open(os.path.join(OUTPUT_DIR, f"{doc}.ics"), "wb") as f:
        f.write(c.to_ical())
