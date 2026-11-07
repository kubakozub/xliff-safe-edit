# xliff-safe-edit

[![tests](https://github.com/kubakozub/xliff-safe-edit/actions/workflows/tests.yml/badge.svg)](https://github.com/kubakozub/xliff-safe-edit/actions/workflows/tests.yml)

A browser based XLIFF 1.2 editor that rewrites only the `<target>` elements you
actually edited, and returns every other byte of the delivery unchanged. Edit
nothing and you get the input file back, byte for byte, archive and all.

```console
$ python3 examples/roundtrip_demo.py
open and save with no edit: identical bytes
archive  2657 bytes -> 2658 bytes
content.xlf lines changed by that one edit: 1 of 40
   -			<target state="translated" state-qualifier="leveraged-mt" tms:origin="mt">Witaj swiecie.</ta
   +			<target state="translated" state-qualifier="leveraged-mt" tms:origin="mt">Witaj, swiecie!</t
skeleton.skl unchanged: True
entry order preserved:  True
```

Python 3.9+, standard library only, no dependencies, no build step, nothing
leaves the machine.

## Why byte preservation is the whole point

A translation delivery goes back into a tool chain that diffs it. Open a
delivery in an editor that parses the XML and writes it out again, and you get a
file where the content is correct and **every line looks changed**: entities
respelled, attribute order shuffled, whitespace normalised, `&#160;` turned into
a literal character. The reviewer on the other end has no way to see the three
segments you fixed, so they stop reviewing, and the tool that ingests it may
reject the file outright.

So the engine never re-serialises. It locates the exact byte range of each
`<target>` and splices. Everything else, including the `skeleton.skl` inside a
`.xlz` and the order of entries in the archive, is passed through untouched.

That claim is tested rather than asserted, and the test says exactly what is and
is not promised: an edited segment is written as plain text because its content
is new, while **no other segment changes how it spells anything**.

## Run it

```bash
pip install xliff-safe-edit
xliff-safe-edit
# XLIFF editor on http://127.0.0.1:8070   user: translator   password: .../data/secret
```

It binds `127.0.0.1` and generates a password into `data/secret` with mode 600
on first run. To use one running on another machine, forward the port rather
than exposing it:

```bash
ssh -L 8070:127.0.0.1:8070 user@your-server
```

Drop a `.xlz` or `.xlf` in, translate, download. `Ctrl`+`Enter` saves and moves
on, `Ctrl`+`1..9` inserts tag N, and tags cannot be split: they are inserted and
deleted whole, because half a `<bpt>` is a broken delivery.

## What it checks while you work

Quality rules run over the whole file on demand, each one written against a
mistake that actually ships:

- **tags**: missing, extra or reordered against the source, including segments
  built only of tags, where emptying the target loses a hyperlink silently
- **numbers**: a differing figure, and specifically a lost decimal separator,
  which is a factor of ten in an amount. `1,500` against `1 500` is not a
  mismatch; `1.5` against `15` is. `1,234,567` and `1,234.567` must never
  normalise to the same value, because that is a factor of a thousand
- **whitespace and typography**: leading and trailing space against the source,
  double spaces, space before punctuation, unbalanced brackets and quotation
  marks, repeated words. Each fires only when the source does not have the same
  thing, so a faithful copy of a sloppy source does not raise an alarm
- **terminology**: a glossary from CSV, TSV or a two column `.docx` table. An
  inflected form counts as compliance, because a glossary holds base forms and
  running text inflects them. Without that the rule fires on every correct
  sentence in an inflected language
- **consistency**: the same source translated two ways, or one target reused for
  two sources, across the whole file

## The limits, up front

**XLIFF 1.2 only.** XLIFF 2.x restructured the format: `<trans-unit>` became
`<unit>` wrapping a `<segment>`. A 2.x file is valid XML that contains no
`<trans-unit>`, so it used to open as an empty document, which reads as a corrupt
delivery rather than an unsupported version. It now refuses with that
explanation. Supporting 2.x safely needs different code, not a looser parser.

**One person, one machine.** There is a single password, no accounts, no audit
trail and no locking between users. It is a desk tool, not a server for a team.

**It is not a CAT tool.** No translation memory, no machine translation, no
project management, no segmentation. It edits a delivery that a real tool chain
produced.

## Security, since it handles client files

The attacks are in the test suite, not just the design:

| | |
|---|---|
| password comparison | `hmac.compare_digest` on bytes, so a non ASCII password is a 401 and not a `TypeError` that drops the connection |
| cross origin | a required `X-XLIFF-Edit` header, which a browser will not add from another origin without a preflight, plus an `Origin` check when one is present |
| content policy | `default-src 'none'` with `self` only for script, style and connect, plus `frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'none'` |
| path handling | file ids must match `[0-9a-f]{16}` before anything is joined to a path, so traversal is refused at the route |
| archives | a 512 MB uncompressed cap, so a zip bomb is refused rather than expanded |
| concurrency | every save is atomic under a lock. Two concurrent saves used to destroy `state.json`, and the symptom was a delivery that quietly lost edits. Twenty threads writing six rounds each is a test |
| output | ids and text from the uploaded file reach the page through `textContent`, never `innerHTML` |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

**102 tests, all offline.** The engine tests and the HTTP tests both run here:
the server is started in process on an ephemeral port bound to `127.0.0.1`, so
CI covers the HTTP surface. Previously these could only be run against an
instance someone had started by hand, which meant the HTTP layer was the part
nobody tested.

Every test is a regression and the names say what broke. The ones worth reading
first:

- **duplicate ids**: XLIFF permits the same `id` in different `<file>` sections.
  Addressing segments by id put an edit into a *different segment*, which is the
  worst thing this tool could do, because it corrupts a delivery silently.
  Segments are addressed by ordinal position now, and two tests keep it that way
- **tag identity**: edits are stored as token references rather than text, so a
  reference that cannot be resolved after reloading the original file would make
  a saved edit unrecoverable. One test replays a saved edit onto a freshly
  parsed file and compares the serialised result
- **encodings**: UTF-8, UTF-16 in three flavours and UTF-32 all round-trip, with
  and without a byte order mark, and CRLF survives
- **a comment is not a segment**: a commented out `<trans-unit>` used to be
  editable

Target text in the fixtures is Polish. That is test data, not prose: a corpus of
ASCII would not exercise the thing most likely to break, which is non ASCII
round-tripping through entities and encodings.

## Where this came from

I translate professionally and I write software. Every editor I could use either
re-serialised the delivery, or ran in the cloud, or cost a licence per seat for
the one job of fixing three segments before a deadline. This does that one job
and leaves every other byte alone.

MIT.
