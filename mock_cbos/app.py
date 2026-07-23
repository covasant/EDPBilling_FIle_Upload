"""Import shim — THE mock CBOS server now lives in edpb-core (wayfinder
ticket 06), shared by all three repos so everyone tests against the same v5
simulation. This module keeps `mock_cbos.app:app` / existing imports working.

Run:  uvicorn mock_cbos.app:app --port 8009   (or edpb_core.mock_cbos.app:app)
"""

from edpb_core.mock_cbos.app import *  # noqa: F401,F403
from edpb_core.mock_cbos.app import app  # noqa: F401
