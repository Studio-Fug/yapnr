"""Sphinx configuration for the yapnr documentation.

This file is copied into a staging tree by docs/build_docs.py (the
``//docs:build`` target); it is not run in place. The staging tree mirrors the
repository layout, so ``docs/index.md`` is the root document and links between
Markdown files resolve as they do on GitHub.
"""

from __future__ import annotations

project = "yapnr"
author = "Kevin Balke"
copyright = "2026 Kevin Balke. Licensed under AGPL-3.0-or-later"

# -- General ------------------------------------------------------------------

extensions = [
    "myst_parser",  # Markdown (MyST) sources
    "sphinx_copybutton",  # copy buttons on code blocks
    "sphinx_design",  # cards and grids
    "sphinxcontrib.mermaid",  # diagrams
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "html_image",
    "strikethrough",  # ~~done~~ items in status lists (HTML builders only)
    "substitution",
    "tasklist",
]
myst_heading_anchors = 4
# Plain ```mermaid fences render on GitHub too; treat them as the directive.
myst_fence_as_directive = ["mermaid"]

suppress_warnings = [
    "myst.header",
    # MyST warns on every ~~strikethrough~~, even for HTML, the only builder used here.
    "myst.strikethrough",
    "myst.xref_missing",
    "toc.not_readable",
    "misc.highlighting_failure",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
root_doc = "docs/index"

exclude_patterns = [
    "_build",
    "_extra",
    "Thumbs.db",
    ".DS_Store",
    "**/__pycache__",
]

# -- HTML output --------------------------------------------------------------

html_title = "yapnr documentation"
html_theme = "furo"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_favicon = "_static/yapnr-favicon.svg"
# branding/ is copied into the output root so the raw-HTML images in README.md
# (branding/yapnr-logo-256.png) resolve.
html_extra_path = ["_extra"]

# Palette (branding/README.md): forest green #024823, antique gold #ba9015,
# light sage #ddeee0 (replaces the green on dark backgrounds).
html_theme_options = {
    "light_logo": "yapnr-logo-256.png",
    "dark_logo": "yapnr-logo-light.png",
    "sidebar_hide_name": True,
    "navigation_with_keys": True,
    "source_repository": "https://github.com/Studio-Fug/yapnr/",
    "source_branch": "main",
    "source_directory": "",
    "light_css_variables": {
        "color-brand-primary": "#024823",
        "color-brand-content": "#024823",
        "color-brand-visited": "#024823",
        "color-yapnr-accent": "#ba9015",
    },
    "dark_css_variables": {
        "color-brand-primary": "#ddeee0",
        "color-brand-content": "#ddeee0",
        "color-brand-visited": "#ba9015",
        "color-yapnr-accent": "#ba9015",
    },
}

# Pin the mermaid runtime (sphinxcontrib-mermaid loads it from a CDN), as in
# Splanc, so diagrams render the same way everywhere.
mermaid_version = "11.4.1"
# sphinxcontrib-mermaid gives every diagram a fixed 500px-tall box, which
# shrinks wide diagrams to unreadable text and pads them with blank space.
# Size diagrams by their aspect ratio instead; custom.css caps the height.
mermaid_height = "auto"

# Copy button: strip shell and REPL prompts.
copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True
