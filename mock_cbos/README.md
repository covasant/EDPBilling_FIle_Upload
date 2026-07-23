# Mock CBOS — moved to edpb-core

The canonical v5 mock CBOS server now lives in the shared `edpb-core`
package (`EDP_Billing/packages/edpb-core`, module `edpb_core.mock_cbos`) so
all three repos test against the SAME simulation. The modules here are thin
import shims kept so existing imports (`mock_cbos.app:app`, `from
mock_cbos.state import STATE`) keep working.

Run it:

    uv run uvicorn mock_cbos.app:app --port 8009
    # equivalently: uvicorn edpb_core.mock_cbos.app:app --port 8009

Full docs: `edpb_core/mock_cbos/README.md` in the package.
