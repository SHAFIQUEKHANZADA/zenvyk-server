"""
Tests for the WE OWE extractor.

These cover everything that happens AROUND the vision call -- filename parsing,
normalisation, and the decisions in build_record(). The vision call itself is
tested by running the CLI against the three real samples in ../weowe_files and
reading the output, because a fixture of what the model returned yesterday
proves nothing about what it returns today.

The three sample forms are the fixtures below, transcribed by hand from the
scans:

    ElHondasales_260907_122420   Damian L Zagorski, 2 items owed, no e-mail field
    sthsales_260903_203526       NAME BLANK, 1 item owed, e-mail present
    stksales_202609071811        Scott Charles Smith, NOTHING owed
"""

import pathlib

import pytest

import weowe as w


SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "weowe_files"


# --- filename ---------------------------------------------------------------

@pytest.mark.parametrize("filename,store_key", [
    ("ElHondasales_260907_122420.pdf", "mcgrath_honda_elgin"),
    ("sthsales_260903_203526.pdf", "mcgrath_honda_stcharles"),
    ("stksales_202609071811.pdf", "mcgrath_kia_stcharles"),
    ("something_else.pdf", None),
])
def test_store_from_filename(filename, store_key):
    assert w.store_from_filename(filename)["store_key"] == store_key


@pytest.mark.parametrize("filename,expected", [
    ("ElHondasales_260907_122420.pdf", "2026-09-07T12:24:20"),
    ("sthsales_260903_203526.pdf", "2026-09-03T20:35:26"),
    ("stksales_202609071811.pdf", "2026-09-07T18:11:00"),
    ("no_timestamp_here.pdf", None),
])
def test_scanned_at_from_filename(filename, expected):
    assert w.scanned_at_from_filename(filename) == expected


def test_impossible_timestamp_is_not_a_date():
    # A month of 99 must come back None rather than raising or inventing a date.
    assert w.scanned_at_from_filename("sthsales_269907_122420.pdf") is None


# --- normalisation ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("(224) 623-1009", "+12246231009"),
    ("708-954-6353", "+17089546353"),
    ("1 (708) 807-8930", "+17088078930"),
    ("555-1234", None),          # too short -- better None than a bad dial
    ("", None),
    (None, None),
])
def test_normalise_phone(raw, expected):
    assert w.normalise_phone(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("09/07/2026", "2026-09-07"),
    ("9/3/26", "2026-09-03"),
    ("2026-09-07", "2026-09-07"),
    ("sometime next week", None),
])
def test_normalise_date(raw, expected):
    assert w.normalise_date(raw) == expected


def test_name_from_email_recovers_a_blank_name():
    assert w.name_from_email("gallagher.shannon.r@gmail.com") == "Shannon Gallagher"


def test_name_from_email_gives_up_on_a_handle():
    # smithkaytea is not a name in two parts. Guessing here would put a wrong
    # name in front of a customer.
    assert w.name_from_email("smithkaytea@att.net") is None
    assert w.name_from_email("") is None


# --- build_record -----------------------------------------------------------

ELGIN = {
    "customer_name": "Damian L Zagorski", "address": "9465 Rainsford Dr",
    "city": "Huntley", "state": "IL", "zip_code": "60142",
    "phone": "(224) 623-1009", "email": None,
    "stock_no": "E17675", "condition": "both", "year": "2026",
    "make": "Honda", "model": "Pilot", "vin": "5FNYG1H9XTB057805",
    "salesperson": "Julio Carrasco Irias", "form_date": "09/07/2026",
    "we_owe_items": [
        {"qty": None, "item": "repel (already applied)", "part": None, "labor": None},
        {"qty": None, "item": "undercoating", "part": None, "labor": None},
    ],
    "you_owe_items": [], "nothing_owed_noted": False, "handwritten": False,
}

STCHARLES_HONDA = {
    "customer_name": None, "address": "313 Fieldstone Ct",
    "city": "Bolingbrook", "state": "IL", "zip_code": "60440",
    "phone": "(708) 954-6353", "email": "gallagher.shannon.r@gmail.com",
    "stock_no": "S24682", "condition": None, "year": "2027",
    "make": "Honda", "model": "HR-V", "vin": "3CZRZ2H55VM720692",
    "salesperson": "Mo Akhtar", "form_date": "09/03/2026",
    "we_owe_items": [
        {"qty": None, "item": "Repel - Already Applied - Undercoating",
         "part": None, "labor": None},
    ],
    "you_owe_items": [], "nothing_owed_noted": True, "handwritten": False,
}

KIA_NOTHING_OWED = {
    "customer_name": "Scott Charles Smith", "address": "2452 Imgrund Rd",
    "city": "North Aurora", "state": "IL", "zip_code": "60542",
    "phone": "(708) 807-8930", "email": "smithkaytea@att.net",
    "stock_no": "T1786", "condition": None, "year": "2027",
    "make": "Kia", "model": "Telluride", "vin": "5XYPLES19VG019589",
    "salesperson": "Juan Luviano", "form_date": "09/07/2026",
    "we_owe_items": [], "you_owe_items": [],
    "nothing_owed_noted": True, "handwritten": False,
}


