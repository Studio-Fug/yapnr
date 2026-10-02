"""Staged ordering (docs/design/fab-and-ordering.md §8).

Staging only (docs/decisions.md): yapnr prepares the files and takes the human to the vendor's
upload, quote or cart page. It never pays, never confirms or submits an order, never stores
payment data, never logs in, never creates an account and never accepts terms for anyone.

``card`` is the order card every fab bundle carries, and ``vendors`` the vendors' staging pages
and steps (from ``yapnr/fab/data/vendors``). Neither opens a network connection.
"""
