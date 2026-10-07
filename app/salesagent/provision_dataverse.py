"""Create (or complete) the Dataverse table used by the AI sales agent.

    docker compose run --rm tool-api python -m salesagent.provision_dataverse

Idempotent: existing table and columns are left untouched; only missing columns
are added. Prints the DATAVERSE_PREFIX and DATAVERSE_TABLE values to put in .env.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx
from azure.identity import ClientSecretCredential

from .config import get_settings

API = "/api/data/v9.2"
TABLE_SUFFIX = "aicallqualification"


def label(text: str) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [
            {"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": text, "LanguageCode": 1033}
        ],
    }


REQ_NONE = {"Value": "None", "CanBeChanged": True, "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings"}


def string_attr(schema: str, display: str, max_len: int, fmt: str = "Text", primary: bool = False) -> dict[str, Any]:
    a = {
        "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "AttributeType": "String",
        "AttributeTypeName": {"Value": "StringType"},
        "SchemaName": schema,
        "RequiredLevel": REQ_NONE,
        "MaxLength": max_len,
        "FormatName": {"Value": fmt},
        "DisplayName": label(display),
    }
    if primary:
        a["IsPrimaryName"] = True
    return a


def int_attr(schema: str, display: str) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
        "AttributeType": "Integer",
        "AttributeTypeName": {"Value": "IntegerType"},
        "SchemaName": schema,
        "RequiredLevel": REQ_NONE,
        "Format": "None",
        "MinValue": 0,
        "MaxValue": 2147483647,
        "DisplayName": label(display),
    }


def decimal_attr(schema: str, display: str) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.DecimalAttributeMetadata",
        "AttributeType": "Decimal",
        "AttributeTypeName": {"Value": "DecimalType"},
        "SchemaName": schema,
        "RequiredLevel": REQ_NONE,
        "Precision": 2,
        "MinValue": 0,
        "MaxValue": 100000000000,
        "DisplayName": label(display),
    }


def memo_attr(schema: str, display: str) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
        "AttributeType": "Memo",
        "AttributeTypeName": {"Value": "MemoType"},
        "SchemaName": schema,
        "RequiredLevel": REQ_NONE,
        "Format": "TextArea",
        "MaxLength": 4000,
        "DisplayName": label(display),
    }


def datetime_attr(schema: str, display: str) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
        "AttributeType": "DateTime",
        "AttributeTypeName": {"Value": "DateTimeType"},
        "SchemaName": schema,
        "RequiredLevel": REQ_NONE,
        "Format": "DateAndTime",
        "DateTimeBehavior": {"Value": "UserLocal"},
        "DisplayName": label(display),
    }


def columns(p: str) -> list[dict[str, Any]]:
    return [
        string_attr(f"{p}_company", "Company", 200),
        string_attr(f"{p}_contactname", "Contact Name", 200),
        string_attr(f"{p}_phone", "Phone", 32, "Phone"),
        int_attr(f"{p}_score", "Lead Score"),
        string_attr(f"{p}_band", "Score Band", 32),
        int_attr(f"{p}_mobilelines", "Mobile Lines"),
        string_attr(f"{p}_currentcarrier", "Current Carrier", 120),
        int_attr(f"{p}_renewalmonths", "Contract Renewal (months)"),
        string_attr(f"{p}_decisionrole", "Decision Role", 40),
        decimal_attr(f"{p}_opportunityvalue", "Estimated Opportunity (USD)"),
        string_attr(f"{p}_reasoncodes", "Score Reason Codes", 400),
        memo_attr(f"{p}_summary", "Call Summary"),
        string_attr(f"{p}_nextaction", "Recommended Next Action", 400),
        string_attr(f"{p}_outcome", "Call Outcome", 64),
        datetime_attr(f"{p}_meetingstart", "Meeting Start"),
        string_attr(f"{p}_meetingurl", "Teams Meeting Link", 1000, "Url"),
        string_attr(f"{p}_repupn", "Assigned Rep", 200),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", help="publisher prefix (default: the Default solution's publisher)")
    args = ap.parse_args(argv)

    s = get_settings()
    if not s.dataverse_configured:
        print("DATAVERSE_URL and the Entra service principal must be configured first.", file=sys.stderr)
        return 2
    base = s.dataverse_url.rstrip("/")
    cred = ClientSecretCredential(s.azure_tenant_id, s.azure_client_id, s.azure_client_secret.get_secret_value())
    token = cred.get_token(base + "/.default").token
    headers = {
        "Authorization": f"Bearer {token}",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }

    with httpx.Client(base_url=base + API, headers=headers, timeout=60) as c:
        prefix = args.prefix
        if not prefix:
            r = c.get(
                "solutions",
                params={
                    "$filter": "uniquename eq 'Default'",
                    "$select": "uniquename",
                    "$expand": "publisherid($select=customizationprefix)",
                },
            )
            r.raise_for_status()
            prefix = r.json()["value"][0]["publisherid"]["customizationprefix"]
        logical = f"{prefix}_{TABLE_SUFFIX}"
        print(f"Using publisher prefix '{prefix}', table '{logical}'")

        r = c.get(f"EntityDefinitions(LogicalName='{logical}')", params={"$select": "LogicalName"})
        if r.status_code == 404:
            body = {
                "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
                "SchemaName": logical,
                "DisplayName": label("AI Call Qualification"),
                "DisplayCollectionName": label("AI Call Qualifications"),
                "Description": label("Leads qualified by the AI sales voice agent"),
                "OwnershipType": "UserOwned",
                "IsActivity": False,
                "HasActivities": False,
                "HasNotes": False,
                "Attributes": [string_attr(f"{prefix}_name", "Name", 100, primary=True)],
            }
            cr = c.post("EntityDefinitions", json=body)
            if cr.status_code >= 400:
                print(f"Create table failed: {cr.status_code} {cr.text[:500]}", file=sys.stderr)
                return 1
            print("Created table")
        elif r.status_code >= 400:
            print(f"Lookup failed: {r.status_code} {r.text[:500]}", file=sys.stderr)
            return 1
        else:
            print("Table already exists")

        r = c.get(f"EntityDefinitions(LogicalName='{logical}')/Attributes", params={"$select": "LogicalName"})
        r.raise_for_status()
        existing = {a["LogicalName"] for a in r.json()["value"]}
        for col in columns(prefix):
            if col["SchemaName"].lower() in existing:
                continue
            cr = c.post(f"EntityDefinitions(LogicalName='{logical}')/Attributes", json=col)
            if cr.status_code >= 400:
                print(f"Add column {col['SchemaName']} failed: {cr.status_code} {cr.text[:400]}", file=sys.stderr)
                return 1
            print(f"Added column {col['SchemaName']}")

        r = c.get(f"EntityDefinitions(LogicalName='{logical}')", params={"$select": "EntitySetName"})
        r.raise_for_status()
        entity_set = r.json()["EntitySetName"]

    print("\nAdd to .env:")
    print(f"DATAVERSE_PREFIX={prefix}")
    print(f"DATAVERSE_TABLE={entity_set}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
