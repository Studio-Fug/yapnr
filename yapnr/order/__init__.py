"""Staged ordering: ``yapnr order stage|vendors`` (docs/design/fab-and-ordering.md §8, O1).

Staging only (docs/decisions.md): yapnr prepares the files and takes the human to the vendor's
upload, quote or cart page. It never pays, never confirms or submits an order, never stores
payment data, never logs in, never creates an account and never accepts terms for anyone.

O1 opens no network connection: opening a page hands a URL to the human's browser, and yapnr does
not fetch it. The one exception is ``--verify-url`` (``net.verify_url``), a read-only GET of the
bundle's own published zip. The opt-in upload (O2) and vendor APIs (O3) are not built; they wait
for the owner's decisions D1 and D3.
"""
