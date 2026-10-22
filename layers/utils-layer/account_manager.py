"""
Account Manager for Multi-Account Game Deployments

Account state lives in the GameAccounts table, one item per account, instead of
being derived from game sessions. That lets an account be explicitly held while
its teardown runs, so it is never handed to a new player mid-cleanup.

States:
    AVAILABLE   - free, can be claimed by a new session
    IN_USE      - a game session is running in it
    CLEANING    - teardown in flight, must not be handed out
    QUARANTINED - teardown failed; needs a human before returning to the pool

The pool itself is data: add or provision accounts with scripts/manage_accounts.py.
Each account carries the name of the role to assume in it, so the pool isn't tied
to one hardcoded role name and adding an account doesn't require a template change.
"""

import boto3
import os
from datetime import datetime
from botocore.exceptions import ClientError
from typing import Optional, List, Dict, Any

DEFAULT_ROLE_NAME = "CloudPlayGameRole"

# Where to send the "an account just left the pool" mail. Same address on both
# ends on purpose: SES in the sandbox only talks between verified identities,
# and one verification then covers sender and recipient alike.
ALERT_EMAIL_ENV = "ALERT_EMAIL"

STATUS_AVAILABLE = "AVAILABLE"
STATUS_IN_USE = "IN_USE"
STATUS_CLEANING = "CLEANING"
STATUS_QUARANTINED = "QUARANTINED"


def _now() -> str:
    return datetime.utcnow().isoformat() + 'Z'


