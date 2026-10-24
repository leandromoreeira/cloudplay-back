import json
import boto3
import os
from botocore.exceptions import ClientError

# Import from Lambda Layer
import session_teardown
from account_manager import get_account_manager
from access_control import get_identity, can_access_game


def _response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body)
    }


def lambda_handler(event, context):
    game_id = event['pathParameters']['gameId']
    user_name, groups = get_identity(event)

    if not user_name:
        return _response(401, {'message': 'Invalid or expired token'})

    if not can_access_game(user_name, groups, game_id):
        return _response(403, {'message': 'You do not have access to this game'})

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
            return _response(404, {'message': 'No active game session found'})

        session = response['Items'][0]
        result = session_teardown.end_session(
            table, session, account_manager, reason='ended by player'
        )

        if result['outcome'] == session_teardown.FAILED:
            return _response(500, {
                'error': 'Session ended but account cleanup could not be started.',
                'sessionId': session['session_id']
            })

        return _response(200, {
            'message': f'Game session {game_id} ended successfully',
            'sessionId': session['session_id']
        })

    except ClientError as e:
        print(f"Error with DynamoDB operation: {e}")
        return _response(500, {
            'error': 'Could not update game session.',
            'details': e.response['Error']['Message'],
        })
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return _response(500, {'error': 'An unexpected error occurred.'})
