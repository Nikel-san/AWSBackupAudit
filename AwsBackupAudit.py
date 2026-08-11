#!/usr/bin/env python3

import argparse
import csv
import fnmatch
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError


class Colors:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    RESET = "\033[0m"


@dataclass
class ResourceRecord:
    account_id: str
    object_type: str
    object_name: str
    arn: str
    tags: Dict[str, str]


@dataclass
class BackupSelectionRule:
    plan_name: str
    resources: List[str]
    not_resources: List[str]
    list_of_tags: List[Dict[str, str]]
    conditions: Dict[str, List[Dict[str, str]]]


def print_colored(color: str, message: str) -> None:
    print(f"{color}{message}{Colors.RESET}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit AWS Backup coverage for prod EC2 and RDS resources")
    parser.add_argument("-a", "--account-id", help="AWS account ID to audit")
    parser.add_argument("-r", "--region", default="us-east-1", help="AWS region (default: us-east-1)")
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV filename (default: backup_audit_<account>_<timestamp>.csv)",
    )
    parser.add_argument("-k", "--aws-access-key-id", help="AWS access key ID")
    parser.add_argument("-s", "--aws-secret-access-key", help="AWS secret access key")
    parser.add_argument("-t", "--aws-session-token", help="AWS session token (optional)")

    args = parser.parse_args()

    if not args.account_id:
        print_colored(Colors.RED, "Error: --account-id/-a is required")
        sys.exit(1)

    if args.aws_access_key_id and not args.aws_secret_access_key:
        print_colored(Colors.RED, "Error: --aws-secret-access-key is required when --aws-access-key-id is provided")
        sys.exit(1)

    if args.aws_secret_access_key and not args.aws_access_key_id:
        print_colored(Colors.RED, "Error: --aws-access-key-id is required when --aws-secret-access-key is provided")
        sys.exit(1)

    if not args.output:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        args.output = f"backup_audit_{args.account_id}_{timestamp}.csv"

    return args


def build_session(args: argparse.Namespace) -> boto3.Session:
    access_key = args.aws_access_key_id or os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = args.aws_secret_access_key or os.getenv("AWS_SECRET_ACCESS_KEY")
    session_token = args.aws_session_token or os.getenv("AWS_SESSION_TOKEN")

    if access_key and not secret_key:
        print_colored(Colors.RED, "Error: AWS secret access key is missing")
        sys.exit(1)

    if secret_key and not access_key:
        print_colored(Colors.RED, "Error: AWS access key ID is missing")
        sys.exit(1)

    if access_key and secret_key:
        return boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            region_name=args.region,
        )

    return boto3.Session(region_name=args.region)


def validate_sts(session: boto3.Session, requested_account_id: str) -> str:
    try:
        identity = session.client("sts").get_caller_identity()
    except (NoCredentialsError, PartialCredentialsError):
        print_colored(
            Colors.RED,
            "Error: AWS credentials were not found. Provide CLI credentials or set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.",
        )
        sys.exit(1)
    except (ClientError, BotoCoreError) as exc:
        print_colored(Colors.RED, f"Error: Failed to validate AWS credentials via STS: {exc}")
        sys.exit(1)

    caller_account = identity.get("Account", "")
    print_colored(Colors.GREEN, f"Authenticated AWS account: {caller_account}")

    if caller_account != requested_account_id:
        print_colored(
            Colors.YELLOW,
            f"Warning: Caller account ({caller_account}) differs from --account-id ({requested_account_id})",
        )

    return caller_account


def ec2_arn(region: str, account_id: str, instance_id: str) -> str:
    return f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}"


def list_prod_ec2_instances(ec2_client, account_id: str, region: str) -> List[ResourceRecord]:
    resources: List[ResourceRecord] = []
    paginator = ec2_client.get_paginator("describe_instances")
    pages = paginator.paginate(Filters=[{"Name": "tag:env", "Values": ["prod"]}])

    for page in pages:
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance.get("InstanceId", "")
                tags_list = instance.get("Tags", [])
                tags = {t.get("Key", ""): t.get("Value", "") for t in tags_list if t.get("Key")}
                object_name = tags.get("Name") or instance_id
                resources.append(
                    ResourceRecord(
                        account_id=account_id,
                        object_type="EC2 Instance",
                        object_name=object_name,
                        arn=ec2_arn(region, account_id, instance_id),
                        tags=tags,
                    )
                )

    return resources


