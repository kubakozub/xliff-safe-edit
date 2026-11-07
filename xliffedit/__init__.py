"""xliff-safe-edit: edit an XLIFF delivery without re-serialising it.

The engine's contract: a delivery file is modified only inside the `<target>`
elements that were actually edited, and every other byte comes back unchanged.
Edit nothing and you get the input bytes back, archive and all.

    from xliffedit import XliffDoc, Package, qa_check
"""

from __future__ import annotations

__version__ = "1.0.0"

from .core import (ALLOWED_STATES, Package, XliffDoc, parse_glossary, plain,
                   qa_check, real_tags, serialize, tag_inventory, tokenize,
                   unescape)

__all__ = ["XliffDoc", "Package", "qa_check", "parse_glossary", "plain",
           "tokenize", "serialize", "tag_inventory", "real_tags", "unescape",
           "ALLOWED_STATES", "__version__"]
