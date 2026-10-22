#!/usr/bin/env python3
"""
Admin CLI for the pool of AWS accounts games are deployed into.

Account lifecycle:
  AVAILABLE   - free, can be claimed by a new session
  IN_USE      - a game session is running in it
  CLEANING    - teardown in flight, must not be handed out
  QUARANTINED - teardown failed; needs a human before returning to the pool

`provision` creates a brand new AWS account and registers it, and is the normal
way to grow the pool. `add` only registers an account that already exists.

Run `provision` with credentials for the ORGANIZATION MANAGEMENT ACCOUNT —
organizations:CreateAccount exists nowhere else.

If your profile comes from `aws login`, AWS_PROFILE alone is not enough: the
AWS CLI v2 bundles its own botocore that reads that credential cache, while a
standalone boto3 does not, and you get NoCredentialsError. Export them first:

    eval "$(aws configure export-credentials --profile <profile> --format env)"

Examples:
  python manage_accounts.py list
  python manage_accounts.py provision --email leandro.lx3+cloudplay-03@gmail.com \\
      --ou-id ou-abcd-12345678 --orchestrator-account 673000199571
  python manage_accounts.py add --account-id 369488682912
  python manage_accounts.py remove --account-id 369488682912
  python manage_accounts.py set-status --account-id 369488682912 --status AVAILABLE
  python manage_accounts.py team-role --account-id 369488682912
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# The player's role and its guardrails are defined once, in the Lambda layer,
# so this script and start_game_session can never drift apart on what a
# guardrail is.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'layers' / 'utils-layer'))
import team_role  # noqa: E402

STATUSES = ['AVAILABLE', 'IN_USE', 'CLEANING', 'QUARANTINED']

DEFAULT_ROLE_NAME = 'CloudPlayGameRole'
CREATE_POLL_SECONDS = 10
CREATE_TIMEOUT_SECONDS = 900


def now():
    return datetime.utcnow().isoformat() + 'Z'


def list_accounts(table):
    items = sorted(table.scan().get('Items', []), key=lambda i: i['account_id'])

    if not items:
        print("Pool is empty. Add accounts with: manage_accounts.py add --account-id <id>")
        return

    print(f"{'ACCOUNT':<16} {'STATUS':<13} {'SESSION':<38} UPDATED")
    for item in items:
        print(
            f"{item['account_id']:<16} "
            f"{item.get('status', '?'):<13} "
            f"{str(item.get('session_id', '-')):<38} "
            f"{item.get('updated_at', '-')}"
        )
        if item.get('failure_reason'):
            print(f"{'':<16} └─ {item['failure_reason']}")


def add_account(table, account_id, role_name):
    try:
        table.put_item(
            Item={
                'account_id': account_id,
                'status': 'AVAILABLE',
                'role_name': role_name,
                'updated_at': now(),
            },
            ConditionExpression='attribute_not_exists(account_id)',
        )
        print(f"✅ {account_id} added to the pool as AVAILABLE (role {role_name})")
        print(f"⚠️  The account must already have a {role_name} role trusting this account")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print(f"⚠️  {account_id} is already in the pool (use set-status to change its state)")
            return
        raise


def remove_account(table, account_id):
    response = table.get_item(Key={'account_id': account_id})
    item = response.get('Item')

    if not item:
        print(f"⚠️  {account_id} is not in the pool")
        return

    if item.get('status') in ('IN_USE', 'CLEANING'):
        print(f"❌ {account_id} is {item['status']} — wait for it to settle before removing it")
        sys.exit(1)

    table.delete_item(Key={'account_id': account_id})
    print(f"✅ {account_id} removed from the pool")


def set_status(table, account_id, status):
    try:
        table.update_item(
            Key={'account_id': account_id},
            UpdateExpression='SET #status = :status, updated_at = :now REMOVE failure_reason, session_id',
            ConditionExpression='attribute_exists(account_id)',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':status': status, ':now': now()},
        )
        print(f"✅ {account_id} is now {status}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print(f"❌ {account_id} is not in the pool")
            sys.exit(1)
        raise


def create_account(organizations, email, account_name, role_name):
    """Create the account and wait for Organizations to finish provisioning it."""
    print(f"Creating account '{account_name}' <{email}> with role {role_name}...")

    request_id = organizations.create_account(
        Email=email,
        AccountName=account_name,
        RoleName=role_name,
    )['CreateAccountStatus']['Id']

    deadline = time.time() + CREATE_TIMEOUT_SECONDS
    while time.time() < deadline:
        status = organizations.describe_create_account_status(
            CreateAccountRequestId=request_id
        )['CreateAccountStatus']

        if status['State'] == 'SUCCEEDED':
            account_id = status['AccountId']
            print(f"✅ Account {account_id} created")
            return account_id

        if status['State'] == 'FAILED':
            raise SystemExit(f"❌ Account creation failed: {status.get('FailureReason')}")

        print(f"   still {status['State']}...")
        time.sleep(CREATE_POLL_SECONDS)

    raise SystemExit(f"❌ Account creation did not finish within {CREATE_TIMEOUT_SECONDS}s "
                     f"(request {request_id}); check the console before retrying")


def retrust_role(account_id, role_name, orchestrator_account, management_account, region):
    """
    Point the account's role at the orchestrator account.

    Organizations creates the role trusting only the management account, so if
    the Lambdas run somewhere else they can't assume it until we rewrite the
    trust policy. Management stays in the trust so this script can still get
    back in later.

    This MUST run before the account joins the guardrails OU: the SCP there
    denies iam:UpdateAssumeRolePolicy on this very role.
    """
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    sts = boto3.client('sts', region_name=region)

    credentials = None
    for attempt in range(10):
        try:
            credentials = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName='CloudPlayProvisioning',
            )['Credentials']
            break
        except ClientError as e:
            # A freshly created account takes a little while to become assumable
            print(f"   waiting for {role_name} to become assumable ({attempt + 1}/10)...")
            time.sleep(CREATE_POLL_SECONDS)
    else:
        raise SystemExit(f"❌ Could not assume {role_arn}")

    iam = boto3.client(
        'iam',
        region_name=region,
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken'],
    )

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "AWS": [
                        f"arn:aws:iam::{orchestrator_account}:root",
                        f"arn:aws:iam::{management_account}:root",
                    ]
                },
                "Action": "sts:AssumeRole",
            }
        ],
    }

    iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(trust_policy))
    print(f"✅ {role_name} now trusts orchestrator account {orchestrator_account}")


def setup_team_role(account_id, role_name, orchestrator_account, region):
    """
    Put HengHaTeamRole into a game account, or refresh the one already there.

    Idempotent, so it doubles as the way to roll a guardrail change out to
    accounts that were provisioned before it existed.
    """
    sts = boto3.client('sts', region_name=region)

    if orchestrator_account is None:
        orchestrator_account = sts.get_caller_identity()['Account']

    credentials = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{role_name}",
        RoleSessionName='CloudPlayTeamRoleSetup',
    )['Credentials']

    iam = boto3.client(
        'iam',
        region_name=region,
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken'],
    )

    created = team_role.ensure_team_role(iam, orchestrator_account)
    verb = 'created in' if created else 'already in'
    print(f"✅ {team_role.ROLE_NAME} {verb} {account_id}, "
          f"guardrails ({team_role.GUARDRAIL_POLICY_NAME}) up to date")


def move_to_ou(organizations, account_id, ou_id):
    root_id = organizations.list_roots()['Roots'][0]['Id']
    organizations.move_account(
        AccountId=account_id,
        SourceParentId=root_id,
        DestinationParentId=ou_id,
    )
    print(f"✅ Account {account_id} moved into {ou_id} (guardrail SCP now applies)")


def provision(table, email, account_name, ou_id, role_name, orchestrator_account, region):
    organizations = boto3.client('organizations', region_name=region)
    management_account = boto3.client('sts', region_name=region).get_caller_identity()['Account']

    if orchestrator_account is None:
        orchestrator_account = management_account

    account_id = create_account(organizations, email, account_name, role_name)

    if orchestrator_account != management_account:
        retrust_role(account_id, role_name, orchestrator_account, management_account, region)
    else:
        print(f"ℹ️  Orchestrator is the management account, role trust already correct")

    move_to_ou(organizations, account_id, ou_id)
    setup_team_role(account_id, role_name, orchestrator_account, region)

    table.put_item(
        Item={
            'account_id': account_id,
            'status': 'AVAILABLE',
            'role_name': role_name,
            'updated_at': now(),
        }
    )
    print(f"✅ Account {account_id} registered in the pool as AVAILABLE")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--stack-name', default='aws-jam', help="SAM stack name (default: aws-jam)")
    parser.add_argument('--region', default='us-east-1')

    subparsers = parser.add_subparsers(dest='command', required=True)

    subparsers.add_parser('list')

    p = subparsers.add_parser('add', help="register an account that already exists")
    p.add_argument('--account-id', required=True)
    p.add_argument('--role-name', default=DEFAULT_ROLE_NAME)

    p = subparsers.add_parser('remove')
    p.add_argument('--account-id', required=True)

    p = subparsers.add_parser('set-status')
    p.add_argument('--account-id', required=True)
    p.add_argument('--status', required=True, choices=STATUSES)

    p = subparsers.add_parser('team-role',
                              help=f"create or refresh {team_role.ROLE_NAME} in an account")
    p.add_argument('--account-id', required=True)
    p.add_argument('--role-name', default=DEFAULT_ROLE_NAME,
                   help="role to assume to get in (not the one being created)")
    p.add_argument('--orchestrator-account',
                   help="account the role should trust (default: the caller's)")

    p = subparsers.add_parser('provision', help="create a new AWS account and register it")
    p.add_argument('--email', required=True, help="unique root email, e.g. you+cloudplay-03@gmail.com")
    p.add_argument('--name', help="account name (default: derived from the email)")
    p.add_argument('--ou-id', required=True, help="OU from the org/organization.yaml stack output")
    p.add_argument('--role-name', default=DEFAULT_ROLE_NAME)
    p.add_argument('--orchestrator-account',
                   help="account the Lambdas run in (default: the management account)")

    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    table = dynamodb.Table(f"{args.stack_name}-Game-Accounts")

    try:
        if args.command == 'list':
            list_accounts(table)
        elif args.command == 'add':
            add_account(table, args.account_id, args.role_name)
        elif args.command == 'remove':
            remove_account(table, args.account_id)
        elif args.command == 'set-status':
            set_status(table, args.account_id, args.status)
        elif args.command == 'team-role':
            setup_team_role(args.account_id, args.role_name,
                            args.orchestrator_account, args.region)
        elif args.command == 'provision':
            provision(
                table,
                email=args.email,
                account_name=args.name or args.email.split('@')[0],
                ou_id=args.ou_id,
                role_name=args.role_name,
                orchestrator_account=args.orchestrator_account,
                region=args.region,
            )
    except ClientError as e:
        print(f"❌ {e.response['Error']['Message']}")
        sys.exit(1)


if __name__ == '__main__':
    main()
