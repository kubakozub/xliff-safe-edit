"""XLIFF test fixtures, built in code rather than committed as files.

Two deliberate choices:

The vendor namespace and the odd attribute shapes are kept, because real
deliveries carry them and they are what the scanner has to survive. A '>' inside
an attribute value is in here on purpose: that is where a regex based parser
fails.

Target text is Polish. This was built for an English to Polish workflow, and a
test corpus of ASCII would not exercise the thing most likely to break, which is
non ASCII round-tripping through entities and encodings. Polish in these strings
is test DATA, not prose.
"""

from __future__ import annotations

import io
import zipfile

HEAD = ('<?xml version="1.0" encoding="utf-8"?>\n'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" '
        'xmlns:tms="http://example.invalid/tms">\n'
        '<file original="x.docx" source-language="en-US" target-language="pl-PL" '
        'datatype="x">\n<body>\n')
FOOT = '</body>\n</file>\n</xliff>\n'

#: A paired formatting tag, in the shape a real tool emits: the markup is
#: escaped inside the tag, which is exactly what must not be escaped twice.
CF_S = '<bpt id="1">&lt;cf italic="on" size="14"&gt;</bpt>'
CF_E = '<ept id="1">&lt;/cf&gt;</ept>'

SKELETON = b"SKELETON-BYTES\x00\x01\x02"


def unit(uid, src, tgt=None, translate="yes", attrs="", alt=None, raw_target=None):
    """One <trans-unit>, with the whitespace and attribute order of a real file."""
    text = ('\t\t\t<trans-unit id="%s"%s translate="%s">\n'
            '\t\t\t\t<source>%s</source>\n' % (uid, attrs, translate, src))
    if raw_target is not None:
        text += raw_target
    elif tgt is not None:
        text += ('\t\t\t<target state="translated" state-qualifier="leveraged-mt" '
                 'tms:origin="mt">%s</target>' % tgt)
    if alt is not None:
        text += '<alt-trans><target>%s</target></alt-trans>' % alt
    text += '</trans-unit>\n'
    return text


def sample_xlf():
    """Eleven segments, each one a case that broke something once."""
    body = "".join([
        unit("1", "Hello world.", "Witaj swiecie."),
        unit("2", " \t", translate="no"),
        unit("3", CF_S + "Winter Exhibition" + CF_E + " tour",
             CF_S + "Wystawa zimowa" + CF_E + " trasa"),
        unit("4", "Section 12(b) &amp; more", "§ 12(b) &amp; wiecej"),
        unit("5", "Nothing here.", ""),
        unit("6", "No target at all."),
        unit("7", "Non&#160;breaking", "Twarda&#160;spacja"),
        unit("8", "Alt trans case", "Nowe tlumaczenie", alt="Stare tlumaczenie"),
        unit("9", "Attr with &gt; inside", "Atrybut z &gt; w srodku",
             attrs=' tms:repetition="a&gt;b:5:5" resname="R>1"'),
        unit("10", "Repeat me", "Powtorz mnie"),
        unit("11", "Repeat me", "Powtorz inaczej"),
    ])
    return (HEAD + body + FOOT).encode("utf-8")


def sample_xlz(xlf=None, skel=SKELETON):
    """A .xlz: a zip holding content.xlf and a skeleton that must never change."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("content.xlf", (2026, 9, 19, 0, 0, 0)),
                         xlf or sample_xlf())
        archive.writestr(zipfile.ZipInfo("skeleton.skl", (2026, 9, 19, 0, 0, 0)), skel)
    return buf.getvalue()


def many_xlf(n=200):
    body = "".join(unit(str(i), "Source line %d." % i, "Zrodlo %d." % i)
                   for i in range(1, n + 1))
    return (HEAD + body + FOOT).encode("utf-8")


def one(src, tgt, translate="yes"):
    """A single segment document, for the quality rules."""
    return (HEAD + unit("1", src, tgt, translate=translate) + FOOT).encode("utf-8")
