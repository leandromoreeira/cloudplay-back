import os
import json
import boto3
from botocore.exceptions import ClientError

# Import from Lambda Layer
import game_progress
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
        stack_status = session.get('stack_status', 'UNKNOWN')
        stack_outputs = session.get('stack_outputs', {})

        # The stack's status is written onto the session by stack_events as
        # CloudFormation reports it, so this is a plain read. Asking
        # CloudFormation from here is what used to cost an AssumeRole and a
        # DescribeStacks on every poll, and there is nothing left for it to
        # find out: a session's `active` is owned by session_teardown, and
        # every transition of the stack arrives on the topic.

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