def test_elgin_form_is_tracked_cleanly():
    rec = w.build_record(ELGIN, "ElHondasales_260907_122420.pdf")
    assert rec.store_key == "mcgrath_honda_elgin"
    assert rec.customer_name == "Damian L Zagorski"
    assert rec.customer_name_source == "form"
    assert rec.phone == "+12246231009"
    assert len(rec.we_owe_items) == 2
    assert rec.has_open_items is True
    assert rec.needs_review is False, rec.review_reasons


def test_thirty_day_expiry_is_calculated_from_the_form_date():
    rec = w.build_record(ELGIN, "ElHondasales_260907_122420.pdf")
    assert rec.form_date == "2026-09-07"
    assert rec.expires_on == "2026-10-07"


def test_blank_name_is_recovered_and_flagged():
    rec = w.build_record(STCHARLES_HONDA, "sthsales_260903_203526.pdf")
    assert rec.customer_name == "Shannon Gallagher"
    assert rec.customer_name_source == "email"
    # Recovered, but a human must confirm before we address her by it.
    assert rec.needs_review is True
    assert any("blank" in r.lower() for r in rec.review_reasons)


def test_nothing_owed_form_is_not_tracked():
    # The whole point: this form is filed, not chased.
    rec = w.build_record(KIA_NOTHING_OWED, "stksales_202609071811.pdf")
    assert rec.has_open_items is False
    assert rec.needs_review is False, rec.review_reasons


def test_empty_form_with_no_nothing_owed_note_goes_to_review():
    # No items found AND no note saying nothing is owed. Either it is genuinely
    # empty or the read missed the items -- never silently drop it.
    raw = dict(KIA_NOTHING_OWED, nothing_owed_noted=False)
    rec = w.build_record(raw, "stksales_202609071811.pdf")
    assert rec.has_open_items is False
    assert rec.needs_review is True


def test_preprinted_you_owe_rows_without_a_date_are_not_items():
    # "1) Title to Trade In Vehicle" is on every form ever printed. It only
    # counts when someone wrote a date beside it.
    raw = dict(ELGIN, we_owe_items=[], nothing_owed_noted=True, you_owe_items=[
        {"item": "Title to Trade In Vehicle", "due_date": None},
        {"item": "All Monies", "due_date": None},
    ])
    rec = w.build_record(raw, "ElHondasales_260907_122420.pdf")
    assert rec.you_owe_items == []
    assert rec.has_open_items is False


def test_you_owe_row_with_a_date_is_an_item():
    raw = dict(ELGIN, we_owe_items=[], you_owe_items=[
        {"item": "Title to Trade In Vehicle", "due_date": "09/20/2026"},
    ])
    rec = w.build_record(raw, "ElHondasales_260907_122420.pdf")
    assert len(rec.you_owe_items) == 1
    assert rec.has_open_items is True


def test_short_vin_is_dropped_not_kept():
    raw = dict(ELGIN, vin="5FNYG1H9XTB05")
    rec = w.build_record(raw, "ElHondasales_260907_122420.pdf")
    assert rec.vin is None
    assert any("17" in r for r in rec.review_reasons)


def test_uncontactable_customer_is_flagged():
    raw = dict(ELGIN, phone=None, email=None)
    rec = w.build_record(raw, "ElHondasales_260907_122420.pdf")
    assert rec.needs_review is True
    assert any("cannot be contacted" in r for r in rec.review_reasons)


def test_missing_form_date_flags_the_expiry():
    raw = dict(ELGIN, form_date=None)
    rec = w.build_record(raw, "ElHondasales_260907_122420.pdf")
    assert rec.expires_on is None
    assert any("expiry" in r for r in rec.review_reasons)


def test_handwritten_form_is_flagged_for_review():
    raw = dict(ELGIN, handwritten=True)
    rec = w.build_record(raw, "ElHondasales_260907_122420.pdf")
    assert rec.needs_review is True


def test_unreadable_fields_are_surfaced():
    raw = dict(ELGIN, unreadable_fields=["vin", "phone"])
    rec = w.build_record(raw, "ElHondasales_260907_122420.pdf")
    assert sum("Could not read" in r for r in rec.review_reasons) == 2


# --- loose JSON -------------------------------------------------------------

def test_fenced_json_is_parsed():
    assert w._loads_loose('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_with_a_preamble_is_parsed():
    assert w._loads_loose('Here you go:\n{"a": 1}\nhope that helps') == {"a": 1}


# --- the real files ---------------------------------------------------------

@pytest.mark.skipif(not SAMPLES.exists(), reason="sample scans not present")
def test_samples_have_no_text_layer():
    """If this ever fails, a store changed how it scans and we could read the
    PDF directly instead of paying for a vision call."""
    import pypdf

    for pdf in sorted(SAMPLES.glob("*.pdf")):
        reader = pypdf.PdfReader(str(pdf))
        text = "".join((p.extract_text() or "") for p in reader.pages).strip()
        assert text == "", f"{pdf.name} now has a text layer: {text[:80]!r}"


@pytest.mark.skipif(not SAMPLES.exists(), reason="sample scans not present")
def test_samples_render_to_readable_pages():
    for pdf in sorted(SAMPLES.glob("*.pdf")):
        pages = w.render_pages(pdf.read_bytes())
        assert len(pages) == 1
        # A blank or near-blank render means the page didn't rasterise.
        assert len(pages[0]) > 50_000, f"{pdf.name} rendered to {len(pages[0])} bytes"