def list_prod_rds_instances(rds_client, account_id: str) -> List[ResourceRecord]:
    resources: List[ResourceRecord] = []
    marker: Optional[str] = None

    while True:
        params = {}
        if marker:
            params["Marker"] = marker

        response = rds_client.describe_db_instances(**params)
        for db in response.get("DBInstances", []):
            db_arn = db.get("DBInstanceArn", "")
            db_identifier = db.get("DBInstanceIdentifier", "")

            try:
                tag_response = rds_client.list_tags_for_resource(ResourceName=db_arn)
            except (ClientError, BotoCoreError) as exc:
                print_colored(Colors.YELLOW, f"Warning: Failed to read tags for RDS instance {db_identifier}: {exc}")
                continue

            tags = {t.get("Key", ""): t.get("Value", "") for t in tag_response.get("TagList", []) if t.get("Key")}
            if tags.get("env") != "prod":
                continue

            resources.append(
                ResourceRecord(
                    account_id=account_id,
                    object_type="RDS Database",
                    object_name=db_identifier,
                    arn=db_arn,
                    tags=tags,
                )
            )

        marker = response.get("Marker")
        if not marker:
            break

    return resources


def get_tag_key(condition_key: str) -> str:
    if condition_key.startswith("aws:ResourceTag/"):
        return condition_key.split("/", 1)[1]
    if condition_key.lower().startswith("aws:resourcetag:"):
        return condition_key.split(":")[-1]
    if condition_key.startswith("tag:"):
        return condition_key.split(":", 1)[1]
    return condition_key


