"""Unit-test fixtures for SageIntacctConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve before the connector is
# imported.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import SageIntacctConnector  # noqa: E402

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"
GATEWAY_URL = "https://api.intacct.com/ia/xml/xmlgw.phtml"

TEST_CONFIG = {
    "sender_id": "test-sender",
    "sender_password": "test-sender-pw",
    "user_id": "test-user",
    "user_password": "test-user-pw",
    "company_id": "TestCo",
    "location_id": "",
    "entity_id": "",
    "base_url": GATEWAY_URL,
    "rate_limit_per_min": 30,
}


# ── Sample envelopes ─────────────────────────────────────────────────────

SAMPLE_READ_BY_QUERY_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <senderid>test-sender</senderid>
    <controlid>ctrl-1</controlid>
    <uniqueid>false</uniqueid>
    <dtdversion>3.0</dtdversion>
  </control>
  <operation>
    <authentication>
      <status>success</status>
      <userid>test-user</userid>
      <companyid>TestCo</companyid>
      <sessiontimestamp>2026-06-21T12:00:00-08:00</sessiontimestamp>
    </authentication>
    <result>
      <status>success</status>
      <function>readByQuery</function>
      <controlid>fn-1</controlid>
      <data listtype="customer" count="1" totalcount="1" numremaining="0" resultId="result-id-abc">
        <customer>
          <CUSTOMERID>CUST-001</CUSTOMERID>
          <NAME>Acme Corp</NAME>
          <STATUS>active</STATUS>
          <WHENCREATED>2026-01-01T00:00:00</WHENCREATED>
        </customer>
      </data>
    </result>
  </operation>
</response>"""

SAMPLE_AUTH_FAILURE = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>failure</status>
      <errormessage>
        <error>
          <errorno>XL03000006</errorno>
          <description>Sign-in information is incorrect.</description>
          <correction>Check your sign-in credentials and try again.</correction>
        </error>
      </errormessage>
    </authentication>
  </operation>
</response>"""

SAMPLE_VALIDATION_FAILURE = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>failure</status>
      <function>readByQuery</function>
      <controlid>fn-1</controlid>
      <errormessage>
        <error>
          <errorno>BL01001973</errorno>
          <description>Object definition CUSTOMERX not found.</description>
          <correction>Check the object name and try again.</correction>
        </error>
      </errormessage>
    </result>
  </operation>
</response>"""

SAMPLE_CREATE_CUSTOMER_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>success</status>
      <function>create_customer</function>
      <controlid>fn-1</controlid>
      <key>CUST-NEW-1</key>
    </result>
  </operation>
</response>"""

SAMPLE_EMPTY_GLACCOUNT_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>success</status>
      <function>readByQuery</function>
      <controlid>fn-1</controlid>
      <data listtype="glaccount" count="1" totalcount="1" numremaining="0">
        <glaccount>
          <ACCOUNTNO>1000</ACCOUNTNO>
          <TITLE>Cash</TITLE>
        </glaccount>
      </data>
    </result>
  </operation>
</response>"""

SAMPLE_GET_SESSION_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>success</status>
      <function>getAPISession</function>
      <controlid>fn-1</controlid>
      <data>
        <api>
          <sessionid>SESS-ABC-123</sessionid>
          <endpoint>https://api.intacct.com/ia/xml/xmlgw.phtml</endpoint>
          <locationid/>
        </api>
      </data>
    </result>
  </operation>
</response>"""

SAMPLE_READ_BY_KEY_VENDOR = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>success</status>
      <function>read</function>
      <controlid>fn-1</controlid>
      <data listtype="vendor" count="1" totalcount="1" numremaining="0">
        <vendor>
          <VENDORID>VEND-1</VENDORID>
          <NAME>Sample Vendor</NAME>
          <STATUS>active</STATUS>
        </vendor>
      </data>
    </result>
  </operation>
</response>"""

SAMPLE_READ_MORE_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>success</status>
      <function>readMore</function>
      <controlid>fn-1</controlid>
      <data listtype="customer" count="1" totalcount="2" numremaining="0" resultId="result-id-abc">
        <customer>
          <CUSTOMERID>CUST-002</CUSTOMERID>
          <NAME>Beta Co</NAME>
          <STATUS>active</STATUS>
        </customer>
      </data>
    </result>
  </operation>
</response>"""

SAMPLE_PAGE_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>success</status>
      <function>readByQuery</function>
      <controlid>fn-1</controlid>
      <data listtype="customer" count="1" totalcount="2" numremaining="1" resultId="result-id-abc">
        <customer>
          <CUSTOMERID>CUST-001</CUSTOMERID>
          <NAME>Acme Corp</NAME>
        </customer>
      </data>
    </result>
  </operation>
</response>"""

SAMPLE_SMART_EVENT_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<response>
  <control>
    <status>success</status>
    <controlid>ctrl-1</controlid>
  </control>
  <operation>
    <authentication>
      <status>success</status>
    </authentication>
    <result>
      <status>success</status>
      <function>run_smart_event</function>
      <controlid>fn-1</controlid>
      <data>
        <key>EVENT-OK</key>
      </data>
    </result>
  </operation>
</response>"""


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis / DB side-effects."""
    mocker.patch.object(SageIntacctConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(SageIntacctConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(SageIntacctConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(SageIntacctConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(SageIntacctConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        SageIntacctConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(SageIntacctConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return SageIntacctConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out the HTTP-client backoff sleep."""
    from client import http_client as hc

    async def _zero_sleep(_attempt):
        return None
    monkeypatch.setattr(
        hc.SageIntacctHTTPClient, "_sleep_backoff", staticmethod(_zero_sleep),
    )
    return _zero_sleep
