import json
import boto3
import os
from datetime import datetime
from botocore.exceptions import ClientError

# Import from Lambda Layer
from account_manager import get_account_manager
from access_control import get_identity, can_access_game


AWS_REGION = os.environ['AWS_REGION']

def lambda_handler(event, context):
    game_id = event['pathParameters']['gameId']
    user_name, groups = get_identity(event)

    if not user_name:
        return {
            'statusCode': 401,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'message': 'Invalid or expired token'})
        }

    if not can_access_game(user_name, groups, game_id):
        return {
            'statusCode': 403,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'message': 'You do not have access to this game'})
        }

    # Initialize account manager
    account_manager = get_account_manager()

    try:
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['GAME_SESSIONS_TABLE'])

        # Find the active session for this user and game
        response = table.query(
            IndexName='user_game_index',
            KeyConditionExpression='user_name = :user_name and game_id = :game_id',
            ExpressionAttributeValues={
                ':user_name': user_name,
                ':game_id': game_id,
                ':active': True
            },
            FilterExpression='active = :active'
        )

        if not response['Items']:
            return {
                'statusCode': 404,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({'message': 'No active game session found'})
            }

        session = response['Items'][0]
        session_id = session['session_id']
        account_id = session.get('account_id')  # Get the account where game is deployed
        stack_name = f'game-session-{session_id}'

        # Update stack status to indicate deletion is starting
        table.update_item(
            Key={'session_id': session_id},
            UpdateExpression='SET stack_status = :status',
            ExpressionAttributeValues={
                ':status': 'DELETE_IN_PROGRESS'
            }
        )

        try:
            # Use cross-account CloudFormation client if account_id is specified
            if account_id:
                cf_client = account_manager.create_cross_account_clients(account_id)
                print(f"Using cross-account CloudFormation client for account {account_id} to delete stack {stack_name}")
            else:
                # Fallback to local account (for backward compatibility)
                cf_client = boto3.client('cloudformation')
                print(f"Using local CloudFormation client to delete stack {stack_name} (no account_id found)")

            print(f"Deleting CloudFormation stack {stack_name}")
            cf_client.delete_stack(StackName=stack_name)
            message = f'Game session {game_id} ended successfully'

            # Update stack status to indicate deletion was initiated
            final_stack_status = 'DELETE_IN_PROGRESS'

        except ClientError as e:
            if e.response['Error']['Code'] == 'ValidationError' and 'does not exist' in e.response['Error']['Message']:
                print(f"Stack {stack_name} does not exist, continuing with session cleanup")
                message = f'Game session {game_id} ended successfully (stack already removed)'
                final_stack_status = 'DELETE_COMPLETE'
            else:
                print(f"Error deleting CloudFormation stack: {e}")
                final_stack_status = 'DELETE_FAILED'
                # The account still holds whatever the stack left behind, so it
                # stays out of the pool until someone looks at it.
                if account_id:
                    account_manager.mark_quarantined(
                        account_id,
                        f"delete_stack failed for {stack_name}: {e.response['Error']['Message']}"
                    )
                return {
                    "statusCode": 500,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    },
                    "body": json.dumps({
                        "error": "Could not delete CloudFormation stack.",
                        "details": e.response["Error"]["Message"],
                    }),
                }

        # Update the DynamoDB record to set end_time and stack_status, but keep active=true during deletion
        if final_stack_status == 'DELETE_COMPLETE':
            # Only set active=false when deletion is complete
            table.update_item(
                Key={'session_id': session_id},
                UpdateExpression='SET active = :active, end_time = :end_time, stack_status = :stack_status',
                ExpressionAttributeValues={
                    ':active': False,
                    ':end_time': datetime.utcnow().isoformat() + 'Z',
                    ':stack_status': final_stack_status
                }
            )
            # Nothing left to tear down, so the account is reusable right away
            if account_id:
                account_manager.mark_available(account_id)
        else:
            # Keep session active during deletion process
            table.update_item(
                Key={'session_id': session_id},
                UpdateExpression='SET end_time = :end_time, stack_status = :stack_status',
                ExpressionAttributeValues={
                    ':end_time': datetime.utcnow().isoformat() + 'Z',
                    ':stack_status': final_stack_status
                }
            )
            # Teardown is in flight; the account stays held until it finishes
            if account_id:
                account_manager.mark_cleaning(account_id)

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': message,
                'sessionId': session_id,
                'accountId': account_id
            })
        }

    except ClientError as e:
        print(f"Error with DynamoDB operation: {e}")
        return {
            "statusCode": 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            "body": json.dumps({
                "error": "Could not update game session.",
                "details": e.response["Error"]["Message"],
            }),
        }
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return {
            "statusCode": 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            "body": json.dumps({"error": "An unexpected error occurred."}),
        }