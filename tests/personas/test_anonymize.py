from proofloop.personas.anonymize import AnonymizationReport, anonymize_record, anonymize_text


def test_removes_contact_details():
    text = ("Hi, I'm Daniel Ortiz. Email me at dan.o@example.com or call +1 (555) 123-4567. "
            "I live at 42 Elm Street, Springfield, IL 62704. Card 4111 1111 1111 1111. @danny_o")
    report = AnonymizationReport()
    out = anonymize_text(text, report)
    for leaked in ("Daniel", "Ortiz", "dan.o@example.com", "555", "42 Elm Street", "62704", "4111", "@danny_o"):
        assert leaked not in out, leaked
    assert {"email", "phone", "address", "name", "number", "handle"} <= set(report.replaced)


def test_keeps_ordinary_words_that_look_like_names():
    out = anonymize_text("Mark the box. Will it work? The rose garden page is slow.")
    assert out == "Mark the box. Will it work? The rose garden page is slow."


def test_names_mid_sentence_are_removed():
    out = anonymize_text("My partner Sarah set it up and then emma helped.")
    assert "Sarah" not in out and "[NAME]" in out


def test_pii_columns_are_dropped():
    rec = anonymize_record({"Name": "Maria Lopez", "Email": "m@x.com", "age": "67",
                            "comments": "Call me on 555-123-4567"})
    assert "Name" not in rec and "Email" not in rec
    assert rec["age"] == "67" and "555" not in rec["comments"]
