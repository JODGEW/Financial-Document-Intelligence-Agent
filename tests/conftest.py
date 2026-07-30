"""Shared pytest configuration for the local authenticated API boundary."""

import hashlib
import os


# The API fails closed without a strong runtime secret.  Tests install a
# deterministic, test-only value before pytest imports any module-level
# ``TestClient(api.app)`` instances.  Runtime code never supplies this value.
TEST_AUTH_SECRET = hashlib.sha256(
    b"fdia deterministic pytest auth secret v1"
).hexdigest()
os.environ["FDIA_AUTH_SECRET"] = TEST_AUTH_SECRET
