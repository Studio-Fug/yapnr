"""`python -m yapnr.rf.coupons`: the command line (yapnr.rf.coupons.cli)."""

import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
    from yapnr.rf.coupons.cli import main

    sys.exit(main())
