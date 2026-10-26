import json
import os
import boto3
from botocore.exceptions import ClientError

# Import from Lambda Layer
import cors
import game_progress
from access_control import get_identity, can_access_game

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['GAMES_TABLE'])

def lambda_handler(event, context):
    game_id = event['pathParameters']['gameId']
    email, groups = get_identity(event)

    try:
        response = table.get_item(Key={'id': game_id})
        item = response.get('Item')

        # Return the same 404 whether the game doesn't exist or the user has
        # no access to it, so callers can't probe for restricted game ids.
        if not item or not can_access_game(email, groups, game_id):
            return {
                'statusCode': 404,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': cors.allowed_origin()
                },
                'body': json.dumps({'error': 'Game not found'})
            }

        # Only the headline status here. Which tasks are done belongs to
        # the attempt, so it rides on the session the page already polls.
        item['progress'] = {'status': game_progress.status_for(email, game_id)}

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
            },
            'body': json.dumps(item)
        }
    except ClientError as e:
        print(e.response['Error']['Message'])
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
            },
            'body': json.dumps({'error': 'Could not fetch game'})
        }