import os
import sys
import tempfile
from pathlib import Path

# Isolated data directory and no example seeding before anything imports earniq.
_TMP = tempfile.mkdtemp(prefix="earniq-test-")
os.environ["EARNIQ_DATA_DIR"] = _TMP
os.environ["EARNIQ_SEED_EXAMPLES"] = "0"
os.environ.pop("EARNIQ_BASIC_AUTH", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from earniq import db  # noqa: E402


@pytest.fixture()
def fresh_db():
    if db.DB.exists():
        db.DB.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db.DB) + suffix)
        if p.exists():
            p.unlink()
    db.init()
    yield db.DB
