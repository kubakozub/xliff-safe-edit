#!/usr/bin/env python3
"""Show the claim rather than assert it.

    python3 examples/roundtrip_demo.py

Builds a small .xlz, edits exactly one segment, writes it back, and reports how
many bytes and how many lines changed.
"""

import difflib
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tests"))

from fixtures import sample_xlz  # noqa: E402
from xliffedit import Package  # noqa: E402


def content(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.read("content.xlf").decode("utf-8")


before = sample_xlz()

untouched = Package(before, "delivery.xlz")
print("open and save with no edit: %s"
      % ("identical bytes" if untouched.to_bytes() == before else "DIFFERENT"))

package = Package(before, "delivery.xlz")
package.doc.set_target_tokens("0", [{"kind": "text", "text": "Witaj, swiecie!"}])
after = package.to_bytes()

print("archive  %d bytes -> %d bytes" % (len(before), len(after)))
old_lines = content(before).splitlines(keepends=True)
new_lines = content(after).splitlines(keepends=True)
changed = [line for line in difflib.unified_diff(old_lines, new_lines, n=0)
           if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
print("content.xlf lines changed by that one edit: %d of %d"
      % (len(changed) // 2, len(old_lines)))
for line in changed:
    print("   %s" % line.rstrip()[:96])

with zipfile.ZipFile(io.BytesIO(before)) as a, zipfile.ZipFile(io.BytesIO(after)) as b:
    print("skeleton.skl unchanged: %s" % (a.read("skeleton.skl") == b.read("skeleton.skl")))
    print("entry order preserved:  %s" % (a.namelist() == b.namelist()))
