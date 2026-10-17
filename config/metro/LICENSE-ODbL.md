# Licence of the metro ridership aggregates

`ridership.json` and `segments.json` in this folder are an **adapted database** (ODbL 1.0, section 1.0 "Derivative Database")
derived from the hourly Namma Metro ridership dataset **bmrcl-ridership-hourly** by Vonter, which republishes data that
BMRCL supplied under the Right to Information Act (August and September 2025):

- Original dataset: https://github.com/Vonter/bmrcl-ridership-hourly
- Licence: Open Database Licence (ODbL) 1.0, https://opendatacommons.org/licenses/odbl/1-0/ (full text: https://opendatacommons.org/files/2018/02/odbl-10.txt)

What was changed: hourly station boardings/exits and station-pair flows were reduced to medians per station and hour for
four day types (weekday, Saturday, Sunday, holiday); pair flows were routed over a station graph and summed into
per-link hourly loads. Script: `backend/tools_metro_etl.py`. Method and caveats: `data_notes.json`.

## What the ODbL requires of this adapted database

- **Attribution**: keep this notice and show: "Ridership data: BMRCL via RTI, compiled by Vonter/bmrcl-ridership-hourly (ODbL)".
- **Share-alike**: if you publicly use or redistribute this adapted database, or a database derived from it, you must offer it
  under the ODbL (or a compatible licence) and make the adapted database available. Produced Works (such as the planner
  pages, charts and crowd estimates shown to users) may be under other terms but must carry the attribution and a pointer to this database.
- **Keep open**: do not apply technical measures that restrict others from exercising ODbL rights over this database.
- The data is provided as is, without warranty. Crowd values are historical estimates, not live conditions.

This file is a summary, not legal advice; the ODbL 1.0 text governs.
