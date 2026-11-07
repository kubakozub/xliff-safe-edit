"""The XLIFF engine: parse, edit, and write back byte for byte.

The governing rule: a delivery file is modified ONLY inside the `<target>`
elements the user actually edited. Every untouched byte stays where it was,
including entity style, whitespace and attribute order. When nothing was
changed, the input bytes are returned literally, archive and all for a .xlz.

That is not tidiness. A translation delivery goes back into a tool chain that
diffs it, and a re-serialising editor turns one edited segment into a file where
every line looks changed, which is how a reviewer stops reviewing.

Addressing: a segment is identified by its 'key', its ordinal position in the
file, and never by its id attribute. XLIFF permits the same id in different
`<file>` sections, and addressing by id put an edit into a different segment.
Tags are addressed by 'tid', their ordinal position in the segment, for the same
reason: `<mrk>` and `<g>` are not required to carry an id.
"""

import io
import re
import unicodedata
import zipfile

TAG_ATOMIC = {"bpt", "ept", "ph", "it", "x", "bx", "ex"}
TAG_WRAPPER = {"g", "mrk", "sub"}
# kept literally, and not counted as formatting tags
TAG_META = {"#comment", "#cdata", "#pi"}

ALLOWED_STATES = {"new", "needs-translation", "needs-l10n", "needs-adaptation",
                  "needs-review-translation", "needs-review-l10n",
                  "needs-review-adaptation", "translated", "signed-off", "final"}

MAX_UNCOMPRESSED = 512 * 1024 * 1024


# --------------------------------------------------------------------------
# the XML scanner
# --------------------------------------------------------------------------

def _scan_tag(s, i):
    """Read the tag starting at s[i] == '<'. Returns (end, name, self_closing, kind).

    Quoted attribute values are respected, so a '>' inside a value does not end
    the tag. That is precisely where a naive regex gives up, and real delivery
    files contain such values.
    """
    if s[i] != "<":
        return i + 1, None, True, "text"
    if s.startswith("<!--", i):
        end = s.find("-->", i)
        return (len(s) if end < 0 else end + 3), "#comment", True, "comment"
    if s.startswith("<![CDATA[", i):
        end = s.find("]]>", i)
        return (len(s) if end < 0 else end + 3), "#cdata", True, "cdata"
    if s.startswith("<?", i):
        end = s.find("?>", i)
        return (len(s) if end < 0 else end + 2), "#pi", True, "pi"
    if s.startswith("<!", i):
        end = s.find(">", i)
        return (len(s) if end < 0 else end + 1), "#decl", True, "decl"

    closing = s.startswith("</", i)
    j = i + (2 if closing else 1)
    m = re.compile(r"[A-Za-z_][\w.\-:]*").match(s, j)
    if not m:
        return i + 1, None, True, "text"
    name = m.group(0)
    j = m.end()
    quote = None
    while j < len(s):
        c = s[j]
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == ">":
            self_closing = s[j - 1] == "/"
            return j + 1, name, (self_closing or closing), ("close" if closing else "open")
        j += 1
    return len(s), name, True, "open"


def comment_spans(s):
    """Comment and CDATA spans, so a commented out <trans-unit> is not a segment."""
    spans = []
    i = 0
    while True:
        a = s.find("<!--", i)
        b = s.find("<![CDATA[", i)
        if a < 0 and b < 0:
            break
        if a < 0 or (0 <= b < a):
            end = s.find("]]>", b)
            end = len(s) if end < 0 else end + 3
            spans.append((b, end))
        else:
            end = s.find("-->", a)
            end = len(s) if end < 0 else end + 3
            spans.append((a, end))
        i = end
    return spans


def _in_spans(pos, spans):
    for a, b in spans:
        if a <= pos < b:
            return True
        if pos < a:
            return False
    return False


