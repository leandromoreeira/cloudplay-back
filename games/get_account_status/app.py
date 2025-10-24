import json
import os
import jwt
from botocore.exceptions import ClientError

# Import from Lambda Layer
from account_manager import get_account_manager


def lambda_handler(event, context):
    """
    Get the status of all game deployment accounts.
    Returns information about which accounts are available/busy.
    """

    # Verify authentication
    auth_header = event['headers'].get('Authorization')
    if not auth_header:
        return {
            'statusCode': 401,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'message': 'Authorization header required'})
        }

    try:
        jwt_token = auth_header.split(' ')[1]
        decoded_token = jwt.decode(jwt_token, options={"verify_signature": False})
        user_name = decoded_token.get('email')

        if not user_name:
            raise Exception("No email in token")

    except Exception as e:
        return {
            'statusCode': 401,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'message': 'Invalid or expired token'})
        }

    try:
        # Initialize account manager
        account_manager = get_account_manager()

        # Get status of all accounts
        account_status = account_manager.get_account_status()

        # Calculate summary
        total_accounts = len(account_status)
        available_accounts = len([acc for acc in account_status if acc.get('is_available', False)])
        busy_accounts = total_accounts - available_accounts

        response_data = {
            'accounts': account_status,
            'summary': {
                'total_accounts': total_accounts,
                'available_accounts': available_accounts,
                'busy_accounts': busy_accounts,
                'all_busy': available_accounts == 0
            }
        }

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(response_data)
        }

    except Exception as e:
        print(f"Error getting account status: {e}")
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'error': 'Failed to get account status',
                'details': str(e)
            })
        }