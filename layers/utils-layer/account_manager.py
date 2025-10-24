"""
Account Manager for Multi-Account Game Deployments

This module handles:
- Account selection logic to ensure only one game per account
- Cross-account role assumption for CloudFormation deployments
- Account availability checking
"""

import boto3
import os
from botocore.exceptions import ClientError
from typing import Optional, List, Dict, Any

# Configuration for available accounts
AVAILABLE_ACCOUNTS = [
    "021914682333",  # account1
    "339105149717"   # account2
]

CROSS_ACCOUNT_ROLE_NAME = "OrganizationAccountAccessRole"


class AccountManager:
    def __init__(self, game_sessions_table_name: str):
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(game_sessions_table_name)
        self.sts_client = boto3.client('sts')

    def get_available_account(self) -> Optional[str]:
        """
        Find an available account that doesn't have any active game sessions.

        Returns:
            str: Account ID if available, None if all accounts are busy
        """
        for account_id in AVAILABLE_ACCOUNTS:
            if self._is_account_available(account_id):
                return account_id
        return None

    def _is_account_available(self, account_id: str) -> bool:
        """
        Check if an account has any active game sessions.

        Args:
            account_id: AWS account ID to check

        Returns:
            bool: True if account is available, False if busy
        """
        try:
            response = self.table.query(
                IndexName='account_index',
                KeyConditionExpression='account_id = :account_id',
                ExpressionAttributeValues={
                    ':account_id': account_id,
                    ':active': True
                },
                FilterExpression='active = :active'
            )

            # Account is available if no active sessions found
            return len(response.get('Items', [])) == 0

        except ClientError as e:
            print(f"Error checking account availability for {account_id}: {e}")
            # If we can't check, assume it's not available for safety
            return False

    def assume_cross_account_role(self, account_id: str) -> Dict[str, Any]:
        """
        Assume the OrganizationAccountAccessRole in the target account.

        Args:
            account_id: Target AWS account ID

        Returns:
            dict: Temporary credentials for the target account

        Raises:
            ClientError: If role assumption fails
        """
        role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"
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

    def create_cross_account_clients(self, account_id: str):
        """
        Create boto3 client for CloudFormation using cross-account credentials.

        Args:
            account_id: Target AWS account ID

        Returns:
            CloudFormation client for the target account
        """
        credentials = self.assume_cross_account_role(account_id)

        # Create CloudFormation client with temporary credentials
        cf_client = boto3.client(
            'cloudformation',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )

        return cf_client

    def get_account_status(self) -> List[Dict[str, Any]]:
        """
        Get status of all configured accounts.

        Returns:
            list: List of account status information
        """
        account_status = []

        for account_id in AVAILABLE_ACCOUNTS:
            try:
                # Query active sessions for this account
                response = self.table.query(
                    IndexName='account_index',
                    KeyConditionExpression='account_id = :account_id',
                    ExpressionAttributeValues={
                        ':account_id': account_id,
                        ':active': True
                    },
                    FilterExpression='active = :active'
                )

                active_sessions = response.get('Items', [])
                is_available = len(active_sessions) == 0

                account_info = {
                    'account_id': account_id,
                    'is_available': is_available,
                    'active_sessions': len(active_sessions),
                    'sessions': active_sessions if active_sessions else []
                }

                account_status.append(account_info)

            except ClientError as e:
                print(f"Error getting status for account {account_id}: {e}")
                account_status.append({
                    'account_id': account_id,
                    'is_available': False,
                    'active_sessions': -1,  # Indicates error
                    'error': str(e),
                    'sessions': []
                })

        return account_status


def get_account_manager() -> AccountManager:
    """
    Factory function to create AccountManager instance.

    Returns:
        AccountManager: Configured instance
    """
    game_sessions_table = os.environ['GAME_SESSIONS_TABLE']
    return AccountManager(game_sessions_table)