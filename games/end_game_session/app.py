import json
import boto3
import os
from datetime import datetime
from botocore.exceptions import ClientError

# Import from Lambda Layer
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

        # The session is over for the player the moment they ask to end it.
        # Wiping the account happens in the background; they don't wait for it.
        table.update_item(
            Key={'session_id': session_id},
            UpdateExpression='SET active = :active, end_time = :end_time, stack_status = :status',
            ExpressionAttributeValues={
                ':active': False,
                ':end_time': datetime.utcnow().isoformat() + 'Z',
                ':status': 'CLEANUP_IN_PROGRESS'
            }
        )

        if not account_id:
            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'message': f'Game session {game_id} ended successfully',
                    'sessionId': session_id,
                    'accountId': None
                })
            }

        # Hold the account before kicking off the wipe. The build refuses to run
        # against an account that isn't CLEANING, so this has to come first.
        if not account_manager.mark_cleaning(account_id):
            print(f"Account {account_id} was not IN_USE, skipping cleanup build")
            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'message': f'Game session {game_id} ended successfully',
                    'sessionId': session_id,
                    'accountId': account_id
                })
            }

        try:
            build = boto3.client('codebuild').start_build(
                projectName=os.environ['CLEANUP_PROJECT_NAME'],
                environmentVariablesOverride=[
                    {'name': 'TARGET_ACCOUNT', 'value': account_id, 'type': 'PLAINTEXT'}
                ]
            )
            build_id = build['build']['id']
            print(f"Started cleanup build {build_id} for account {account_id}")

            table.update_item(
                Key={'session_id': session_id},
                UpdateExpression='SET cleanup_build_id = :build_id',
                ExpressionAttributeValues={':build_id': build_id}
            )
        except ClientError as e:
            # The account is dirty and nothing is coming to clean it, so it
            # must not go back into the pool on its own.
            print(f"Could not start cleanup build: {e}")
            account_manager.mark_quarantined(
                account_id,
                f"could not start cleanup build: {e.response['Error']['Message']}"
            )
            return {
                'statusCode': 500,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': 'Session ended but account cleanup could not be started.',
                    'sessionId': session_id,
                    'accountId': account_id
                })
            }

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': f'Game session {game_id} ended successfully',
                'sessionId': session_id,
                'accountId': account_id,
                'cleanupBuildId': build_id
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