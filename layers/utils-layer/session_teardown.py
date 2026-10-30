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

import game_progress
import team_role

# Outcomes, from the happy path outwards
STARTED = 'STARTED'          # cleanup build is running
NO_ACCOUNT = 'NO_ACCOUNT'    # the session never held an account
SKIPPED = 'SKIPPED'          # the account wasn't IN_USE; it isn't ours to wipe
FAILED = 'FAILED'            # the build wouldn't start, so the account is quarantined


def _now():
    return datetime.utcnow().isoformat() + 'Z'


def _revoke_console_access(account_manager, account_id):
    """
    Take the player's permissions away the moment their session ends.

    A console session they opened lives up to an hour, and the account is
    about to be wiped underneath them. Deleting GamePolicy leaves only the
    deny-everything guardrails on the role, so an open console tab loses
    everything now instead of when the credential happens to expire.

    Best effort: the wipe is what actually matters and it is already on its
    way, and the credential expires within the hour regardless.
    """
    try:
        iam = account_manager.create_cross_account_client(account_id, 'iam')
        iam.delete_role_policy(
            RoleName=team_role.ROLE_NAME,
            PolicyName=team_role.GAME_POLICY_NAME,
        )
        print(f"Revoked console permissions in {account_id}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchEntity':
            return
        print(f"Could not revoke console permissions in {account_id}: {e}")
    except Exception as e:
        print(f"Could not revoke console permissions in {account_id}: {e}")


def _delete_lab_stack(account_manager, account_id, session_id):
    """
    Ask CloudFormation to start tearing the lab down, and don't wait.

    The wipe does not depend on this. What it buys is a head start: the
    cleanup build spends its first minute provisioning a container and
    downloading aws-nuke, and aws-nuke then calls DeleteStack itself and
    blocks on it. One observed game spent 18 of its 20 minutes inside that
    call. Starting the clock here means that minute overlaps instead of
    queueing behind.

    StackName is the only parameter worth sending, which took being told:

        "You can activate DeletionMode FORCE_DELETE_STACK in a delete stack
         operation only when the stack is in the DELETE_FAILED state."

    So neither forceful option can be used on a healthy stack. RetainResources
    is DELETE_FAILED-only too, and needs the logical IDs of whatever refused
    to go. Both belong to a second attempt after a stack sticks, and aws-nuke
    already makes exactly that attempt.
    """
    try:
        cloudformation = account_manager.create_cross_account_client(
            account_id, 'cloudformation'
        )
        cloudformation.delete_stack(StackName=f'game-session-{session_id}')
        print(f"Asked CloudFormation to delete game-session-{session_id}")
    except ClientError as e:
        print(f"Could not start the stack delete in {account_id}: {e}")
    except Exception as e:
        print(f"Could not start the stack delete in {account_id}: {e}")


def start_cleanup_build(account_id):
    """
    Put the aws-nuke build on an account, and hand back its id.

    Both the first attempt and every retry come through here, so there is
    one place that knows the project's name and how it is addressed.
    """
    build = boto3.client('codebuild').start_build(
        projectName=os.environ['CLEANUP_PROJECT_NAME'],
        environmentVariablesOverride=[
            {'name': 'TARGET_ACCOUNT', 'value': account_id, 'type': 'PLAINTEXT'}
        ]
    )
    return build['build']['id']


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

    # Whatever else happens below, the player is no longer in this game.
    # Best effort for the same reason as everything else here: wiping the
    # account matters more, and a stale "playing" clears itself the next
    # time they start the game.
    if session.get('user_name') and session.get('game_id'):
        try:
            game_progress.mark_not_playing(session['user_name'], session['game_id'])
        except Exception as e:
            print(f"Could not clear the playing flag for {session_id}: {e}")

    if not account_id:
        return _result(NO_ACCOUNT)

    # The build refuses to touch an account that isn't CLEANING, so the hold
    # has to be taken before the build starts, not after.
    if not account_manager.mark_cleaning(account_id):
        print(f"Account {account_id} was not IN_USE, skipping cleanup build")
        return _result(SKIPPED, account_id)

    # Only now: before the hold, the account could still belong to someone
    # else, and acting on it would be acting on the wrong account.
    _revoke_console_access(account_manager, account_id)
    _delete_lab_stack(account_manager, account_id, session_id)

    try:
        build_id = start_cleanup_build(account_id)
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

    print(f"Started cleanup build {build_id} for account {account_id}")

    sessions_table.update_item(
        Key={'session_id': session_id},
        UpdateExpression='SET cleanup_build_id = :build_id',
        ExpressionAttributeValues={':build_id': build_id}
    )

    return _result(STARTED, account_id, build_id)
