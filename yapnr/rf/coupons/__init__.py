"""Fab-model test coupons: generation, expected S-parameters and parameter extraction.

Design: docs/design/rf-fab-coupons.md (issue #32); user guide: docs/rf-fab-coupons.md.

- `stackups`: the layered JLCPCB stackups, the fit parameters and their priors;
- `xsec`: the 2D quasi-static cross-section solver (scikit-fem + gmsh, optional: only the table
  tool and the slow tests need it);
- `families`: line families (cross-sections) and their shipped surrogate tables;
- `models`: Djordjevic-Sarkar dielectrics, roughness, RLGC lines, network algebra, structures;
- `catalog`: the sticks of each board; `layout`: the KiCad board writer; `fab`: DRC and gerbers;
- `touchstone`, `session`: files and measurement sessions;
- `calibrate`: multiline TRL and the TDR impedance; `fit`: the joint fit; `export`: results;
- `synthetic`: synthetic sessions for the recovery test; `expected`: predicted Touchstone files.

The package needs numpy only. scikit-rf is optional (a cross-check of the calibration).
"""

SCHEMA_FIT = "yapnr-stackup-fit/1"
SCHEMA_SESSION = "yapnr-coupon-session/1"
SCHEMA_TABLES = "yapnr-coupon-tables/1"
SCHEMA_CATALOG = "yapnr-coupon-catalog/1"
