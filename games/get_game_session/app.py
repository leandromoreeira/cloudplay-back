import os
import json
import boto3
from botocore.exceptions import ClientError

# Import from Lambda Layer
import game_progress
from account_manager import get_account_manager
from access_control import get_identity, can_access_game


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
        # Query the GameSessionsTable for an active session for the given game and user
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['GAME_SESSIONS_TABLE'])

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

        if not response["Items"]:
            return {
                "statusCode": 200,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                "body": json.dumps({"active_session": False}),
            }

        # If a session is found, return its details
        session = response["Items"][0]
        session_id = session["session_id"]
        account_id = session.get("account_id")  # Get the account where game is deployed
        stack_name = f'game-session-{session_id}'

        # Check CloudFormation stack status if session is active
        stack_status = session.get('stack_status', 'UNKNOWN')
        stack_outputs = session.get('stack_outputs', {})

        # A session that has not asked CloudFormation for anything yet has no
        # stack to describe, and the ValidationError branch below would read
        # that as a finished teardown and close the session out from under
        # the player.
        if session.get('active', False) and stack_status != 'STARTING':
            try:
                # Use cross-account CloudFormation client if account_id is specified
                if account_id:
                    cf_client = account_manager.create_cross_account_clients(account_id)
                    print(f"Using cross-account CloudFormation client for account {account_id}")
                else:
                    # Fallback to local account (for backward compatibility)
                    cf_client = boto3.client('cloudformation')
                    print("Using local CloudFormation client (no account_id found)")

                stack_response = cf_client.describe_stacks(StackName=stack_name)
                current_stack_status = stack_response['Stacks'][0]['StackStatus']

                # Update DynamoDB with current stack status if it has changed
                if current_stack_status != stack_status:
                    table.update_item(
                        Key={'session_id': session_id},
                        UpdateExpression='SET stack_status = :status',
                        ExpressionAttributeValues={
                            ':status': current_stack_status
                        }
                    )
                    stack_status = current_stack_status

                # If stack is complete and we haven't retrieved outputs yet, get them now
                if ((current_stack_status == 'CREATE_COMPLETE' or current_stack_status == 'UPDATE_COMPLETE') and
                    (not stack_outputs or len(stack_outputs) == 0)):

                    print(f"Stack {stack_name} is complete, retrieving outputs...")
                    new_stack_outputs = {}

                    if 'Outputs' in stack_response['Stacks'][0]:
                        for output in stack_response['Stacks'][0]['Outputs']:
                            new_stack_outputs[output['OutputKey']] = output['OutputValue']

                    # Update DynamoDB with stack outputs
                    table.update_item(
                        Key={'session_id': session_id},
                        UpdateExpression='SET stack_outputs = :outputs',
                        ExpressionAttributeValues={
                            ':outputs': new_stack_outputs
                        }
                    )

                    stack_outputs = new_stack_outputs
                    print(f"Retrieved and stored stack outputs: {stack_outputs}")

            except ClientError as e:
                if e.response['Error']['Code'] == 'ValidationError':
                    # Stack doesn't exist - deletion was successful
                    stack_status = 'DELETE_COMPLETE'
                    # Set session as inactive since stack is gone
                    table.update_item(
                        Key={'session_id': session_id},
                        UpdateExpression='SET active = :active, stack_status = :status',
                        ExpressionAttributeValues={
                            ':active': False,
                            ':status': stack_status
                        }
                    )
                    session['active'] = False  # Update local session object
                    # Deliberately does NOT release the account: the stack
                    # disappearing is the start of the account wipe, not the end
                    # of it. The cleanup build's result decides when the account
                    # goes back into the pool.
                else:
                    print(f"Error checking stack status: {e}")
                    stack_status = 'ERROR'
            except Exception as e:
                print(f"Unexpected error checking stack status: {e}")
                stack_status = 'ERROR'

        return {
            "statusCode": 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            # Only what the player's screen actually needs. The account the
            # game runs in is infrastructure, and the session id is ours.
            "body": json.dumps(
                {
                    "active_session": session.get('active', False),
                    "start_time": session["start_time"],
                    "stack_status": stack_status,
                    "stack_outputs": stack_outputs,
                    # Which tasks are done in this attempt, and how long each
                    # took. Drives the ticks, the timings and which task the
                    # page lets the player open next.
                    "completed_tasks": game_progress.completed_view(session),
                }
            ),
        }

    except ClientError as e:
        print(f"Error querying DynamoDB: {e}")
        return {
            "statusCode": 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            "body": json.dumps(
                {
                    "error": "Could not retrieve game status.",
                    "details": e.response["Error"]["Message"],
                }
            ),
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