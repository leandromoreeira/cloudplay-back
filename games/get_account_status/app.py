import json

# Import from Lambda Layer
import cors
from account_manager import get_account_manager
from access_control import get_identity


def lambda_handler(event, context):
    """
    Get the status of all game deployment accounts.
    Returns information about which accounts are available/busy.
    """

    # Identity comes from the claims the Cognito authorizer already verified,
    # like every other handler. Decoding the token here would just be a second,
    # unverified opinion about who the caller is.
    user_name, _groups = get_identity(event)

    if not user_name:
        return {
            'statusCode': 401,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
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
        cleaning_accounts = len([acc for acc in account_status if acc.get('status') == 'CLEANING'])
        quarantined_accounts = len([acc for acc in account_status if acc.get('status') == 'QUARANTINED'])

        response_data = {
            'accounts': account_status,
            'summary': {
                'total_accounts': total_accounts,
                'available_accounts': available_accounts,
                'busy_accounts': busy_accounts,
                'cleaning_accounts': cleaning_accounts,
                'quarantined_accounts': quarantined_accounts,
                'all_busy': available_accounts == 0
            }
        }

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
            },
            'body': json.dumps(response_data)
        }

    except Exception as e:
        print(f"Error getting account status: {e}")
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
            },
            'body': json.dumps({
                'error': 'Failed to get account status',
                'details': str(e)
            })
        }
