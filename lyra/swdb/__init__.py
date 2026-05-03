"""Shortwave broadcaster database (EiBi) for the panadapter overlay.

v0.0.9 Step 4 — provides Lyra with a CSV-driven station-name
overlay for shortwave broadcast frequencies.  When an operator
tunes outside the amateur bands of their selected region, the
panadapter renders labels for stations currently on-air per the
loaded EiBi schedule.

Architecture (see ``docs/architecture/v0.0.9_memory_stations_design.md``
section 5):

  - ``eibi_parser`` — parses the semicolon-delimited CSV format
    EiBi publishes at <https://www.eibispace.de/>.
  - ``store``       — sorted-by-frequency in-memory index;
    binary-search lookup by visible panadapter range.
  - ``time_filter`` — "is this entry currently on-air per its
    UTC schedule?" predicate.
  - ``overlay_gate``— "should the overlay render right now?"
    predicate based on master enable + current freq vs
    operator-region band plan.
  - ``downloader``  — background HTTPS GET for the season-named
    CSV file (sked-A26.csv etc.) with progress signalling.

License posture: EiBi's data is free for non-commercial use with
attribution.  Lyra does NOT bundle the file -- the operator
downloads it themselves through the Settings UI on first use.
This keeps Lyra's GPL distribution clean of any third-party
license entanglement.  Same model SDR# / SDRuno follow.
"""