def _find_element(s, name, start=0, limit=None):
    """The first <name ...> ... </name> element inside s[start:limit].

    Returns (start, end, inner_start, inner_end, raw_attributes) or None.
    Handles the self closing form, <target/>.
    """
    limit = len(s) if limit is None else limit
    i = start
    while i < limit:
        i = s.find("<", i)
        if i < 0 or i >= limit:
            return None
        end, tname, selfclose, kind = _scan_tag(s, i)
        if kind == "open" and tname == name:
            open_end = end
            attrs = s[i:end]
            if selfclose:
                return i, end, end, end, attrs
            depth = 1
            j = end
            while j < limit:
                j = s.find("<", j)
                if j < 0 or j >= limit:
                    return None
                e2, n2, sc2, k2 = _scan_tag(s, j)
                if n2 == name:
                    if k2 == "open" and not sc2:
                        depth += 1
                    elif k2 == "close":
                        depth -= 1
                        if depth == 0:
                            return i, e2, open_end, j, attrs
                j = e2
            return None
        i = end
    return None


def _attr(raw, key):
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(key), raw)
    if m:
        return m.group(1)
    m = re.search(r"\b%s\s*=\s*'([^']*)'" % re.escape(key), raw)
    return m.group(1) if m else None


def _set_attr(open_tag, key, value):
    """Replace or add an attribute on an opening tag. Copes with <target/>."""
    tag = open_tag
    selfclose = tag.endswith("/>")
    body = tag[:-2] if selfclose else tag[:-1]
    pat_d = re.compile(r'(\b%s\s*=\s*)"[^"]*"' % re.escape(key))
    pat_s = re.compile(r"(\b%s\s*=\s*)'[^']*'" % re.escape(key))
    if pat_d.search(body):
        body = pat_d.sub(lambda m: m.group(1) + '"%s"' % value, body, count=1)
    elif pat_s.search(body):
        body = pat_s.sub(lambda m: m.group(1) + "'%s'" % value, body, count=1)
    else:
        body = body.rstrip() + ' %s="%s"' % (key, value)
    return body + ">"


# --------------------------------------------------------------------------
# entities
# --------------------------------------------------------------------------

NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}
ENT_RE = re.compile(r"&(#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")


def _ent(m):
    body = m.group(1)
    try:
        if body.startswith("#x") or body.startswith("#X"):
            return chr(int(body[2:], 16))
        if body.startswith("#"):
            return chr(int(body[1:]))
    except (ValueError, OverflowError):
        return m.group(0)
    return NAMED.get(body, m.group(0))


def unescape(t):
    return ENT_RE.sub(_ent, t)


