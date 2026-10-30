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

# A failed wipe is retried from here rather than from inside the build.
# Waiting for AWS to release an ENI is waiting, and doing it inside a
# CodeBuild container means paying by the minute to sleep; this function
# already wakes up on a schedule and costs the same whether it finds
# anything or not.
RETRY_CLEANUP_AFTER_MINUTES = float(os.environ.get('RETRY_CLEANUP_AFTER_MINUTES', '15'))
MAX_CLEANUP_ATTEMPTS = int(os.environ.get('MAX_CLEANUP_ATTEMPTS', '3'))


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


def _retry_cleanups(account_manager, now):
    """
    Start another wipe on accounts whose last one failed.

    cleanup_failed_at is only written once a build has finished and failed,
    so nothing here can collide with an attempt still running.
    """
    retried, quarantined = [], []

    for account in account_manager.cleanups_awaiting_retry():
        account_id = account['account_id']
        failed_at = _started_at({'start_time': account.get('cleanup_failed_at')})

        if failed_at is None:
            print(f"Account {account_id} has an unreadable cleanup_failed_at, leaving it")
            continue

        waited = (now - failed_at).total_seconds() / 60
        if waited < RETRY_CLEANUP_AFTER_MINUTES:
            continue

        attempts = int(account.get('cleanup_attempts', 0))
        if attempts >= MAX_CLEANUP_ATTEMPTS:
            # Out of patience: something is genuinely stuck rather than
            # slow, and that needs a person.
            account_manager.mark_quarantined(
                account_id,
                f"cleanup still failing after {attempts} attempts"
            )
            quarantined.append(account_id)
            continue

        try:
            # Cleared first: if the build fails again, cleanup_result writes
            # a fresh marker, and if starting it raises we would rather try
            # again next sweep than lose the account to silence.
            account_manager.clear_cleanup_failure(account_id)
            build_id = session_teardown.start_cleanup_build(account_id)
            print(f"Retrying cleanup of {account_id} (attempt {attempts + 1}): {build_id}")
            retried.append(account_id)
        except ClientError as e:
            print(f"Could not retry the cleanup of {account_id}: {e}")
        except Exception as e:
            print(f"Unexpected error retrying the cleanup of {account_id}: {e}")

    return retried, quarantined


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

    retried, quarantined = _retry_cleanups(account_manager, now)

    print(
        f"Janitor checked {checked} active session(s), expired {len(expired)}, "
        f"retried {len(retried)} cleanup(s), quarantined {len(quarantined)}"
    )
    return {
        'checked': checked,
        'expired': expired,
        'retried_cleanups': retried,
        'quarantined': quarantined,
    }