def is_pattern_match(value: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(value, pattern)


def tag_conditions_match(tags: Dict[str, str], selection: BackupSelectionRule) -> bool:
    for tag_condition in selection.list_of_tags:
        condition_type = (tag_condition.get("ConditionType") or "").upper()
        condition_key = get_tag_key(tag_condition.get("ConditionKey", ""))
        expected = tag_condition.get("ConditionValue", "")
        actual = tags.get(condition_key)

        if condition_type == "STRINGEQUALS":
            if actual != expected:
                return False
        else:
            return False

    for entry in selection.conditions.get("StringEquals", []):
        key = get_tag_key(entry.get("ConditionKey", ""))
        expected = entry.get("ConditionValue", "")
        if tags.get(key) != expected:
            return False

    for entry in selection.conditions.get("StringLike", []):
        key = get_tag_key(entry.get("ConditionKey", ""))
        pattern = entry.get("ConditionValue", "")
        actual = tags.get(key, "")
        if not is_pattern_match(actual, pattern):
            return False

    for entry in selection.conditions.get("StringNotEquals", []):
        key = get_tag_key(entry.get("ConditionKey", ""))
        expected = entry.get("ConditionValue", "")
        if tags.get(key) == expected:
            return False

    for entry in selection.conditions.get("StringNotLike", []):
        key = get_tag_key(entry.get("ConditionKey", ""))
        pattern = entry.get("ConditionValue", "")
        actual = tags.get(key, "")
        if is_pattern_match(actual, pattern):
            return False

    return True


def any_arn_match(arn: str, patterns: List[str]) -> bool:
    for pattern in patterns:
        if is_pattern_match(arn, pattern):
            return True
    return False


def resource_covered_by_selection(resource: ResourceRecord, selection: BackupSelectionRule) -> bool:
    arn = resource.arn

    if selection.not_resources and any_arn_match(arn, selection.not_resources):
        return False

    explicit_match = any_arn_match(arn, selection.resources) if selection.resources else False

    has_tag_rules = bool(selection.list_of_tags) or any(selection.conditions.get(k) for k in selection.conditions.keys())
    tag_match = False

    if has_tag_rules:
        in_scope = True
        if selection.resources:
            in_scope = explicit_match
        if in_scope and tag_conditions_match(resource.tags, selection):
            tag_match = True

    return explicit_match or tag_match


def get_backup_selection_rules(backup_client) -> List[BackupSelectionRule]:
    rules: List[BackupSelectionRule] = []
    next_token: Optional[str] = None

    while True:
        params = {"IncludeDeleted": False, "MaxResults": 1000}
        if next_token:
            params["NextToken"] = next_token

        plans_response = backup_client.list_backup_plans(**params)
        for plan in plans_response.get("BackupPlansList", []):
            plan_id = plan.get("BackupPlanId")
            plan_name = plan.get("BackupPlanName", "")
            if not plan_id:
                continue

            selection_token: Optional[str] = None
            while True:
                selection_params = {"BackupPlanId": plan_id, "MaxResults": 1000}
                if selection_token:
                    selection_params["NextToken"] = selection_token

                selections_response = backup_client.list_backup_selections(**selection_params)
                for selection in selections_response.get("BackupSelectionsList", []):
                    selection_id = selection.get("SelectionId")
                    if not selection_id:
                        continue

                    details = backup_client.get_backup_selection(
                        BackupPlanId=plan_id,
                        SelectionId=selection_id,
                    )
                    backup_selection = details.get("BackupSelection", {})
                    rules.append(
                        BackupSelectionRule(
                            plan_name=plan_name,
                            resources=backup_selection.get("Resources", []) or [],
                            not_resources=backup_selection.get("NotResources", []) or [],
                            list_of_tags=backup_selection.get("ListOfTags", []) or [],
                            conditions=backup_selection.get("Conditions", {}) or {},
                        )
                    )

                selection_token = selections_response.get("NextToken")
                if not selection_token:
                    break

        next_token = plans_response.get("NextToken")
        if not next_token:
            break

    return rules


def find_coverage(resource: ResourceRecord, selection_rules: List[BackupSelectionRule]) -> List[str]:
    covered_by: List[str] = []
    for rule in selection_rules:
        if resource_covered_by_selection(resource, rule):
            covered_by.append(rule.plan_name)
    # Deduplicate while preserving order.
    return list(dict.fromkeys(covered_by))


def write_csv(output_file: str, rows: List[Dict[str, str]]) -> None:
    fieldnames = ["Account ID", "Object Type", "Object Name", "Backup Plan Name"]
    with open(output_file, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    session = build_session(args)
    validate_sts(session, args.account_id)

    ec2_client = session.client("ec2", region_name=args.region)
    rds_client = session.client("rds", region_name=args.region)
    backup_client = session.client("backup", region_name=args.region)

    print_colored(Colors.CYAN, f"Scanning EC2 and RDS resources tagged env=prod in {args.region}...")
    ec2_resources = list_prod_ec2_instances(ec2_client, args.account_id, args.region)
    rds_resources = list_prod_rds_instances(rds_client, args.account_id)
    all_resources = ec2_resources + rds_resources
    print_colored(Colors.CYAN, f"Discovered {len(ec2_resources)} EC2 and {len(rds_resources)} RDS resources")

    print_colored(Colors.CYAN, "Loading AWS Backup plan selections...")
    selection_rules = get_backup_selection_rules(backup_client)

    rows: List[Dict[str, str]] = []
    covered_count = 0

    for resource in all_resources:
        plan_names = find_coverage(resource, selection_rules)
        if plan_names:
            covered_count += 1

        rows.append(
            {
                "Account ID": resource.account_id,
                "Object Type": resource.object_type,
                "Object Name": resource.object_name,
                "Backup Plan Name": "; ".join(plan_names),
            }
        )

    write_csv(args.output, rows)
    total = len(all_resources)
    not_covered = total - covered_count

    print_colored(Colors.GREEN, f"CSV report written to {args.output}")
    print_colored(Colors.CYAN, f"Total resources: {total}")
    print_colored(Colors.GREEN, f"Covered count: {covered_count}")
    if not_covered > 0:
        print_colored(Colors.RED, f"Not covered count: {not_covered}")
    else:
        print_colored(Colors.GREEN, "Not covered count: 0")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print_colored(Colors.YELLOW, "Interrupted by user")
        raise SystemExit(1)