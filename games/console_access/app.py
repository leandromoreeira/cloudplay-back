"""
Hands a player a console session in the account their game is running in.

Cognito says who the caller is; this says whether they still own an active
session in that account, and only then brokers the AWS credentials.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

# Import from Lambda Layer
import console_access
from account_manager import get_account_manager, STATUS_IN_USE
from access_control import get_identity, can_access_game

# The lab has to be up. The button only appears once it is, but the client
# is not what decides that.
READY_STACK_STATUSES = ('CREATE_COMPLETE', 'UPDATE_COMPLETE')


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

    try:
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

        if not response['Items']:
            return _response(404, {'message': 'No active game session found'})

        session = response['Items'][0]
        account_id = session.get('account_id')
        stack_status = session.get('stack_status')

        if stack_status not in READY_STACK_STATUSES:
            return _response(409, {
                'message': 'The game environment is not ready yet',
                'stackStatus': stack_status
            })

        account_manager = get_account_manager()
        account = account_manager.get_account(account_id) if account_id else None

        # A session row can outlive the account's claim on it, through the
        # janitor or a teardown that went sideways. Signing someone in to an
        # account the pool has already moved on from is the one thing here
        # worth being strict about.
        if (not account
                or account.get('status') != STATUS_IN_USE
                or account.get('session_id') != session['session_id']):
            return _response(409, {'message': 'This session no longer holds its account'})

        credentials = console_access.player_credentials(account_id, user_name)
        issuer = f"{os.environ['FRONTEND_URL']}/reconnect?game={game_id}"
        url = console_access.signin_url(credentials, issuer=issuer)

        # Never the URL itself: it signs whoever holds it in as the player.
        print(f"Console session for {user_name} in {account_id}, "
              f"expires {credentials['Expiration'].isoformat()}")

        # expiresAt stays: it is when this console session dies and the
        # player gets bounced to /reconnect, which the page needs to know.
        return _response(200, {
            'consoleUrl': url,
            'expiresAt': credentials['Expiration'].isoformat()
        })

    except ClientError as e:
        print(f"Could not broker a console session: {e}")
        return _response(500, {
            'error': 'Could not create a console session.',
            'details': e.response['Error']['Message']
        })
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return _response(500, {'error': 'An unexpected error occurred.'})
