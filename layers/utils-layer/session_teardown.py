"""
Ending a game session and handing its account over to be wiped.

Two callers need this: the player pressing "end", and the janitor expiring a
session nobody ever ended. Keeping it in one place is what guarantees an
abandoned session is torn down exactly the same way a finished one is — the
account held, the build started, and the account quarantined if it isn't.
"""

import os
import boto3
from datetime import datetime
from botocore.exceptions import ClientError

# Outcomes, from the happy path outwards
STARTED = 'STARTED'          # cleanup build is running
NO_ACCOUNT = 'NO_ACCOUNT'    # the session never held an account
SKIPPED = 'SKIPPED'          # the account wasn't IN_USE; it isn't ours to wipe
FAILED = 'FAILED'            # the build wouldn't start, so the account is quarantined


def _now():
    return datetime.utcnow().isoformat() + 'Z'


def _result(outcome, account_id=None, build_id=None, error=None):
    return {
        'outcome': outcome,
        'account_id': account_id,
        'build_id': build_id,
        'error': error,
    }


def end_session(sessions_table, session, account_manager, reason=None):
    """
    Close a session out and start the wipe of the account behind it.

    The session is over for the player the moment this returns; wiping the
    account happens in the background and nobody waits for it.

    Args:
        sessions_table: boto3 Table for the game sessions
        session: the session item, needs at least session_id
        account_manager: an AccountManager
        reason: why the session ended, stored on the item when given

    Returns:
        dict: outcome (one of the constants above), account_id, build_id, error
    """
    session_id = session['session_id']
    account_id = session.get('account_id')

    update = 'SET active = :active, end_time = :end_time, stack_status = :status'
    values = {
        ':active': False,
        ':end_time': _now(),
        ':status': 'CLEANUP_IN_PROGRESS',
    }
    if reason:
        update += ', ended_reason = :reason'
        values[':reason'] = reason

    sessions_table.update_item(
        Key={'session_id': session_id},
        UpdateExpression=update,
        ExpressionAttributeValues=values
    )

    if not account_id:
        return _result(NO_ACCOUNT)

    # The build refuses to touch an account that isn't CLEANING, so the hold
    # has to be taken before the build starts, not after.
    if not account_manager.mark_cleaning(account_id):
        print(f"Account {account_id} was not IN_USE, skipping cleanup build")
        return _result(SKIPPED, account_id)

    try:
        build = boto3.client('codebuild').start_build(
            projectName=os.environ['CLEANUP_PROJECT_NAME'],
            environmentVariablesOverride=[
                {'name': 'TARGET_ACCOUNT', 'value': account_id, 'type': 'PLAINTEXT'}
            ]
        )
    except ClientError as e:
        # The account is dirty and nothing is coming to clean it, so it must
        # not drift back into the pool on its own.
        message = e.response['Error']['Message']
        print(f"Could not start cleanup build for {account_id}: {e}")
        account_manager.mark_quarantined(
            account_id,
            f"could not start cleanup build: {message}"
        )
        return _result(FAILED, account_id, error=message)

    build_id = build['build']['id']
    print(f"Started cleanup build {build_id} for account {account_id}")

    sessions_table.update_item(
        Key={'session_id': session_id},
        UpdateExpression='SET cleanup_build_id = :build_id',
        ExpressionAttributeValues={':build_id': build_id}
    )

    return _result(STARTED, account_id, build_id)
