from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_whatsapp_test_001"
VALID_TOKEN = "EAAtest1234567890abcdefghijklmnopqrstuvwxyz"
PHONE_NUMBER_ID = "1234567890"
WABA_ID = "9876543210"
