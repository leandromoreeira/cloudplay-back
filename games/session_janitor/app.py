"""
Expires game sessions that nobody ended.

A player who closes the tab never calls /end, and an active session holds its
account out of the pool forever. With a handful of accounts that is the
difference between a working event and an empty pool, so anything older than
MAX_SESSION_HOURS gets torn down on a schedule — the same teardown the player
would have triggered by hand.
"""

import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr

# Import from Lambda Layer
import session_teardown
from account_manager import get_account_manager

MAX_SESSION_HOURS = float(os.environ.get('MAX_SESSION_HOURS', '3'))


def _started_at(session):
    """
    start_time is written as an ISO timestamp with a trailing Z.

    Returns None when it is missing or unreadable, which means we cannot tell
    the session's age and must leave it alone rather than guess it is old.
    """
    raw = session.get('start_time')

    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None


def _active_sessions(table):
    """Every session still marked active, across all pages of the scan."""
    kwargs = {'FilterExpression': Attr('active').eq(True)}

    while True:
        page = table.scan(**kwargs)
        for item in page.get('Items', []):
            yield item

        last_key = page.get('LastEvaluatedKey')
        if not last_key:
            return
        kwargs['ExclusiveStartKey'] = last_key


def lambda_handler(event, context):
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(os.environ['GAME_SESSIONS_TABLE'])
    account_manager = get_account_manager()

    now = datetime.now(timezone.utc)
    checked = 0
    expired = []

    for session in _active_sessions(table):
        checked += 1
        session_id = session['session_id']

        started = _started_at(session)
        if started is None:
            print(f"Session {session_id} has no readable start_time, leaving it alone")
            continue

        age_hours = (now - started).total_seconds() / 3600
        if age_hours < MAX_SESSION_HOURS:
            continue

        # One bad session must not stop the sweep: the whole point is to free
        # the accounts the others are holding.
        try:
            result = session_teardown.end_session(
                table,
                session,
                account_manager,
                reason=f'expired by janitor after {age_hours:.1f}h'
            )
            print(f"Expired session {session_id} after {age_hours:.1f}h: {result['outcome']}")
            expired.append({'session_id': session_id, **result})
        except ClientError as e:
            print(f"Could not expire session {session_id}: {e}")
        except Exception as e:
            print(f"Unexpected error expiring session {session_id}: {e}")

    print(f"Janitor checked {checked} active session(s), expired {len(expired)}")
    return {'checked': checked, 'expired': expired}
