"""XML envelope builders + response parser for Sage Intacct's XML Gateway.

Intacct request shape::

    <?xml version="1.0" encoding="UTF-8"?>
    <request>
      <control>
        <senderid>...</senderid>
        <password>...</password>
        <controlid>{uuid}</controlid>
        <uniqueid>false</uniqueid>
        <dtdversion>3.0</dtdversion>
        <includewhitespace>false</includewhitespace>
      </control>
      <operation>
        <authentication>
          <login>
            <userid>...</userid>
            <companyid>...</companyid>
            <password>...</password>
            <locationid/>           <!-- optional -->
          </login>
          <!-- OR, after install() has cached a session_id: -->
          <!-- <sessionid>...</sessionid> -->
        </authentication>
        <content>
          <function controlid="{uuid}">
            <readByQuery> ... </readByQuery>
          </function>
        </content>
      </operation>
    </request>

All helpers below return XML *strings* with no surrounding whitespace. The
connector layer concatenates them, escapes user input via
``xml.sax.saxutils.escape``, and POSTs the final body to the gateway.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape


def next_controlid() -> str:
    """Generate a fresh controlid for one request / function pair."""
    return uuid.uuid4().hex


def _esc(value: Any) -> str:
    """xml-escape any scalar value (str, int, bool) for inclusion in a node."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return xml_escape(str(value))


# ── Envelope ──────────────────────────────────────────────────────────────

