import os
import tempfile

# Never touch the real ClearLoop folder from tests.
os.environ.setdefault("CLEARLOOP_DATA", tempfile.mkdtemp(prefix="clearloop-test-"))
