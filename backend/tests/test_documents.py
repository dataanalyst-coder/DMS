import pytest

from app.models import DesignRevision, DocumentVersion
from app.services import designs, documents, items, ledger, storage
from app.services.errors import NotFoundError, PermissionDenied, ValidationError

PDF = b"%PDF-1.4\n%fake but plausible\n"


def new_doc(db, people, key="dwg-100", **kw):
    return documents.create_document(db, people["eng"], doc_key=key, doc_type=kw.get("doc_type", "DIE_DRAWING"),
                                     title="Die 32 drawing")


def test_upload_creates_draft_only_and_needs_engineering(db, people):
    d = new_doc(db, people)
    for who in ("stores", "prod"):
        with pytest.raises(PermissionDenied):
            documents.add_version(db, people[who], d.id, filename="a.pdf", data=PDF)
    v = documents.add_version(db, people["eng"], d.id, filename="a.pdf", data=PDF)
    assert (v.revision, v.status) == ("A", "DRAFT")
    assert documents.current_version(db, d.id) is None  # nothing approved yet: no document for new work


def test_upload_alone_never_releases_an_item_or_approves_a_design(db, people, world, make_item):
    it = make_item(condition="HOLD")
    d = new_doc(db, people)
    documents.link(db, people["eng"], d.id, "ITEM", it.id)
    documents.link(db, people["eng"], d.id, "DESIGN", world["die"])
    documents.add_version(db, people["eng"], d.id, filename="inspection.pdf", data=PDF)
    assert ledger.get_state(db, it.id)["item_condition"] == "HOLD"  # still held
    new_rev = designs.new_revision(db, people["eng"], world["die"], reason="r")
    documents.add_version(db, people["eng"], new_doc(db, people, "dwg-101").id, filename="b.pdf", data=PDF)
    assert db.get(DesignRevision, new_rev.id).status == "DRAFT"  # design revision not approved by upload


def test_approval_needs_engineering_and_supersedes_older_versions(db, people):
    d = new_doc(db, people)
    v1 = documents.add_version(db, people["eng"], d.id, filename="a.pdf", data=PDF)
    for who in ("stores",):
        with pytest.raises(PermissionDenied):
            documents.approve_version(db, people[who], v1.id)
    documents.approve_version(db, people["eng"], v1.id)
    with pytest.raises(ValidationError):  # new revision needs a change reason
        documents.add_version(db, people["eng"], d.id, filename="a2.pdf", data=PDF + b"x")
    v2 = documents.add_version(db, people["eng"], d.id, filename="a2.pdf", data=PDF + b"x",
                               change_reason="bore corrected")
    assert documents.current_version(db, d.id).id == v1.id  # new work still uses v1 until v2 approved
    documents.approve_version(db, people["eng"], v2.id)
    assert documents.current_version(db, d.id).id == v2.id
    detail = documents.document_detail(db, d.id)
    assert [(v["revision"], v["status"]) for v in detail["versions"]] == [("A", "OBSOLETE"), ("B", "APPROVED")]
    # the old file is retained and still readable for history
    _, data = documents.read_version_file(db, v1.id)
    assert data == PDF


def test_upload_validation(db, people):
    d = new_doc(db, people)
    for name, data, why in [("run.exe", b"MZ", "not allowed"), ("page.html", b"<script>", "not allowed"),
                            ("fake.pdf", b"not a pdf", "does not look like"), ("empty.pdf", b"", "empty"),
                            ("noext", PDF, "not allowed")]:
        with pytest.raises(ValidationError) as e:
            documents.add_version(db, people["eng"], d.id, filename=name, data=data)
        assert why in e.value.message
    with pytest.raises(ValidationError):
        documents.add_version(db, people["eng"], d.id, filename="big.pdf",
                              data=PDF + b"0" * (storage.MAX_DOC_BYTES + 1))
    assert db.query(DocumentVersion).count() == 0  # nothing stored for rejected uploads


def test_original_engineering_files_accepted_and_names_sanitised(db, people):
    d = new_doc(db, people, "cad-1", doc_type="TOOL_DRAWING")
    v = documents.add_version(db, people["eng"], d.id, filename="..\\..\\evil/../Die 32 (rev A).step",
                              data=b"ISO-10303-21;")
    assert v.file_name.endswith(".step") and "/" not in v.file_name and "\\" not in v.file_name
    assert ".." not in v.stored_path


def test_stored_files_cannot_be_read_outside_storage(db, people):
    with pytest.raises(NotFoundError):
        storage.read_stored("../../etc/passwd")


def test_links_and_lookup_by_target(db, people, world, make_item):
    it = make_item()
    d = new_doc(db, people)
    documents.link(db, people["eng"], d.id, "DESIGN", world["die"])
    with pytest.raises(ValidationError):
        documents.link(db, people["eng"], d.id, "DESIGN", world["die"])  # duplicate
    with pytest.raises(NotFoundError):
        documents.link(db, people["eng"], d.id, "ITEM", 9999)
    total, rows = documents.list_documents(db, target_type="DESIGN", target_id=world["die"])
    assert total == 1 and rows[0]["doc_key"] == "DWG-100"
    assert documents.list_documents(db, target_type="ITEM", target_id=it.id)[0] == 0
    documents.unlink(db, people["eng"], d.id, "DESIGN", world["die"])
    assert documents.list_documents(db, target_type="DESIGN", target_id=world["die"])[0] == 0


def test_item_photos_validated_and_stored(db, people, make_item):
    it = make_item()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    p = items.add_photo(db, people["stores"], it.id, "STORAGE", "correct-storage.png", png)
    got, data = items.get_photo(db, p.id)
    assert data == png and got.kind == "STORAGE" and got.mime == "image/png"
    for bad in [("x.exe", b"MZ"), ("x.png", b"not png"), ("x.pdf", PDF)]:
        with pytest.raises(ValidationError):
            items.add_photo(db, people["stores"], it.id, "ITEM", *bad)
    with pytest.raises(PermissionDenied):
        items.add_photo(db, people["prod"], it.id, "ITEM", "a.png", png)