class AccountManager:
    def __init__(self, accounts_table_name: str):
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(accounts_table_name)
        self.sts_client = boto3.client('sts')

    def claim_account(self, session_id: str) -> Optional[str]:
        """
        Atomically claim an available account for a session.

        The claim is a conditional write, so two sessions starting at the same
        time can never win the same account: the loser just tries the next one.

        Returns:
            str: Claimed account ID, or None if the whole pool is busy
        """
        try:
            response = self.table.scan(
                FilterExpression='#status = :available',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={':available': STATUS_AVAILABLE}
            )
        except ClientError as e:
            print(f"Error listing accounts: {e}")
            return None

        for item in response.get('Items', []):
            account_id = item['account_id']
            if self._try_claim(account_id, session_id):
                return account_id

        return None

    def _try_claim(self, account_id: str, session_id: str) -> bool:
        try:
            self.table.update_item(
                Key={'account_id': account_id},
                UpdateExpression=(
                    'SET #status = :in_use, session_id = :session_id, updated_at = :now '
                    'REMOVE failure_reason'
                ),
                ConditionExpression='#status = :available',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={
                    ':in_use': STATUS_IN_USE,
                    ':available': STATUS_AVAILABLE,
                    ':session_id': session_id,
                    ':now': _now(),
                }
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                # Another session claimed it first
                return False
            print(f"Error claiming account {account_id}: {e}")
            return False

    def mark_cleaning(self, account_id: str) -> bool:
        """Move an in-use account into teardown. Returns False if it wasn't in use."""
        return self._transition(
            account_id,
            new_status=STATUS_CLEANING,
            allowed_from=[STATUS_IN_USE]
        )

    def mark_available(self, account_id: str) -> bool:
        """Return an account to the pool once its teardown finished."""
        return self._transition(
            account_id,
            new_status=STATUS_AVAILABLE,
            allowed_from=[STATUS_CLEANING, STATUS_IN_USE]
        )

    def mark_quarantined(self, account_id: str, reason: str) -> bool:
        """
        Pull an account out of the pool after a failed teardown.

        Deliberately unconditional: this is the escape hatch, it has to work
        from any state.
        """
        try:
            self.table.update_item(
                Key={'account_id': account_id},
                UpdateExpression='SET #status = :status, failure_reason = :reason, updated_at = :now',
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues={
                    ':status': STATUS_QUARANTINED,
                    ':reason': reason,
                    ':now': _now(),
                }
            )
        except ClientError as e:
            print(f"Error quarantining account {account_id}: {e}")
            return False

        self._notify_quarantine(account_id, reason)
        return True

    def _notify_quarantine(self, account_id: str, reason: str) -> None:
        """
        Tell a human that an account just dropped out of the pool.

        A pool of three quietly becoming a pool of two is exactly the failure
        this alert exists to catch, but it must never be the reason the state
        change fails: the account really is quarantined either way, so every
        error here is swallowed and logged.
        """
        address = os.environ.get(ALERT_EMAIL_ENV)
        if not address:
            print(f"{ALERT_EMAIL_ENV} is unset, not sending the quarantine alert")
            return

        body = (
            f"Account {account_id} was quarantined and is no longer in the pool.\n\n"
            f"Reason: {reason}\n\n"
            f"It will not serve another game session until someone puts it back:\n"
            f"  python scripts/manage_accounts.py set-status "
            f"--account-id {account_id} --status AVAILABLE\n\n"
            f"Check what the cleanup build left behind before doing that."
        )

        try:
            boto3.client('ses').send_email(
                Source=address,
                Destination={'ToAddresses': [address]},
                Message={
                    'Subject': {'Data': f"[CloudPlay] Account {account_id} quarantined"},
                    'Body': {'Text': {'Data': body}},
                }
            )
            print(f"Quarantine alert for {account_id} sent to {address}")
        except Exception as e:
            print(f"Could not send the quarantine alert for {account_id}: {e}")

    def _transition(self, account_id: str, new_status: str, allowed_from: List[str]) -> bool:
        placeholders = {f':from{i}': status for i, status in enumerate(allowed_from)}
        condition = '#status IN ({})'.format(', '.join(placeholders))

        expression_values = {
            ':status': new_status,
            ':now': _now(),
        }
        expression_values.update(placeholders)

        try:
            self.table.update_item(
                Key={'account_id': account_id},
                UpdateExpression='SET #status = :status, updated_at = :now',
                ConditionExpression=condition,
                ExpressionAttributeNames={'#status': 'status'},
                ExpressionAttributeValues=expression_values
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                print(f"Account {account_id} is not in {allowed_from}, skipping move to {new_status}")
                return False
            print(f"Error moving account {account_id} to {new_status}: {e}")
            return False

    def _role_name_for(self, account_id: str) -> str:
        """
        Which role to assume in a given account.

        Accounts carry their own role_name, so the pool isn't tied to a single
        hardcoded name. The default only covers items written by hand without
        one, and matches the role the Lambdas are actually allowed to assume.
        """
        try:
            item = self.table.get_item(Key={'account_id': account_id}).get('Item') or {}
        except ClientError as e:
            print(f"Could not read role_name for {account_id}, using default: {e}")
            return DEFAULT_ROLE_NAME

        return item.get('role_name') or DEFAULT_ROLE_NAME

    def assume_cross_account_role(self, account_id: str) -> Dict[str, Any]:
        """
        Assume the orchestrator's role in the target account.

        Args:
            account_id: Target AWS account ID

        Returns:
            dict: Temporary credentials for the target account

        Raises:
            ClientError: If role assumption fails
        """
        role_arn = f"arn:aws:iam::{account_id}:role/{self._role_name_for(account_id)}"
        role_session_name = f"GameDeployment-{account_id}"

        try:
            response = self.sts_client.assume_role(
                RoleArn=role_arn,
                RoleSessionName=role_session_name,
                DurationSeconds=3600  # 1 hour should be enough for deployment
            )

            return response['Credentials']

        except ClientError as e:
            print(f"Failed to assume role {role_arn}: {e}")
            raise

    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        """The pool entry for one account, or None if it isn't in the pool."""
        try:
            return self.table.get_item(Key={'account_id': account_id}).get('Item')
        except ClientError as e:
            print(f"Error reading account {account_id}: {e}")
            return None

    def orchestrator_account_id(self) -> str:
        """The account the Lambdas run in, i.e. who the team role must trust."""
        return self.sts_client.get_caller_identity()['Account']

    def create_cross_account_client(self, account_id: str, service_name: str):
        """
        Create a boto3 client for any service inside a game account.

        Args:
            account_id: Target AWS account ID
            service_name: e.g. 'cloudformation', 'iam'

        Returns:
            A boto3 client authenticated into the target account
        """
        credentials = self.assume_cross_account_role(account_id)

        return boto3.client(
            service_name,
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )

    def create_cross_account_clients(self, account_id: str):
        """
        Create boto3 client for CloudFormation using cross-account credentials.

        Args:
            account_id: Target AWS account ID

        Returns:
            CloudFormation client for the target account
        """
        return self.create_cross_account_client(account_id, 'cloudformation')

    def get_account_status(self) -> List[Dict[str, Any]]:
        """
        Get the state of every account in the pool.

        Returns:
            list: One entry per account, sorted by account ID
        """
        try:
            response = self.table.scan()
        except ClientError as e:
            print(f"Error getting account status: {e}")
            return []

        accounts = [
            {
                'account_id': item['account_id'],
                'status': item.get('status'),
                'is_available': item.get('status') == STATUS_AVAILABLE,
                'session_id': item.get('session_id'),
                'updated_at': item.get('updated_at'),
                'failure_reason': item.get('failure_reason'),
            }
            for item in response.get('Items', [])
        ]

        return sorted(accounts, key=lambda account: account['account_id'])


def get_account_manager() -> AccountManager:
    """
    Factory function to create AccountManager instance.

    Returns:
        AccountManager: Configured instance
    """
    accounts_table = os.environ['GAME_ACCOUNTS_TABLE']
    return AccountManager(accounts_table)
