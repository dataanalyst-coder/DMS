import pytest
from sqlalchemy.orm import sessionmaker

from app.database import init_db, make_engine
from app.services import ledger as svc


@pytest.fixture
def db_empty(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    init_db(engine)
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    # shared die C56 used by T1 and T2; T2 has no flanges at all
    svc.create_item(session, "c56", "DIE", home_location="RACK-A")
    svc.create_item(session, "d60", "DIE", home_location="RACK-A")
    svc.create_item(session, "p12", "PATTERN", home_location="PAT-STORE")
    svc.create_item(session, "p13", "PATTERN", home_location="PAT-STORE")
    svc.create_item(session, "tf-01", "TOP_FLANGE", home_location="RACK-B")
    svc.create_item(session, "bf-01", "BOTTOM_FLANGE", home_location="RACK-B")
    svc.create_assembly(session, "t1", die_id="c56", pattern_id="p12",
                        top_flange_id="tf-01", bottom_flange_id="bf-01")
    svc.create_assembly(session, "t2", die_id="C56", pattern_id="p13")
    svc.create_assembly(session, "t3", die_id="d60", pattern_id="p12")
    yield session
    session.close()
    engine.dispose()


ISSUE = dict(wo_id="WO-100", batch_no="B-1", issued_by="tejas", issued_to="Machine 3")
