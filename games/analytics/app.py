"""
GET /analytics — how far people have got, and how long it took them.

Who sees what is decided here and nowhere else: a player sees their own
rows, an administrator sees everyone's and may ask for one person. The
page hides what it should not offer, but that is cosmetic — the scope is
settled from the groups the Cognito authorizer attached to the request.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

# Import from Lambda Layer
import analytics
import cors
from access_control import get_identity, is_admin


def _response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': cors.allowed_origin(),
        },
        'body': json.dumps(body, default=str)
    }


def _table(name):
    return boto3.resource('dynamodb').Table(os.environ[name])


def _scan(table, **kwargs):
    """Every page of a scan. These tables are small and stay that way."""
    items = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get('Items', []))

        last = page.get('LastEvaluatedKey')
        if not last:
            return items
        kwargs['ExclusiveStartKey'] = last


def _sessions(user=None):
    table = _table('GAME_SESSIONS_TABLE')

    if user:
        # user_name is the hash key of this index, so one query beats a
        # scan even while the table is small.
        return table.query(
            IndexName='user_game_index',
            KeyConditionExpression=Key('user_name').eq(user)
        ).get('Items', [])

    return _scan(table)


def _finished_pairs(user=None):
    """
    (game_id, player) for every game ever completed.

    Read from the progress table rather than counted from sessions: a game
    finished last week is still finished, and replaying it must not count
    twice.
    """
    table = _table('GAME_PROGRESS_TABLE')

    if user:
        items = table.query(
            KeyConditionExpression=Key('user_email').eq(user)
        ).get('Items', [])
    else:
        items = _scan(table)

    return [
        (item['game_id'], item['user_email'])
        for item in items if item.get('ever_completed_at')
    ]


def _titles():
    """Game ids to titles, so the tables read as names and not as game103."""
    try:
        games = _scan(_table('GAMES_TABLE'), ProjectionExpression='id, title')
    except ClientError as e:
        print(f"Could not read the catalogue for titles: {e}")
        return {}

    return {g['id']: g.get('title', g['id']) for g in games if g.get('id')}


def lambda_handler(event, context):
    caller, groups = get_identity(event)

    if not caller:
        return _response(401, {'message': 'Invalid or expired token'})

    params = event.get('queryStringParameters') or {}
    wanted = (params.get('user') or '').strip().lower() or None
    admin = is_admin(groups)

    # A player may ask about themselves and nobody else, however the URL
    # is written.
    if not admin:
        if wanted and wanted != caller.lower():
            return _response(403, {'message': 'You can only see your own progress'})
        wanted = caller

    try:
        sessions = _sessions(wanted)
        finished = _finished_pairs(wanted)
        titles = _titles()
    except ClientError as e:
        print(f"Could not read the analytics data: {e}")
        return _response(500, {'message': 'Could not read the numbers'})

    body = {
        'scope': 'user' if wanted else 'all',
        'user': wanted,
        'summary': analytics.summarise(sessions, len(finished)),
        'per_game': analytics.per_game(sessions, titles, finished),
        'per_task': analytics.per_task(sessions, titles),
        'recent': analytics.recent(sessions, titles),
    }

    if admin:
        # Only an administrator is told who else exists, and the filter on
        # their screen is built from this.
        body['per_user'] = analytics.per_user(sessions, finished)

    return _response(200, body)
