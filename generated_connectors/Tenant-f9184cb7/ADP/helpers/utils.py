"""Pure builders for ADP Events API envelopes."""
from typing import Any, Dict, Optional


def build_time_off_event(
    worker_aoid: str,
    policy_code: str,
    start_date: str,
    end_date: str,
    hours: Optional[float] = None,
    comments: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the JSON body for POST /time-off/v2/workers/{aoid}/time-off-requests."""
    body: Dict[str, Any] = {
        "events": [
            {
                "data": {
                    "eventContext": {
                        "worker": {"associateOID": worker_aoid},
                    },
                    "transform": {
                        "timeOffRequest": {
                            "timeOffPolicyCode": {"codeValue": policy_code},
                            "startDate": start_date,
                            "endDate": end_date,
                        }
                    },
                }
            }
        ]
    }
    tor = body["events"][0]["data"]["transform"]["timeOffRequest"]
    if hours is not None:
        tor["totalTimeOffHours"] = hours
    if comments:
        tor["comments"] = [{"textValue": comments}]
    return body


def build_email_change_event(
    worker_aoid: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the body for POST /events/hr/v1/worker.business-communication.email.change.

    At least one of `email` / `phone` must be supplied. Both can be supplied in
    a single envelope.
    """
    if not email and not phone:
        raise ValueError("build_email_change_event requires at least one of email/phone")

    transform: Dict[str, Any] = {}
    if email:
        transform["businessCommunication"] = {
            "email": {"emailUri": email},
        }
    if phone:
        transform.setdefault("businessCommunication", {})
        # ADP phone is split into country/area/number — keep simple shape that
        # downstream consumers can refine. We store the raw E.164 string.
        transform["businessCommunication"]["landline"] = {
            "formattedNumber": phone,
        }

    return {
        "events": [
            {
                "data": {
                    "eventContext": {
                        "worker": {"associateOID": worker_aoid},
                    },
                    "transform": transform,
                }
            }
        ]
    }
