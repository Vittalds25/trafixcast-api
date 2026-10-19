# Licence of the bus data

`bus.json.gz` in this folder is an **adapted database** (ODbL 1.0, section 1.0 "Derivative Database") derived from the community
BMTC GTFS feed **bmtc-gtfs** by Vonter, which was collected from the Namma BMTC app (BMTC, Bengaluru Metropolitan Transport Corporation):

- Original dataset: https://github.com/Vonter/bmtc-gtfs
- Licence: Open Database Licence (ODbL) 1.0, https://opendatacommons.org/licenses/odbl/1-0/ (full text: https://opendatacommons.org/files/2018/02/odbl-10.txt)
- The dataset's author notes that some of its contents remain under BMTC's own copyright. No separate BMTC licence has been given.

What was changed: only the 2 most frequent stop sequences of each route and direction were kept; times along each sequence and the list of
scheduled departures from its first stop were reduced to whole minutes; stop-pair fares (GTFS fare rules) were kept as they are. Bus
services were classed as ordinary, Vajra (AC) or Vayu Vajra (AC, airport) from the route number and from whether the feed gives the
route fares of its own. Script: `backend/tools_bus_build.py`.

This derived database is made available under the same licence (ODbL 1.0). Attribution: "Bus data: BMTC, via Vonter/bmtc-gtfs (ODbL 1.0)".
Times are scheduled times, not live. Fares are the ones in the BMTC app data and can differ from the price at the counter.