def escape(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# tokenising the contents of <source> and <target>
# --------------------------------------------------------------------------

def tokenize(inner):
    """The inside of a source or target element, as a list of tokens.

    text:  {kind: 'text', raw, text}
    tag:   {kind: 'tag', raw, name, id, role, tid}
    A comment or CDATA arrives as a 'tag' token with the 'meta' role: kept
    literally, and never shown to the translator as editable text.
    """
    toks = []
    i = 0
    n = len(inner)
    buf_start = 0

    def flush(upto):
        nonlocal buf_start
        if upto > buf_start:
            raw = inner[buf_start:upto]
            toks.append({"kind": "text", "raw": raw, "text": unescape(raw)})
        buf_start = upto

    while i < n:
        c = inner.find("<", i)
        if c < 0:
            break
        end, name, selfclose, kind = _scan_tag(inner, c)
        if kind == "text":
            i = end
            continue
        flush(c)
        if kind in ("comment", "cdata", "pi", "decl"):
            toks.append({"kind": "tag", "raw": inner[c:end], "name": name or "#decl",
                         "id": "", "role": "meta"})
        elif kind == "open" and name in TAG_ATOMIC and not selfclose:
            el = _find_element(inner, name, c)
            if el:
                raw = inner[el[0]:el[1]]
                toks.append({"kind": "tag", "raw": raw, "name": name,
                             "id": _attr(raw, "id") or "", "role": "atomic"})
                i = buf_start = el[1]
                continue
            toks.append({"kind": "tag", "raw": inner[c:end], "name": name,
                         "id": _attr(inner[c:end], "id") or "", "role": "atomic"})
        elif kind == "open" and selfclose:
            toks.append({"kind": "tag", "raw": inner[c:end], "name": name,
                         "id": _attr(inner[c:end], "id") or "", "role": "atomic"})
        elif kind == "open" and name in TAG_WRAPPER:
            toks.append({"kind": "tag", "raw": inner[c:end], "name": name,
                         "id": _attr(inner[c:end], "id") or "", "role": "open"})
        elif kind == "open":
            el = _find_element(inner, name, c)
            if el:
                raw = inner[el[0]:el[1]]
                toks.append({"kind": "tag", "raw": raw, "name": name,
                             "id": _attr(raw, "id") or "", "role": "atomic"})
                i = buf_start = el[1]
                continue
            toks.append({"kind": "tag", "raw": inner[c:end], "name": name,
                         "id": _attr(inner[c:end], "id") or "", "role": "open"})
        else:
            toks.append({"kind": "tag", "raw": inner[c:end], "name": name or "",
                         "id": "", "role": "close"})
        i = buf_start = end
    flush(n)
    _number_tags(toks)
    return toks


def _number_tags(toks):
    k = 0
    for t in toks:
        if t["kind"] == "tag":
            t["tid"] = k
            k += 1


def serialize(toks):
    """Assemble tokens back into XML. An untouched token returns its own 'raw'."""
    out = []
    for t in toks:
        if t["kind"] == "tag":
            out.append(t["raw"])
        elif "raw" in t and t.get("text") == unescape(t["raw"]):
            out.append(t["raw"])       # byte for byte, entity style included
        else:
            out.append(escape(t["text"]))
    return "".join(out)


def plain(toks):
    return "".join(t["text"] for t in toks if t["kind"] == "text")


def tag_inventory(toks):
    """Formatting tags only, excluding comments and CDATA."""
    return [(t["name"], t["id"], t["role"]) for t in toks
            if t["kind"] == "tag" and t["role"] != "meta"]


def real_tags(toks):
    return [t for t in toks if t["kind"] == "tag" and t["role"] != "meta"]


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------

class XliffDoc:
    BOMS = (
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xff\xfe", "utf-16-le"),
    )

    #: Declared on the class so they exist even for a file with no <file> element.
    source_language = ""
    target_language = ""

    def __init__(self, raw_bytes, name="content.xlf"):
        self.name = name
        self.raw_bytes = raw_bytes
        self.bom, self.encoding = self._detect_encoding(raw_bytes)
        self.text = raw_bytes[len(self.bom):].decode(self.encoding)
        self.units = []
        self._by_key = {}
        self._parse()

    @classmethod
    def _detect_encoding(cls, b):
        for bom, enc in cls.BOMS:
            if b.startswith(bom):
                return bom, enc
        if b[:4] == b"<\x00?\x00":
            return b"", "utf-16-le"
        if b[:4] == b"\x00<\x00?":
            return b"", "utf-16-be"
        head = b[:400].decode("latin-1", "replace")
        m = re.search(r'encoding\s*=\s*["\']([\w\-]+)["\']', head)
        if m:
            try:
                "x".encode(m.group(1))
                return b"", m.group(1)
            except LookupError:
                pass
        return b"", "utf-8"

    def _parse(self):
        s = self.text
        skip = comment_spans(s)

        # The <file> element declares the languages. Worth having because the
        # editor sets the HTML lang attribute from it, which is what gives a
        # translator a spell checker in the language they are writing. It used to
        # be hardcoded to one language.
        found = _find_element(s, "file")
        if found:
            raw = found[4]
            self.source_language = _attr(raw, "source-language") or ""
            self.target_language = _attr(raw, "target-language") or ""

        i = 0
        ordinal = 0
        while True:
            j = s.find("<trans-unit", i)
            if j < 0:
                break
            if _in_spans(j, skip):          # a commented out segment is not a segment
                i = j + 11
                continue
            end, name, selfclose, kind = _scan_tag(s, j)
            if name != "trans-unit" or kind != "open":
                i = j + 1
                continue
            el = _find_element(s, "trans-unit", j)
            if not el:
                i = end
                continue
            u_start, u_end, in_start, in_end, u_attrs = el
            inner = s[in_start:in_end]

            alt = _find_element(inner, "alt-trans")
            alt_pos = alt[0] if alt else len(inner)

            src = _find_element(inner, "source")
            seg = _find_element(inner, "seg-source")
            tgt = _find_element(inner, "target", 0, alt_pos)

            # a new <target> goes after the last source or seg-source
            after = max([e[1] for e in (src, seg) if e] or [0])

            unit = {
                "key": str(ordinal),
                "id": _attr(u_attrs, "id") or "",
                "resname": _attr(u_attrs, "resname") or "",
                "translate": (_attr(u_attrs, "translate") or "yes").lower(),
                "src_tokens": tokenize(inner[src[2]:src[3]]) if src else [],
                "has_target": bool(tgt),
                "tgt_attrs": tgt[4] if tgt else "",
                "tgt_state": _attr(tgt[4], "state") if tgt else None,
                "tgt_qualifier": _attr(tgt[4], "state-qualifier") if tgt else None,
                "tgt_abs": (in_start + tgt[0], in_start + tgt[1]) if tgt else None,
                "insert_at": in_start + after,
                "tgt_tokens": tokenize(inner[tgt[2]:tgt[3]]) if tgt else [],
                "orig_tgt_inner": inner[tgt[2]:tgt[3]] if tgt else "",
                "orig_state": _attr(tgt[4], "state") if tgt else None,
                "dirty": False,
            }
            # Tag identity is assigned ONCE, at parse time. 'tid' is renumbered on
            # every edit; 'sref' is not, and only 'sref' is written back.
            for t in unit["src_tokens"]:
                if t["kind"] == "tag":
                    t["sref"] = "s%d" % t["tid"]
            for t in unit["tgt_tokens"]:
                if t["kind"] == "tag":
                    t["sref"] = t["tid"]
            self.units.append(unit)
            self._by_key[unit["key"]] = unit
            ordinal += 1
            i = u_end

        if not self.units:
            self._explain_why_nothing_parsed()

    def _explain_why_nothing_parsed(self):
        """Fail loudly instead of presenting an empty, editable document.

        XLIFF 2.0 restructured the format: `<trans-unit>` became `<unit>` with a
        `<segment>` inside it. A 2.0 file is perfectly valid XML and parses here
        to zero segments, so without this the editor opened it and showed nothing,
        which reads as a corrupt delivery rather than an unsupported version.
        """
        head = self.text[:4000]
        if "urn:oasis:names:tc:xliff:document:2." in head or re.search(
                r"<xliff\b[^>]*\bversion\s*=\s*[\"']2\.", head):
            raise ValueError(
                "this is XLIFF 2.x, which this tool does not support. It handles "
                "XLIFF 1.2, where a segment is <trans-unit>. In 2.x a segment is "
                "<unit><segment>, and editing it safely needs different code, not "
                "a looser parser.")
        if "<xliff" not in head:
            raise ValueError("no <xliff> element: this does not look like an "
                             "XLIFF file at all")
        raise ValueError("the file declares XLIFF but holds no <trans-unit>, so "
                         "there is nothing to edit")

    # ---------------- access ----------------

    def unit(self, key):
        u = self._by_key.get(str(key))
        if u is None:
            raise KeyError("no segment %s" % key)
        return u

    def unit_by_id(self, uid):
        """For compatibility and tests only. With duplicate ids it returns the first."""
        for u in self.units:
            if u["id"] == uid:
                return u
        return None

    @property
    def dirty(self):
        return any(u["dirty"] for u in self.units)

    # ---------------- editing ----------------

    def _mark(self, u):
        u["dirty"] = (serialize(u["tgt_tokens"]) != u["orig_tgt_inner"]
                      or u["tgt_state"] != u["orig_state"])

    def set_target_tokens(self, key, tokens):
        u = self.unit(key)
        _number_tags(tokens)
        u["tgt_tokens"] = tokens
        self._mark(u)
        return u

    def set_target_text(self, key, new_text):
        """Replace the text while keeping the tags. Tag positions are given as
        placeholders {1} to {N}, in the order the tags occur in the segment."""
        u = self.unit(key)
        tags = real_tags(u["tgt_tokens"])
        if not tags:
            return self.set_target_tokens(key, [{"kind": "text", "text": new_text}])
        found = [int(m.group(1)) for m in re.finditer(r"\{(\d+)\}", new_text)]
        if not found:
            raise ValueError("the segment has %d tags, so give the text with "
                             "placeholders {1} to {%d}"
                             % (len(tags), len(tags)))
        bad = [n for n in found if n < 1 or n > len(tags)]
        if bad:
            raise ValueError("placeholder(s) out of range: %s (allowed 1 to %d)"
                             % (sorted(set(bad)), len(tags)))
        toks = []
        pos = 0
        for m in re.finditer(r"\{(\d+)\}", new_text):
            if m.start() > pos:
                toks.append({"kind": "text", "text": new_text[pos:m.start()]})
            toks.append(tags[int(m.group(1)) - 1])
            pos = m.end()
        if pos < len(new_text):
            toks.append({"kind": "text", "text": new_text[pos:]})
        return self.set_target_tokens(key, toks)

    def set_state(self, key, state):
        if state not in ALLOWED_STATES:
            raise ValueError("state not allowed: %r" % (state,))
        u = self.unit(key)
        u["tgt_state"] = state
        self._mark(u)
        return u

    # ---------------- writing ----------------

    def _target_xml(self, u):
        open_tag = u["tgt_attrs"] or "<target>"
        if open_tag.endswith("/>"):            # <target/> -> <target>
            open_tag = open_tag[:-2].rstrip() + ">"
        if u["tgt_state"] is not None:
            open_tag = _set_attr(open_tag, "state", u["tgt_state"])
        return open_tag + serialize(u["tgt_tokens"]) + "</target>"

    def to_text(self):
        edits = []
        for u in self.units:
            if not u["dirty"]:
                continue
            if u["has_target"]:
                edits.append((u["tgt_abs"][0], u["tgt_abs"][1], self._target_xml(u)))
            else:
                state = u["tgt_state"] or "translated"
                xml = '<target state="%s">%s</target>' % (state, serialize(u["tgt_tokens"]))
                edits.append((u["insert_at"], u["insert_at"], xml))
        if not edits:
            return self.text
        edits.sort(key=lambda e: e[0])
        out = []
        prev = 0
        for a, b, rep in edits:
            out.append(self.text[prev:a])
            out.append(rep)
            prev = b
        out.append(self.text[prev:])
        return "".join(out)

    def to_bytes(self):
        if not self.dirty:
            return self.raw_bytes            # nothing changed, so return the input
        return self.bom + self.to_text().encode(self.encoding)


# --------------------------------------------------------------------------
# the .xlz package
# --------------------------------------------------------------------------

class Package:
    """A .xlz archive (content.xlf plus skeleton.skl) or a bare .xlf."""

    def __init__(self, data, filename):
        self.filename = filename
        self.raw = data
        self.is_zip = zipfile.is_zipfile(io.BytesIO(data))
        self.entries = []
        self.xlf_index = None
        self.zip_comment = b""
        if self.is_zip:
            total = 0
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                self.zip_comment = z.comment or b""
                for info in z.infolist():
                    total += info.file_size
                    if total > MAX_UNCOMPRESSED:
                        raise ValueError("the archive expands to more than %d MB"
                                         % (MAX_UNCOMPRESSED // (1024 * 1024)))
                    self.entries.append((info, z.read(info.filename)))
            for k, (info, _blob) in enumerate(self.entries):
                if info.filename.lower().endswith((".xlf", ".xliff")):
                    self.xlf_index = k
                    break
            if self.xlf_index is None:
                raise ValueError("the archive holds no .xlf file")
            info, blob = self.entries[self.xlf_index]
            self.doc = XliffDoc(blob, info.filename)
        else:
            self.doc = XliffDoc(data, filename)

    def to_bytes(self):
        if not self.doc.dirty:
            return self.raw                  # open and save with no edit returns the input
        if not self.is_zip:
            return self.doc.to_bytes()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.comment = self.zip_comment
            for k, (info, blob) in enumerate(self.entries):
                payload = self.doc.to_bytes() if k == self.xlf_index else blob
                ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                ni.compress_type = info.compress_type
                ni.external_attr = info.external_attr
                ni.internal_attr = info.internal_attr
                ni.create_system = info.create_system
                ni.comment = info.comment
                ni.extra = info.extra
                z.writestr(ni, payload)
        return buf.getvalue()


# --------------------------------------------------------------------------
# quality checks
# --------------------------------------------------------------------------

NUM_RE = re.compile(r"\d[\d  .,]*\d|\d")
WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _norm_number(raw):
    """'1 500,00' -> '1500.00', '1,500' -> '1500', '1,234,567' -> '1234567', '1,234.567' -> '1234.567'.

    The decimal separator is kept. Dropping it would let a lost comma through
    silently, and a lost comma is a factor of ten in an amount. '1,234,567' and
    '1,234.567' must also not collapse to the same value, because that is a
    factor of a thousand.
    """
    s = raw.replace("\u00a0", "").replace(" ", "")
    seps = [(i, c) for i, c in enumerate(s) if c in ".,"]
    if not seps:
        return s
    kinds = {c for _i, c in seps}
    if len(kinds) == 2:
        dec_i = seps[-1][0]                       # both characters: the last one is decimal
    elif len(seps) > 1:
        return re.sub(r"[.,]", "", s)             # same character repeated: thousands only
    else:
        i, _c = seps[0]
        if len(s) - i - 1 == 3:
            return re.sub(r"[.,]", "", s)         # one separator, three digits after: thousands
        dec_i = i
    head = re.sub(r"[.,]", "", s[:dec_i])
    tail = s[dec_i + 1:]
    return (head + "." + tail) if tail else head


def _numbers(t):
    return sorted(_norm_number(m.group(0)) for m in NUM_RE.finditer(t) if m.group(0))


def _tag_diff(si, ti):
    from collections import Counter
    cs, ct = Counter(si), Counter(ti)
    miss = sorted((cs - ct).elements())
    extra = sorted((ct - cs).elements())
    def fmt(xs):
        return ", ".join("%s%s" % (n, i) for n, i, _r in xs) if xs else "none"
    return fmt(miss), fmt(extra)


def _stem(term):
    """The stem of a term. A glossary holds base forms; running text inflects them."""
    t = term.strip().lower()
    return t[:max(4, int(len(t) * 0.7))] if len(t) > 4 else t


def qa_check(doc, glossary=None, forbidden=None):
    """Returns a list of {unit, key, rule, severity, message}."""
    glossary = glossary or []
    forbidden = forbidden or []
    issues = []
    by_src = {}
    by_tgt = {}

    gloss = [(g.get("source", ""), g.get("target", ""),
              re.compile(r"(?<!\w)%s(?!\w)" % re.escape(g.get("source", "")), re.IGNORECASE),
              _stem(g.get("target", "")))
             for g in glossary if g.get("source") and g.get("target")]

    def add(u, rule, sev, msg):
        issues.append({"unit": u["id"], "key": u["key"], "rule": rule,
                       "severity": sev, "message": msg})

    for u in doc.units:
        src = plain(u["src_tokens"])
        tgt = plain(u["tgt_tokens"])
        si, ti = tag_inventory(u["src_tokens"]), tag_inventory(u["tgt_tokens"])
        translatable = u["translate"] != "no"
        if not translatable:
            continue

        # A segment built only of tags, a hyperlink or a field, still has to match
        if not src.strip():
            if si and sorted(si) != sorted(ti):
                miss, extra = _tag_diff(si, ti)
                add(u, "tag-mismatch", "error",
                    "segment with no text, tags do not match: missing %s, extra %s"
                    % (miss, extra))
            continue

        if not tgt.strip():
            add(u, "empty-target", "error", "empty target where the source is not empty")
            continue

        if sorted(si) != sorted(ti):
            miss, extra = _tag_diff(si, ti)
            add(u, "tag-mismatch", "error",
                "tags do not match: missing %s, extra %s" % (miss, extra))
        elif si != ti:
            add(u, "tag-order", "warning", "tag order differs from the source")

        ns, nt = _numbers(src), _numbers(tgt)
        if ns != nt:
            if ns and nt:
                add(u, "number-mismatch", "warning",
                    "numbers differ, source %s against target %s" % (ns, nt))
            else:
                add(u, "number-mismatch", "info",
                    "numbers on one side only: source %s, target %s"
                    % (ns or "none", nt or "none"))

        if src[:1].isspace() != tgt[:1].isspace():
            add(u, "leading-space", "warning", "leading whitespace differs from the source")
        if src[-1:].isspace() != tgt[-1:].isspace():
            add(u, "trailing-space", "warning", "trailing whitespace differs from the source")

        if WORD_RE.search(src) and src.strip() == tgt.strip() and len(src.strip()) > 3:
            add(u, "same-as-source", "warning", "target is identical to the source")

        # Typography rules fire only when the source does NOT have the same thing,
        # otherwise a faithful copy of a sloppy source raises an alarm.
        if "  " in tgt.strip() and "  " not in src.strip():
            add(u, "double-space", "warning", "double space")
        if re.search(r"\s+[,.;:!?]", tgt) and not re.search(r"\s+[,.;:!?]", src):
            add(u, "space-before-punct", "warning", "space before a punctuation mark")

        m = re.search(r"(?<!\w)([^\W\d_]{3,})\s+\1(?!\w)", tgt, re.IGNORECASE | re.UNICODE)
        if m:
            add(u, "repeated-word", "warning", "repeated word: %s" % m.group(1))

        # The third pair is the Polish quotation mark, which is the target language
        # this was built for. Add your own pair here for another target language.
        for op, cl, label in (("(", ")", "parenthesis"), ("[", "]", "square bracket"),
                              ("\u201e", "\u201d", "quotation mark")):
            if tgt.count(op) != tgt.count(cl) and src.count(op) == src.count(cl):
                add(u, "unbalanced", "warning", "unbalanced %s" % label)

        if re.search(r"„\s", tgt) or re.search(r"\s”", tgt):
            add(u, "quote-spacing", "warning", "space against a quotation mark")

        low_t = tgt.lower()
        for se, te, rx, stem in gloss:
            if rx.search(src) and te.lower() not in low_t and stem not in low_t:
                add(u, "glossary", "warning",
                    "glossary: %s, expected %s" % (se, te))
        for bad in forbidden:
            if bad and bad.lower() in low_t:
                add(u, "forbidden", "error", "forbidden string: %s" % bad)

        by_src.setdefault(re.sub(r"\s+", " ", src.strip()), set()).add(
            re.sub(r"\s+", " ", tgt.strip()))
        by_tgt.setdefault(re.sub(r"\s+", " ", tgt.strip()), set()).add(
            re.sub(r"\s+", " ", src.strip()))

    for u in doc.units:
        if u["translate"] == "no":
            continue
        src = re.sub(r"\s+", " ", plain(u["src_tokens"]).strip())
        tgt = re.sub(r"\s+", " ", plain(u["tgt_tokens"]).strip())
        if not src or not tgt:
            continue
        if len(by_src.get(src, ())) > 1:
            issues.append({"unit": u["id"], "key": u["key"], "rule": "inconsistent-target",
                           "severity": "warning",
                           "message": "the same source has %d different translations"
                                      % len(by_src[src])})
        if len(by_tgt.get(tgt, ())) > 1 and len(tgt) > 12:
            issues.append({"unit": u["id"], "key": u["key"], "rule": "inconsistent-source",
                           "severity": "info",
                           "message": "the same target is used for %d different sources"
                                      % len(by_tgt[tgt])})
    return issues


# --------------------------------------------------------------------------
# glossaries
# --------------------------------------------------------------------------

def parse_glossary(data, filename):
    """A two column CSV or TSV, or a .docx holding a two column table."""
    name = filename.lower()
    pairs = []
    if name.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        for row in re.findall(r"<w:tr[ >].*?</w:tr>", xml, re.S):
            cells = re.findall(r"<w:tc[ >].*?</w:tc>", row, re.S)
            vals = []
            for c in cells:
                txt = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", c, re.S))
                vals.append(unescape(re.sub(r"<[^>]+>", "", txt)).strip())
            if len(vals) >= 2 and vals[0] and vals[1]:
                pairs.append({"source": vals[0], "target": vals[1]})
    else:
        text = data.decode("utf-8-sig", "replace")
        sep = "\t" if text.count("\t") > text.count(",") else ","
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = [p.strip().strip('"') for p in line.split(sep)]
            if len(parts) >= 2 and parts[0] and parts[1]:
                pairs.append({"source": parts[0], "target": parts[1]})
    # Header words are data, not prose: a real glossary delivered in Polish has a
    # Polish header row, and dropping those spellings would stop detecting it.
    if pairs and pairs[0]["source"].lower() in ("source", "en", "english",
                                                "\u017ar\u00f3d\u0142o", "zrodlo"):
        pairs = pairs[1:]
    return pairs
