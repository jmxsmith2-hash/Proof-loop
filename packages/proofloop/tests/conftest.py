import os
import tempfile

# Never touch the real ClearLoop or ProofLoop folders from tests.
os.environ["CLEARLOOP_DATA"] = tempfile.mkdtemp(prefix="clearloop-test-")
os.environ["PROOFLOOP_HOME"] = tempfile.mkdtemp(prefix="proofloop-test-")
