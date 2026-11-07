#!/usr/bin/env python3
"""Engine tests. Every one of these is a regression: each defect found in review
left a test behind, and the names say what broke.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import io
import sys
import unittest
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402
from fixtures import CF_E, CF_S, FOOT, HEAD, SKELETON, one, sample_xlf, sample_xlz, unit  # noqa: E402
from xliffedit import (Package, XliffDoc, parse_glossary, plain, qa_check,  # noqa: E402
                       real_tags, serialize, tag_inventory)
from xliffedit.core import _norm_number  # noqa: E402


def well_formed(text):
    try:
        ElementTree.fromstring(text)
        return True, ""
    except ElementTree.ParseError as error:
        return False, str(error)


class Parsing(unittest.TestCase):
    def setUp(self):
        self.doc = XliffDoc(sample_xlf())

    def test_every_segment_is_found(self):
        self.assertEqual(len(self.doc.units), 11)

    def test_keys_are_ordinals_not_ids(self):
        self.assertEqual([u["key"] for u in self.doc.units],
                         [str(i) for i in range(11)])

    def test_translate_no_is_recognised(self):
        self.assertEqual(self.doc.unit("1")["translate"], "no")

    def test_a_gt_inside_an_attribute_does_not_end_the_tag(self):
        """Where a regex based parser gives up on real delivery files."""
        self.assertEqual(plain(self.doc.unit("8")["src_tokens"]),
                         "Attr with > inside")

    def test_a_missing_target_is_distinct_from_an_empty_one(self):
        self.assertIs(self.doc.unit("5")["has_target"], False)
        self.assertTrue(self.doc.unit("4")["has_target"])
        self.assertEqual(plain(self.doc.unit("4")["tgt_tokens"]), "")

    def test_tags_are_inventoried(self):
        self.assertEqual(tag_inventory(self.doc.unit("2")["tgt_tokens"]),
                         [("bpt", "1", "atomic"), ("ept", "1", "atomic")])

    def test_alt_trans_is_not_mistaken_for_the_target(self):
        self.assertEqual(plain(self.doc.unit("7")["tgt_tokens"]), "Nowe tlumaczenie")

    def test_entities_are_decoded(self):
        self.assertEqual(plain(self.doc.unit("3")["tgt_tokens"]),
                         "§ 12(b) & wiecej")

    def test_a_numeric_entity_is_a_character_not_markup(self):
        """&#160; is a non breaking space, so it must decode to U+00A0 and not to
        an ordinary space. Reading it as a space would quietly change the text."""
        self.assertEqual(plain(self.doc.unit("6")["tgt_tokens"]),
                         "Twarda\u00a0spacja")
        self.assertIn("\u00a0", plain(self.doc.unit("6")["tgt_tokens"]))

    def test_declared_languages_are_read(self):
        self.assertEqual(self.doc.source_language, "en-US")
        self.assertEqual(self.doc.target_language, "pl-PL")


class UnsupportedInput(unittest.TestCase):
    """An unsupported file must say so, not open as an empty document.

    XLIFF 2.0 restructured the format, so a 2.0 delivery is valid XML that
    contains no <trans-unit>. It used to parse to zero segments in silence, which
    a translator reads as a corrupt file rather than an unsupported version.
    """

    def test_xliff_2_0_is_refused_by_namespace(self):
        blob = (b'<?xml version="1.0"?><xliff '
                b'xmlns="urn:oasis:names:tc:xliff:document:2.0" version="2.0" '
                b'srcLang="en" trgLang="pl"><file id="f1"><unit id="1"><segment>'
                b'<source>Hi</source><target>Czesc</target></segment></unit>'
                b'</file></xliff>')
        with self.assertRaises(ValueError) as caught:
            XliffDoc(blob)
        self.assertIn("2.x", str(caught.exception))

    def test_xliff_2_1_is_refused_by_version(self):
        blob = (b'<?xml version="1.0"?><xliff version="2.1" srcLang="en">'
                b'<file id="f1"><unit id="1"><segment><source>Hi</source>'
                b'</segment></unit></file></xliff>')
        with self.assertRaises(ValueError) as caught:
            XliffDoc(blob)
        self.assertIn("2.x", str(caught.exception))

    def test_a_non_xliff_file_says_so(self):
        with self.assertRaises(ValueError) as caught:
            XliffDoc(b'<?xml version="1.0"?><root><a>x</a></root>')
        self.assertIn("XLIFF", str(caught.exception))

    def test_an_empty_but_valid_1_2_file_says_so(self):
        blob = (b'<?xml version="1.0"?><xliff version="1.2"><file original="x" '
                b'source-language="en" target-language="pl" datatype="x"><body>'
                b'</body></file></xliff>')
        with self.assertRaises(ValueError) as caught:
            XliffDoc(blob)
        self.assertIn("nothing to edit", str(caught.exception))


class NumericEntities(unittest.TestCase):
    """Regression: decimal and hexadecimal entities, and no double escaping.

    The em dash characters below are DATA, not prose: this asserts that &#8212;
    decodes to U+2014. A sweep that recasts dashes in written text must not touch
    them, or it deletes the test while reporting success.
    """

    def setUp(self):
        self.raw = (HEAD + unit("1", "Caf&#233; &#8212; test &#xA7;",
                                "Kawiarnia &#243;&#8212;&#167;") + FOOT).encode("utf-8")
        self.doc = XliffDoc(self.raw)

    def test_decimal_and_hex_entities_decode(self):
        self.assertEqual(plain(self.doc.unit("0")["src_tokens"]),
                         "Café — test §")
        self.assertEqual(plain(self.doc.unit("0")["tgt_tokens"]),
                         "Kawiarnia ó—§")

    def test_untouched_file_is_returned_verbatim(self):
        self.assertEqual(self.doc.to_bytes(), self.raw)

    def test_an_edit_does_not_double_escape(self):
        self.doc.set_target_tokens("0", [{"kind": "text", "text": "ó—§ zmiana"}])
        out = self.doc.to_bytes().decode("utf-8")
        self.assertNotIn("&amp;#", out)


class RoundTrip(unittest.TestCase):
    """The headline claim, and the reason this exists."""

    def test_no_edit_returns_the_input_bytes(self):
        raw = sample_xlf()
        self.assertEqual(XliffDoc(raw).to_bytes(), raw)

    def test_one_edit_changes_exactly_one_place(self):
        raw = sample_xlf()
        doc = XliffDoc(raw)
        doc.set_target_tokens("0", [{"kind": "text", "text": "Witaj swiecie!"}])
        out = doc.to_bytes().decode("utf-8")
        self.assertEqual(out.replace("Witaj swiecie!", "Witaj swiecie."),
                         raw.decode("utf-8"))

    def test_an_edit_leaves_another_segment_entity_for_entity(self):
        """The real claim, stated precisely. Editing one segment must not touch
        how any other segment spells its characters. A re-serialising editor
        rewrites every &#160; as a literal character, and then every line of the
        delivery reads as changed to whoever reviews the diff.

        Note what is NOT claimed: the edited segment itself is written as plain
        text, because its content is new. Only untouched bytes are preserved.
        """
        doc = XliffDoc(sample_xlf())
        doc.set_target_tokens("0", [{"kind": "text", "text": "Zmiana"}])
        out = doc.to_bytes().decode("utf-8")

        # Segment 6 was not touched, so its entity must still be an entity.
        sixth = out[out.index("Twarda") - 80:out.index("Twarda") + 40]
        self.assertIn("&#160;", sixth, "an untouched segment was re-encoded: %r" % sixth)
        # And the source of segment 7, likewise untouched.
        self.assertIn("Non&#160;breaking", out)

    def test_crlf_survives(self):
        crlf = sample_xlf().replace(b"\n", b"\r\n")
        self.assertEqual(XliffDoc(crlf).to_bytes(), crlf)

    def test_a_byte_order_mark_survives(self):
        bom = b"\xef\xbb\xbf" + sample_xlf()
        self.assertEqual(XliffDoc(bom).to_bytes(), bom)

    def test_every_declared_encoding_survives(self):
        text = sample_xlf().replace(b'encoding="utf-8"',
                                    b'encoding="utf-16"').decode("utf-8")
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
            blob = text.encode(encoding)
            with self.subTest(encoding=encoding):
                self.assertEqual(XliffDoc(blob).to_bytes(), blob)

    def test_utf16_without_a_bom_still_parses(self):
        text = sample_xlf().replace(b'encoding="utf-8"',
                                    b'encoding="utf-16"').decode("utf-8")
        self.assertEqual(len(XliffDoc(text.encode("utf-16-le")).units), 11)


class SelfClosingTarget(unittest.TestCase):
    """Regression: <target/> is a target, and writing it must stay valid XML."""

    def test_a_self_closing_target_exists(self):
        raw = (HEAD + unit("1", "Text here", raw_target='<target state="new"/>')
               + FOOT).encode("utf-8")
        doc = XliffDoc(raw)
        self.assertTrue(doc.unit("0")["has_target"])
        doc.set_target_tokens("0", [{"kind": "text", "text": "Tekst"}])
        out = doc.to_bytes().decode("utf-8")
        self.assertNotIn("<target/ ", out)
        ok, detail = well_formed(out)
        self.assertTrue(ok, detail)

    def test_a_bare_self_closing_target_takes_attributes(self):
        raw = (HEAD + unit("1", "A", raw_target="<target/>") + FOOT).encode("utf-8")
        doc = XliffDoc(raw)
        doc.set_target_tokens("0", [{"kind": "text", "text": "B"}])
        doc.set_state("0", "translated")
        self.assertIn('<target state="translated">B</target>',
                      doc.to_bytes().decode("utf-8"))


class StateAttribute(unittest.TestCase):
    def setUp(self):
        raw = (HEAD + unit("1", "A", raw_target="<target state='needs-translation'>B</target>")
               + FOOT).encode("utf-8")
        self.doc = XliffDoc(raw)

    def test_a_single_quoted_attribute_is_read(self):
        self.assertEqual(self.doc.unit("0")["tgt_state"], "needs-translation")

    def test_a_single_quoted_attribute_is_replaced(self):
        self.doc.set_state("0", "translated")
        out = self.doc.to_bytes().decode("utf-8")
        self.assertIn("translated", out)
        self.assertNotIn("needs-translation", out)

    def test_an_invented_state_is_refused(self):
        """It would otherwise be written straight into the file."""
        with self.assertRaises(ValueError):
            self.doc.set_state("0", '"><script>')


class DuplicateIds(unittest.TestCase):
    """Regression, and the reason segments are addressed by key.

    XLIFF permits the same id in different <file> sections. Addressing by id put
    an edit into a different segment, which is the worst defect this tool could
    have: it corrupts a delivery silently.
    """

    def setUp(self):
        raw = (HEAD + unit("u1", "First file text", "Pierwszy")
               + '</body>\n</file>\n'
               '<file original="b.docx" source-language="en-US" '
               'target-language="pl-PL" datatype="x">\n<body>\n'
               + unit("u1", "Second file text", "Drugi") + FOOT)
        self.doc = XliffDoc(raw.encode("utf-8"))

    def test_both_segments_are_visible(self):
        self.assertEqual(len(self.doc.units), 2)

    def test_same_id_different_keys(self):
        first, second = self.doc.units
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["id"], "u1")
        self.assertNotEqual(first["key"], second["key"])

    def test_an_edit_lands_in_the_addressed_segment(self):
        self.doc.set_target_tokens("1", [{"kind": "text", "text": "CHANGED SECOND"}])
        out = self.doc.to_bytes().decode("utf-8")
        self.assertEqual(out.count("CHANGED SECOND"), 1)
        self.assertIn("Pierwszy</target>", out)


class CommentsAndCdata(unittest.TestCase):
    def test_a_commented_out_segment_is_not_a_segment(self):
        raw = (HEAD + "<!--" + unit("ghost", "Ghost", "Duch") + "-->\n"
               + unit("real", "Real", "Prawdziwy") + FOOT).encode("utf-8")
        doc = XliffDoc(raw)
        self.assertEqual([u["id"] for u in doc.units], ["real"])
        self.assertEqual(doc.to_bytes(), raw)

    def test_a_comment_inside_a_target_is_not_text(self):
        raw = (HEAD + unit("1", "A", "Tekst<!-- note -->dalej") + FOOT).encode("utf-8")
        doc = XliffDoc(raw)
        self.assertEqual(plain(doc.unit("0")["tgt_tokens"]), "Tekstdalej")
        doc.set_target_tokens("0", doc.unit("0")["tgt_tokens"])
        self.assertIn("<!-- note -->", doc.to_bytes().decode("utf-8"))

    def test_cdata_is_kept_and_is_not_text(self):
        raw = (HEAD + unit("1", "A", "X<![CDATA[ <b>raw</b> ]]>Y") + FOOT).encode("utf-8")
        doc = XliffDoc(raw)
        self.assertEqual(plain(doc.unit("0")["tgt_tokens"]), "XY")
        self.assertEqual(doc.to_bytes(), raw)


class SegSource(unittest.TestCase):
    def test_a_new_target_goes_after_seg_source(self):
        """Inserting it before produced a file no tool would open."""
        raw = (HEAD + '\t\t\t<trans-unit id="1" translate="yes">\n'
               '\t\t\t\t<source>A</source>\n'
               '\t\t\t\t<seg-source><mrk mtype="seg" mid="1">A</mrk></seg-source>\n'
               '\t\t\t</trans-unit>\n' + FOOT).encode("utf-8")
        doc = XliffDoc(raw)
        doc.set_target_tokens("0", [{"kind": "text", "text": "Tlumaczenie"}])
        out = doc.to_bytes().decode("utf-8")
        self.assertLess(out.index("</seg-source>"), out.index("<target"))
        ok, detail = well_formed(out)
        self.assertTrue(ok, detail)


class Tags(unittest.TestCase):
    def test_a_tag_is_written_back_verbatim(self):
        doc = XliffDoc(sample_xlf())
        tokens = [t for t in doc.unit("2")["tgt_tokens"] if t["kind"] == "tag"]
        doc.set_target_tokens("2", tokens + [{"kind": "text", "text": "X"}])
        out = doc.to_bytes().decode("utf-8")
        self.assertIn('<bpt id="1">&lt;cf italic="on" size="14"&gt;</bpt>', out)
        self.assertNotIn("&amp;lt;cf", out)

    def test_placeholders_map_onto_tags(self):
        doc = XliffDoc(sample_xlf())
        doc.set_target_text("2", "{1}Wystawa zimowa{2} trasa")
        self.assertEqual(plain(doc.unit("2")["tgt_tokens"]), "Wystawa zimowa trasa")
        self.assertEqual(len(real_tags(doc.unit("2")["tgt_tokens"])), 2)

    def test_a_wrong_placeholder_set_is_refused(self):
        """Silently dropping a tag changes the delivery's formatting."""
        doc = XliffDoc(sample_xlf())
        for text in ("no placeholders at all", "{0}x", "{99}x"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    doc.set_target_text("2", text)

    def test_tags_without_an_id_still_get_distinct_identities(self):
        raw = (HEAD + unit("1", 'A<mrk mtype="x">B</mrk>C<mrk mtype="y">D</mrk>',
                           'A<mrk mtype="x">B</mrk>C<mrk mtype="y">D</mrk>')
               + FOOT).encode("utf-8")
        doc = XliffDoc(raw)
        tags = real_tags(doc.unit("0")["tgt_tokens"])
        self.assertEqual(len({t["tid"] for t in tags}), len(tags))
        doc.set_target_tokens("0", list(doc.unit("0")["tgt_tokens"]))
        self.assertEqual(doc.to_bytes(), raw)


class InsertTarget(unittest.TestCase):
    def test_a_missing_target_is_created(self):
        doc = XliffDoc(sample_xlf())
        doc.set_target_tokens("5", [{"kind": "text", "text": "Teraz jest target."}])
        out = doc.to_bytes().decode("utf-8")
        self.assertIn('<target state="translated">Teraz jest target.</target>', out)
        again = XliffDoc(out.encode("utf-8"))
        self.assertEqual(len(again.units), 11)
        self.assertEqual(plain(again.unit("5")["tgt_tokens"]), "Teraz jest target.")


class Archive(unittest.TestCase):
    def test_an_untouched_archive_comes_back_byte_for_byte(self):
        blob = sample_xlz()
        self.assertEqual(Package(blob, "x.xlz").to_bytes(), blob)

    def test_an_edit_leaves_the_skeleton_and_the_entry_order_alone(self):
        blob = sample_xlz()
        package = Package(blob, "x.xlz")
        package.doc.set_target_tokens("0", [{"kind": "text", "text": "Zmiana"}])
        with zipfile.ZipFile(io.BytesIO(package.to_bytes())) as archive:
            self.assertEqual(archive.read("skeleton.skl"), SKELETON)
            self.assertIn(b"Zmiana</target>", archive.read("content.xlf"))
            self.assertEqual(archive.namelist(), ["content.xlf", "skeleton.skl"])

    def test_a_bare_xlf_is_accepted(self):
        package = Package(sample_xlf(), "x.xlf")
        self.assertFalse(package.is_zip)
        self.assertEqual(len(package.doc.units), 11)

    def test_a_zip_with_no_xlf_is_refused(self):
        with self.assertRaises(ValueError):
            Package(sample_xlz().replace(b"content.xlf", b"content.bin"), "b.xlz")

    def test_a_zip_bomb_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("content.xlf", sample_xlf())
            archive.writestr("big.bin", b"\0" * (600 * 1024 * 1024))
        with self.assertRaises(ValueError):
            Package(buf.getvalue(), "bomb.xlz")


class QualityRules(unittest.TestCase):
    """Each rule, and for most of them the false alarm it must not raise."""

    def rules(self, src, tgt, **kw):
        return {i["rule"] for i in qa_check(XliffDoc(one(src, tgt)), **kw)}

    def test_empty_target(self):
        self.assertIn("empty-target", self.rules("Something", ""))

    def test_empty_target_is_fine_when_the_segment_is_locked(self):
        doc = XliffDoc(one("S", "", translate="no"))
        self.assertNotIn("empty-target", {i["rule"] for i in qa_check(doc)})

    def test_tag_mismatch(self):
        self.assertIn("tag-mismatch", self.rules(CF_S + "A" + CF_E, "A"))

    def test_matching_tags_raise_nothing(self):
        self.assertNotIn("tag-mismatch",
                         self.rules(CF_S + "A" + CF_E, CF_S + "A" + CF_E))

    def test_the_mismatch_message_names_the_tag(self):
        issues = qa_check(XliffDoc(one(CF_S + "A" + CF_E, CF_S + "A" + CF_E + CF_E)))
        message = next((i["message"] for i in issues
                        if i["rule"] == "tag-mismatch"), "")
        self.assertIn("ept1", message)

    def test_a_tag_only_segment_emptied_is_caught(self):
        self.assertIn("tag-mismatch",
                      self.rules('<ph id="1">&lt;hyperlink&gt;</ph>', ""))

    def test_numbers_that_differ(self):
        self.assertIn("number-mismatch", self.rules("Paid 1500 USD", "Zaplacono 1550 USD"))

    def test_a_different_thousands_separator_is_not_a_mismatch(self):
        self.assertNotIn("number-mismatch",
                         self.rules("Paid 1,500 USD", "Zaplacono 1 500 USD"))

    def test_a_lost_decimal_separator_is_a_mismatch(self):
        """A factor of ten in an amount, which is what this rule exists for."""
        self.assertIn("number-mismatch",
                      self.rules("rate of 1.5 percent", "stopa 15 procent"))

    def test_target_identical_to_source(self):
        self.assertIn("same-as-source", self.rules("Not translated", "Not translated"))

    def test_double_space(self):
        self.assertIn("double-space", self.rules("A b", "A  b"))

    def test_a_double_space_copied_from_the_source_is_not_flagged(self):
        self.assertNotIn("double-space", self.rules("A  b", "A  b jakis inny tekst"))

    def test_repeated_word(self):
        self.assertIn("repeated-word", self.rules("the cat", "kot kot"))

    def test_digit_groups_are_not_a_repeated_word(self):
        self.assertNotIn("repeated-word",
                         self.rules("1 000 000 USD", "1 000 000 USD rocznie"))

    def test_space_before_punctuation(self):
        self.assertIn("space-before-punct", self.rules("a, b", "a , b"))

    def test_unbalanced_bracket(self):
        self.assertIn("unbalanced", self.rules("(a)", "(a"))

    def test_space_against_a_quotation_mark(self):
        self.assertIn("quote-spacing",
                      self.rules('"Quoted"', "„ Cytat”"))

    def test_a_clean_segment_raises_nothing(self):
        found = self.rules("The office opens at nine.",
                           "Biuro otwiera sie o dziewiatej.")
        self.assertEqual(found, set(), str(found))

    def test_glossary_violation(self):
        gloss = [{"source": "Site Manager", "target": "kierownik"}]
        self.assertIn("glossary", self.rules("The Site Manager said",
                                             "Zarzadca powiedzial", glossary=gloss))

    def test_glossary_compliance(self):
        gloss = [{"source": "Site Manager", "target": "kierownik"}]
        self.assertNotIn("glossary", self.rules("The Site Manager said",
                                                "Kierownik powiedzial", glossary=gloss))

    def test_an_inflected_form_satisfies_the_glossary(self):
        """A glossary holds base forms and running text inflects them. Without
        this the rule fires on every correct Polish sentence."""
        gloss = [{"source": "Site Manager", "target": "kierownik"}]
        self.assertNotIn("glossary", self.rules("The Site Manager said",
                                                "Kierownikowi przekazano", glossary=gloss))

    def test_glossary_matches_on_a_word_boundary_not_a_substring(self):
        gloss = [{"source": "art", "target": "sztuka"}]
        self.assertNotIn("glossary", self.rules("The cart was here",
                                                "Wozek tu byl", glossary=gloss))

    def test_forbidden_string(self):
        self.assertIn("forbidden", self.rules("A", "B", forbidden=["B"]))

    def test_inconsistency_across_segments(self):
        doc = XliffDoc(sample_xlf())
        self.assertIn("inconsistent-target", {i["rule"] for i in qa_check(doc)})

    def test_every_issue_carries_a_segment_key(self):
        self.assertTrue(all("key" in i for i in qa_check(XliffDoc(sample_xlf()))))


class Glossaries(unittest.TestCase):
    def test_csv_with_a_header(self):
        data = "Source,Target\nSite Manager,kierownik\nvisitor,zwiedzajacy\n".encode("utf-8")
        pairs = parse_glossary(data, "g.csv")
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0]["source"], "Site Manager")

    def test_tsv_without_a_header(self):
        self.assertEqual(len(parse_glossary(b"a\tb\nc\td\n", "g.tsv")), 2)

    def test_a_docx_two_column_table(self):
        document = ('<?xml version="1.0"?><w:document xmlns:w="w"><w:body><w:tbl>'
                    '<w:tr><w:tc><w:p><w:r><w:t>Site Manager</w:t></w:r></w:p></w:tc>'
                    '<w:tc><w:p><w:r><w:t>kierownik</w:t></w:r></w:p></w:tc></w:tr>'
                    '</w:tbl></w:body></w:document>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("word/document.xml", document)
        self.assertEqual(parse_glossary(buf.getvalue(), "g.docx"),
                         [{"source": "Site Manager", "target": "kierownik"}])


class TagIdentity(unittest.TestCase):
    """Regression: a tag's identity must survive a save and a fresh parse.

    The server stores edits as token references, not as text. If a reference
    cannot be resolved after reloading the original file, the saved edit is
    unrecoverable, so this is the test that keeps that from happening quietly.
    """

    def test_source_tags_keep_their_identity_when_one_is_reused(self):
        doc = XliffDoc(sample_xlf())
        unit_two = doc.unit("2")
        before = [t["sref"] for t in unit_two["src_tokens"] if t["kind"] == "tag"]
        self.assertEqual(before, ["s0", "s1"])
        self.assertEqual([t["sref"] for t in unit_two["tgt_tokens"]
                          if t["kind"] == "tag"], [0, 1])
        source_tag = [t for t in unit_two["src_tokens"] if t["kind"] == "tag"][0]
        doc.set_target_tokens("2", [source_tag, {"kind": "text", "text": "X"}])
        after = [t["sref"] for t in doc.unit("2")["src_tokens"] if t["kind"] == "tag"]
        self.assertEqual(after, before)

    def test_a_saved_edit_replays_onto_a_freshly_parsed_file(self):
        doc = XliffDoc(sample_xlf())
        source_tag = [t for t in doc.unit("2")["src_tokens"] if t["kind"] == "tag"][0]
        doc.set_target_tokens("2", [source_tag, {"kind": "text", "text": "X"}])
        saved = [({"kind": "tag", "ref": t.get("sref")} if t["kind"] == "tag"
                  else {"kind": "text", "text": t["text"]})
                 for t in doc.unit("2")["tgt_tokens"]]

        fresh = XliffDoc(sample_xlf())
        unit_two = fresh.unit("2")
        pool = {t["sref"]: t for t in unit_two["tgt_tokens"] if t["kind"] == "tag"}
        pool.update({t["sref"]: t for t in unit_two["src_tokens"] if t["kind"] == "tag"})
        missing = [t["ref"] for t in saved
                   if t["kind"] == "tag" and t["ref"] not in pool]
        self.assertEqual(missing, [], "unresolvable references: %s" % missing)
        fresh.set_target_tokens("2", [
            pool[t["ref"]] if t["kind"] == "tag" else {"kind": "text", "text": t["text"]}
            for t in saved])
        self.assertEqual(serialize(fresh.unit("2")["tgt_tokens"]),
                         serialize(doc.unit("2")["tgt_tokens"]))


class NumberNormalisation(unittest.TestCase):
    """Regression. Collapsing separators turns a wrong amount into a clean pass."""

    def test_known_shapes(self):
        for raw, want in (("1,234,567", "1234567"), ("1,234.567", "1234.567"),
                          ("1,500", "1500"), ("1.5", "1.5"), ("1 500,00", "1500.00"),
                          ("0.5", "0.5"), ("10.000", "10000")):
            with self.subTest(raw=raw):
                self.assertEqual(_norm_number(raw), want)

    def test_a_thousand_fold_difference_is_not_normalised_away(self):
        self.assertNotEqual(_norm_number("1,234,567"), _norm_number("1,234.567"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
