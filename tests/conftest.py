import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from unittest.mock import Mock
from service.server import MecoService
import meco_pb2


@pytest.fixture
def servicer():
    return MecoService()

@pytest.fixture
def context():
    return Mock()

@pytest.fixture
def tmp_uploads_dir(tmp_path):
    import meco
    meco.UPLOADS_DIR = str(tmp_path / "uploads")
    return meco.UPLOADS_DIR

# All fixtures are current and match the updated codebase. If you rename or remove arguments in your service/client, update these fixtures accordingly.