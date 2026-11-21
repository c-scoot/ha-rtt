"""Synthetic payloads in the format of the RTT Next Generation API."""

from unittest.mock import AsyncMock, MagicMock

CREDENTIAL_MARKER = "fake-test-credential"
STATION = {
    "namespace": "gb-nr",
    "description": "Clapham Junction",
    "shortCodes": ["CLJ"],
    "longCodes": ["CLPHMJN", "CLPHMJC"],
}
DESTINATION = {
    "namespace": "gb-nr",
    "description": "London Waterloo",
    "shortCodes": ["WAT"],
    "longCodes": ["WATRLMN"],
}
ORIGIN = {
    "namespace": "gb-nr",
    "description": "Woking",
    "shortCodes": ["WOK"],
    "longCodes": ["WOKING"],
}
SYSTEM_STATUS = {"realtimeNetworkRail": "OK", "rttCore": "OK"}
INFO = {
    "version": "2026-03-27",
    "credentials": {"entitlements": [], "namespaceRestriction": False},
}
STOPS = {
    "stops": [
        {
            "namespace": "gb-nr",
            "uniqueIdentity": "gb-nr:CLJ",
            "shortCode": "CLJ",
            "description": "Clapham Junction",
        },
        {
            "namespace": "gb-nr",
            "uniqueIdentity": "gb-nr:WAT",
            "shortCode": "WAT",
            "description": "London Waterloo",
        },
    ],
}
SERVICE_ID = "gb-nr:L01525:2026-11-20"


def response(status, body=None, headers=None):
    """Fake an aiohttp response and its enclosing request context manager."""
    context = MagicMock()
    reply = context.__aenter__.return_value
    reply.status = status
    reply.headers = headers or {}
    reply.json = AsyncMock(return_value=body)
    if isinstance(body, Exception):
        reply.json.side_effect = body
    return context, reply


def location_call(
    *, station=STATION, arrival=None, departure=None, display_as="CALL", platform=None
):
    """Build a service detail location with independent arrival and departure."""
    temporal = {"scheduledCallType": "ADVERTISED_OPEN", "displayAs": display_as}
    if arrival is not None:
        temporal["arrival"] = arrival
    if departure is not None:
        temporal["departure"] = departure
    metadata = {"platform": platform} if platform is not None else {}
    return {"location": station, "temporalData": temporal, "locationMetadata": metadata}


def schedule_metadata(unique_identity=SERVICE_ID):
    """Build the metadata shared by line-up and service details endpoints."""
    namespace, identity, departure_date = unique_identity.split(":")
    return {
        "uniqueIdentity": unique_identity,
        "namespace": namespace,
        "identity": identity,
        "departureDate": departure_date,
        "trainReportingIdentity": "2L40",
        "modeType": "TRAIN",
        "inPassengerService": True,
    }


def service_response(*locations, unique_identity=SERVICE_ID):
    """Build a service details response."""
    return {
        "systemStatus": SYSTEM_STATUS,
        "query": {"uniqueIdentity": unique_identity},
        "service": {
            "scheduleMetadata": schedule_metadata(unique_identity),
            "locations": list(locations),
            "origin": [{"location": ORIGIN}],
            "destination": [{"location": DESTINATION}],
        },
    }


def board_response(*locations, service_ids=None, station=STATION):
    """Build a line-up where geography sits in the top-level query."""
    if service_ids is None:
        service_ids = [SERVICE_ID] * len(locations)
    services = []
    for location, service_id in zip(locations, service_ids, strict=True):
        services.append(
            {
                "temporalData": location["temporalData"],
                "locationMetadata": location["locationMetadata"],
                "scheduleMetadata": schedule_metadata(service_id),
                "origin": [{"location": ORIGIN}],
                "destination": [{"location": DESTINATION}],
            }
        )
    return {
        "systemStatus": SYSTEM_STATUS,
        "query": {"location": station},
        "services": services,
    }
