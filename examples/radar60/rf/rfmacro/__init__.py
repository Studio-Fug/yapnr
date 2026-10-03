"""radar60 Board A conventional RF macro: closed-form dimensions, geometry and KiCad output.

The macro is generated in U1's ball frame (mm, +y north, U1 rotated so its RX edge faces north)
from the IWR6843 ball map (TI SWRS219F, Table 6-1) and the stackup of the board plan; no TI design
file is read or copied. `python -m rfmacro --help` (from `examples/radar60/rf`) lists the commands.
"""

SCHEMA = "radar60-rfmacro/1"
