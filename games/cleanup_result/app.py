"""
Turns the outcome of an account cleanup build into account and session state.

The build is only responsible for wiping the account. This owns the state
transition, because a build that times out or gets stopped never reaches its
own last step, and an account stuck in CLEANING is an account permanently out
of the pool.
"""

import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# Import from Lambda Layer
from account_manager import get_account_manager


def _target_account(detail):
    """The account the build was asked to wipe, from its env var overrides."""
    env = detail.get('additional-information', {}).get('environment', {})

    for variable in env.get('environment-variables', []):
        if variable.get('name') == 'TARGET_ACCOUNT':
            return variable.get('value')

    return None


def _build_name(build_id):
    """
    EventBridge reports the build's ARN; the session stored its short id.

    arn:aws:codebuild:us-east-1:123:build/project:uuid -> project:uuid
    """
    if not build_id:
        return None

    return build_id.split(':build/', 1)[-1]


def _close_session(account_id, build_id, stack_status):
    """
    Write the outcome back onto the session this build was cleaning up after.

    Without this the session is left reading CLEANUP_IN_PROGRESS forever, and
    there is no way to tell from the sessions table whether the wipe finished
    or quietly failed. Matching on the build id rather than on the account
    keeps it from touching the account's earlier sessions.
    """
    table = boto3.resource('dynamodb').Table(os.environ['GAME_SESSIONS_TABLE'])
    wanted = _build_name(build_id)

    try:
        sessions = table.query(
            IndexName='account_index',
            KeyConditionExpression=Key('account_id').eq(account_id)
        ).get('Items', [])
    except ClientError as e:
        print(f"Could not look up the session for {account_id}: {e}")
        return

    for session in sessions:
        if session.get('cleanup_build_id') != wanted:
            continue

        try:
            table.update_item(
                Key={'session_id': session['session_id']},
                UpdateExpression='SET stack_status = :status',
                ExpressionAttributeValues={':status': stack_status}
            )
            print(f"Session {session['session_id']} marked {stack_status}")
        except ClientError as e:
            print(f"Could not update session {session['session_id']}: {e}")
        return

    print(f"No session in {account_id} is waiting on build {wanted}")


def lambda_handler(event, context):
    detail = event.get('detail', {})
    status = detail.get('build-status')
    build_id = detail.get('build-id')

    account_id = _target_account(detail)
    if not account_id or account_id == 'unset':
        print(f"Build {build_id} finished as {status} with no TARGET_ACCOUNT, nothing to do")
        return

    account_manager = get_account_manager()

    if status == 'SUCCEEDED':
        account_manager.mark_available(account_id)
        _close_session(account_id, build_id, 'CLEANUP_COMPLETE')
        print(f"Account {account_id} wiped, back in the pool")
        return

    account_manager.mark_quarantined(
        account_id,
        f"cleanup build {status}: {build_id}"
    )
    _close_session(account_id, build_id, 'CLEANUP_FAILED')
    print(f"Account {account_id} quarantined after cleanup build {status}")