def build_envelope(
    sender_id: str,
    sender_password: str,
    user_id: str,
    user_password: str,
    company_id: str,
    function_xml: str,
    location_id: Optional[str] = None,
    entity_id: Optional[str] = None,
    request_controlid: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Wrap one or more ``<function>`` blocks in a full Intacct envelope.

    ``function_xml`` must already be a sequence of complete
    ``<function …>…</function>`` strings (use :func:`build_function_block` to
    produce each).

    When ``session_id`` is supplied, the ``<login>`` block is replaced with
    a ``<sessionid>`` block — Intacct accepts either form, and the session
    form saves the gateway from re-authenticating the user credentials on
    every call. The connector caches the session via :func:`set_metadata`
    after :func:`build_get_session` succeeds at install time.
    """
    rcid = request_controlid or next_controlid()
    if session_id:
        auth_inner = f"<sessionid>{_esc(session_id)}</sessionid>"
    else:
        location_node = (
            f"<locationid>{_esc(location_id)}</locationid>" if location_id else ""
        )
        entity_node = f"<entityid>{_esc(entity_id)}</entityid>" if entity_id else ""
        auth_inner = (
            "<login>"
            f"<userid>{_esc(user_id)}</userid>"
            f"<companyid>{_esc(company_id)}</companyid>"
            f"<password>{_esc(user_password)}</password>"
            f"{location_node}"
            f"{entity_node}"
            "</login>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<request>"
        "<control>"
        f"<senderid>{_esc(sender_id)}</senderid>"
        f"<password>{_esc(sender_password)}</password>"
        f"<controlid>{_esc(rcid)}</controlid>"
        "<uniqueid>false</uniqueid>"
        "<dtdversion>3.0</dtdversion>"
        "<includewhitespace>false</includewhitespace>"
        "</control>"
        "<operation>"
        "<authentication>"
        f"{auth_inner}"
        "</authentication>"
        "<content>"
        f"{function_xml}"
        "</content>"
        "</operation>"
        "</request>"
    )


def build_function_block(controlid: str, inner_xml: str) -> str:
    """Wrap an operation in a ``<function controlid="...">…</function>`` node."""
    return f'<function controlid="{_esc(controlid)}">{inner_xml}</function>'


# ── Session ───────────────────────────────────────────────────────────────

def build_get_session() -> str:
    """Build a ``<getAPISession/>`` inner block — mints a session id for re-use."""
    return "<getAPISession/>"


# ── Standard read operations ──────────────────────────────────────────────

def build_read_by_query(
    object_name: str,
    fields: str = "*",
    query: Optional[str] = None,
    pagesize: int = 100,
    return_format: str = "json",
) -> str:
    """Build a ``<readByQuery>`` inner block for the given object."""
    query_node = f"<query>{_esc(query)}</query>" if query else "<query></query>"
    return (
        "<readByQuery>"
        f"<object>{_esc(object_name)}</object>"
        f"<fields>{_esc(fields)}</fields>"
        f"{query_node}"
        f"<pagesize>{int(pagesize)}</pagesize>"
        f"<returnFormat>{_esc(return_format)}</returnFormat>"
        "</readByQuery>"
    )


def build_read(object_name: str, keys: List[str], fields: str = "*") -> str:
    """Build a ``<read>`` inner block — fetch one or more records by primary key."""
    keys_csv = ",".join(_esc(k) for k in keys)
    return (
        "<read>"
        f"<object>{_esc(object_name)}</object>"
        f"<keys>{keys_csv}</keys>"
        f"<fields>{_esc(fields)}</fields>"
        "<returnFormat>json</returnFormat>"
        "</read>"
    )


def build_read_more(result_id: str) -> str:
    """Build a ``<readMore>`` inner block — fetch next page of a prior readByQuery."""
    return f"<readMore><resultId>{_esc(result_id)}</resultId></readMore>"


# ── Object create / update operations ─────────────────────────────────────

def build_create_customer(
    customer_id: str,
    name: str,
    status: str = "active",
    contact_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a ``<create_customer>`` inner block."""
    contact_xml = _render_contact(contact_info) if contact_info else ""
    return (
        "<create_customer>"
        f"<customerid>{_esc(customer_id)}</customerid>"
        f"<name>{_esc(name)}</name>"
        f"<status>{_esc(status)}</status>"
        "<displaycontact>"
        f"{contact_xml}"
        "</displaycontact>"
        "</create_customer>"
    )


def build_update_customer(
    customer_id: str,
    fields: Dict[str, Any],
) -> str:
    """Build an ``<update_customer>`` inner block.

    ``fields`` is a flat dict of intacct customer keys to overwrite — at
    minimum ``customerid`` will be set from the positional argument.
    """
    allowed_keys = (
        "name",
        "status",
        "termname",
        "currency",
        "comments",
        "entity",
    )
    inner = f"<customerid>{_esc(customer_id)}</customerid>"
    for k in allowed_keys:
        if fields.get(k) is not None:
            inner += f"<{k}>{_esc(fields[k])}</{k}>"
    return f"<update_customer>{inner}</update_customer>"


def build_create_vendor(
    vendor_id: str,
    name: str,
    status: str = "active",
    contact_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a ``<create_vendor>`` inner block."""
    contact_xml = _render_contact(contact_info) if contact_info else ""
    return (
        "<create_vendor>"
        f"<vendorid>{_esc(vendor_id)}</vendorid>"
        f"<name>{_esc(name)}</name>"
        f"<status>{_esc(status)}</status>"
        "<displaycontact>"
        f"{contact_xml}"
        "</displaycontact>"
        "</create_vendor>"
    )


def build_create_invoice(
    customer_id: str,
    invoice_no: str,
    invoice_date: str,
    due_date: str,
    line_items: List[Dict[str, Any]],
) -> str:
    """Build a ``<create_invoice>`` inner block.

    ``line_items`` is a list of dicts: each dict supports keys
    ``glaccountno``, ``amount``, ``memo``, ``departmentid``, ``locationid``.
    """
    line_xml = "".join(_render_invoice_line(li) for li in (line_items or []))
    return (
        "<create_invoice>"
        f"<customerid>{_esc(customer_id)}</customerid>"
        f"<datecreated>{_render_date_node(invoice_date)}</datecreated>"
        f"<dateposted>{_render_date_node(invoice_date)}</dateposted>"
        f"<datedue>{_render_date_node(due_date)}</datedue>"
        f"<invoiceno>{_esc(invoice_no)}</invoiceno>"
        "<invoiceitems>"
        f"{line_xml}"
        "</invoiceitems>"
        "</create_invoice>"
    )


def build_run_smart_event(
    event_name: str, params: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a ``<run_smart_event>`` inner block."""
    params_xml = ""
    if params:
        param_items = "".join(
            f"<parameter><name>{_esc(k)}</name><value>{_esc(v)}</value></parameter>"
            for k, v in params.items()
        )
        params_xml = f"<parameters>{param_items}</parameters>"
    return (
        "<run_smart_event>"
        f"<name>{_esc(event_name)}</name>"
        f"{params_xml}"
        "</run_smart_event>"
    )


# ── Render helpers ────────────────────────────────────────────────────────

def _render_contact(contact: Dict[str, Any]) -> str:
    """Render a ``<contact>`` sub-tree from a flat dict."""
    fields_order = (
        "contactname", "firstname", "lastname", "companyname",
        "phone1", "phone2", "email1", "email2",
    )
    inner = "".join(
        f"<{k}>{_esc(contact[k])}</{k}>" for k in fields_order if contact.get(k)
    )
    mailing = contact.get("mailing_address") or {}
    if isinstance(mailing, dict) and mailing:
        addr_fields = ("address1", "address2", "city", "state", "zip", "country")
        mailing_xml = "".join(
            f"<{k}>{_esc(mailing[k])}</{k}>" for k in addr_fields if mailing.get(k)
        )
        inner += f"<mailaddress>{mailing_xml}</mailaddress>"
    return inner


def _render_invoice_line(line: Dict[str, Any]) -> str:
    fields_order = ("glaccountno", "amount", "memo", "departmentid", "locationid")
    inner = "".join(
        f"<{k}>{_esc(line[k])}</{k}>" for k in fields_order if line.get(k) is not None
    )
    return f"<lineitem>{inner}</lineitem>"


def _render_date_node(date_str: str) -> str:
    """Intacct expects ``<year/><month/><day/>`` nested children for date fields.

    Accepts ISO date strings (``YYYY-MM-DD``). For an unparseable value the
    raw string is dropped into a generic ``<date>`` wrapper as a fallback.
    """
    if not date_str:
        return ""
    parts = date_str.strip().split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        y, m, d = parts
        return f"<year>{int(y)}</year><month>{int(m)}</month><day>{int(d)}</day>"
    return f"<date>{_esc(date_str)}</date>"


# ── Response parser ───────────────────────────────────────────────────────

def parse_envelope(xml_text: str) -> Dict[str, Any]:
    """Parse an Intacct response envelope into a normalized dict.

    Returns::

        {
          "control_status": "success" | "failure",
          "control_error":  {…} | None,
          "session_id":     str | None,   # from getAPISession result, if any
          "endpoint":       str | None,   # from getAPISession result, if any
          "functions": [
            {
              "controlid":     str,
              "status":        "success" | "failure",
              "function_name": str,
              "data":          [ {…}, … ],
              "result_id":     Optional[str],
              "num_remaining": int,
              "total_count":   int,
              "error":         Optional[{errorno, description, correction}],
            },
            …
          ],
        }
    """
    root = ET.fromstring(xml_text)

    # ── control ──
    control = root.find("control")
    control_status_el = control.find("status") if control is not None else None
    control_status = (control_status_el.text or "").strip() if control_status_el is not None else "success"
    control_error = _extract_error(control) if control is not None else None

    # ── operation ──
    functions: List[Dict[str, Any]] = []
    session_id: Optional[str] = None
    endpoint: Optional[str] = None
    operation = root.find("operation")
    if operation is not None:
        # Operation-level auth failure (the authentication block has its own status)
        auth = operation.find("authentication")
        auth_status_el = auth.find("status") if auth is not None else None
        op_auth_status = (
            (auth_status_el.text or "").strip() if auth_status_el is not None else "success"
        )

        result_nodes = operation.findall(".//result")
        for result_el in result_nodes:
            fn = _parse_result_node(result_el)
            functions.append(fn)
            # getAPISession result carries the new sessionid + endpoint
            if fn.get("function_name") == "getAPISession":
                api_session = result_el.find(".//api/sessionid")
                api_endpoint = result_el.find(".//api/endpoint")
                if api_session is not None and api_session.text:
                    session_id = api_session.text.strip()
                if api_endpoint is not None and api_endpoint.text:
                    endpoint = api_endpoint.text.strip()

        # If auth failed and no result blocks were emitted, surface as a synthetic function
        if op_auth_status == "failure" and not functions:
            functions.append({
                "controlid": "",
                "status": "failure",
                "function_name": "",
                "data": [],
                "result_id": None,
                "num_remaining": 0,
                "total_count": 0,
                "error": _extract_error(auth),
            })

    return {
        "control_status": control_status,
        "control_error": control_error,
        "session_id": session_id,
        "endpoint": endpoint,
        "functions": functions,
    }


def _parse_result_node(result_el: ET.Element) -> Dict[str, Any]:
    status_el = result_el.find("status")
    status = (status_el.text or "").strip() if status_el is not None else "success"

    fn_name_el = result_el.find("function")
    function_name = (fn_name_el.text or "").strip() if fn_name_el is not None else ""

    controlid_el = result_el.find("controlid")
    controlid = (controlid_el.text or "").strip() if controlid_el is not None else ""

    data_el = result_el.find("data")
    rows: List[Dict[str, Any]] = []
    result_id: Optional[str] = None
    num_remaining = 0
    total_count = 0
    if data_el is not None:
        result_id = data_el.attrib.get("resultId") or None
        try:
            num_remaining = int(data_el.attrib.get("numremaining") or 0)
        except ValueError:
            num_remaining = 0
        try:
            total_count = int(data_el.attrib.get("totalcount") or 0)
        except ValueError:
            total_count = 0
        for child in data_el:
            rows.append(_element_to_dict(child))

    # Some create_* functions return a <key> element instead of <data>
    if not rows:
        key_el = result_el.find("key")
        if key_el is not None and key_el.text:
            rows.append({"key": key_el.text.strip()})

    return {
        "controlid": controlid,
        "status": status,
        "function_name": function_name,
        "data": rows,
        "result_id": result_id,
        "num_remaining": num_remaining,
        "total_count": total_count,
        "error": _extract_error(result_el) if status == "failure" else None,
    }


def _extract_error(parent: ET.Element) -> Optional[Dict[str, str]]:
    """Extract the first ``<errormessage><error>`` block, if present."""
    error_el = parent.find(".//errormessage/error")
    if error_el is None:
        error_el = parent.find(".//error")
    if error_el is None:
        return None
    out: Dict[str, str] = {}
    for tag in ("errorno", "description", "description2", "correction"):
        node = error_el.find(tag)
        if node is not None and node.text:
            out[tag] = node.text.strip()
    return out or None


def _element_to_dict(el: ET.Element) -> Dict[str, Any]:
    """Convert one Intacct record element (e.g. ``<customer>…</customer>``) to a flat dict."""
    out: Dict[str, Any] = {}
    for child in el:
        if len(child) == 0:
            out[child.tag] = (child.text or "").strip()
        else:
            out[child.tag] = _element_to_dict(child)
    return out
