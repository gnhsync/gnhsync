# -*- coding: utf-8 -*-

import datetime
import icalendar
import copy
import dateutil
import unicodedata
import os
import os.path
import urllib.request
import json
import uuid

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
        recurid = comp.decoded("RECURRENCE-ID")
        d = recurrence_info[uid][1]
        assert recurid not in d
        d[recurid] = comp
    else:
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

        rs = dateutil.rrule.rruleset()
        rs.rrule(
            dateutil.rrule.rrulestr(
                rrule_prop.to_ical().decode(),
                dtstart=dtstart_master,
            ))
        for exdate in master.exdates: rs.exdate(exdate)
        occs = rs.between(dtstart_master, window_end, inc=True)
    else: occs = [dtstart_master]

    for occ_local in occs:
        ovr = d.pop(occ_local, None)
        if ovr is None:
            occ_ev = copy.deepcopy(master)
            occ_ev.DTSTART = occ_local
            occ_ev.DTEND = occ_local + (
                master.decoded('DTEND') - dtstart_master
            )
        else: occ_ev = copy.deepcopy(ovr)

        for k in ("RRULE", "RECURRENCE-ID", "EXDATE"):
            if k in occ_ev: del occ_ev[k]

        occ_ev["UID"] = f"{uid.decode()}-{uuid.uuid4().hex}"
        expanded_instances.append(occ_ev)

    assert not d

doc_cals = {d: icalendar.Calendar() for d in docs}
for c in doc_cals.values():
    c.add("prodid", "-//shifts//")
    c.add("version", "2.0")
    c.add("x-wr-timezone", xwr)
    for vtz in timezones: c.add_component(vtz)

for ev in expanded_instances:
    dt = ev.decoded("DTSTART")
    title = ev.get("SUMMARY", "")
    title_norm = ''.join([
        c for c in unicodedata.normalize('NFKD', title.lower())
        if not unicodedata.combining(c)
    ])

    nev = copy.deepcopy(ev)

    for d in docs:
        if title_norm.startswith(d):
            if isinstance(dt, datetime.datetime):
                tstr = dt.strftime("%-I:%M %p")
                nev["SUMMARY"] = f"{tstr} GNH ED - {title}"

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
