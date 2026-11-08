"""
Stack events, pushed by CloudFormation instead of asked for.

create_stack is given this topic as a NotificationARN, so every state change
of a lab stack arrives here on its own. Before this, the only thing that ever
discovered a stack had finished was the player's page asking — each poll cost
an AssumeRole and a DescribeStacks in the game's account, and the session row
was only as current as the last question somebody had asked.

CloudFormation sends plain text, one key='value' per line, not JSON:

    StackName='game-session-abc'
    ResourceType='AWS::CloudFormation::Stack'
    ResourceStatus='CREATE_COMPLETE'
    Timestamp='2026-10-25T23:01:22.022Z'
"""

import os
import re
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from account_manager import get_account_manager

STACK_PREFIX = 'game-session-'
STACK_RESOURCE = 'AWS::CloudFormation::Stack'
COMPLETE = ('CREATE_COMPLETE', 'UPDATE_COMPLETE')

# A lab that never came up. Teardown statuses are not here: a cleanup that
# fails is the platform's problem afterwards, not a lab the player never got.
CREATION_FAILED = (
    'CREATE_FAILED', 'ROLLBACK_IN_PROGRESS', 'ROLLBACK_COMPLETE',
    'ROLLBACK_FAILED',
)

FIELD = re.compile(r"^(\w+)='(.*)'$")


def _fields(message):
    """The key='value' lines, as a dict. Lines that do not fit are skipped."""
    found = {}
    for line in message.splitlines():
        match = FIELD.match(line.strip())
        if match:
            found[match.group(1)] = match.group(2)

    return found


def _session_id(event):
    """
    The session a stack-level event belongs to, or None.

    Two things are filtered out here. Resource-level events, which are most
    of the traffic and say nothing about the stack as a whole. And nested
    stacks — a game template that nests its validation lambdas produces
    events whose ResourceType is also a stack, told apart by their logical
    id not being the stack's own name.
    """
    if event.get('ResourceType') != STACK_RESOURCE:
        return None

    stack_name = event.get('StackName', '')
    if event.get('LogicalResourceId') != stack_name:
        return None
    if not stack_name.startswith(STACK_PREFIX):
        return None

    return stack_name[len(STACK_PREFIX):]


def _outputs(account_id, stack_name):
    """
    The finished stack's outputs, which the notification does not carry.

    One cross-account call per session, at the moment it completes, in place
    of one on every poll for as long as the player had the page open.
    """
    try:
        cloudformation = get_account_manager().create_cross_account_clients(account_id)
        stacks = cloudformation.describe_stacks(StackName=stack_name)['Stacks']
    except ClientError as e:
        print(f"Could not read the outputs of {stack_name}: {e}")
        return None

    return {o['OutputKey']: o['OutputValue'] for o in stacks[0].get('Outputs', [])}


def _note_failure(table, session_id, timestamp, reason):
    """
    Remember that this lab never came up, where nothing will overwrite it.

    stack_status is one slot and the teardown writes over it, so by the time
    anyone reads the session it says CLEANUP_COMPLETE and there is no trace
    that the stack rolled back. On 03/11/2026 a player started game19 eight
    times against a stack that could not create, finished no task, and every
    report called it a session he did not finish — the same thing it calls
    someone who walked away.

    Written with if_not_exists so the first failure wins: the stack-level
    ROLLBACK_IN_PROGRESS carries the reason ("The following resource(s)
    failed to create: [...]") and the ROLLBACK_COMPLETE behind it usually
    carries none. Unconditional on purpose, unlike the status write — "did
    creation ever fail" does not depend on the order SNS delivers in, and a
    reason that arrives late still fills a slot left empty.
    """
    update = 'SET create_failed_at = if_not_exists(create_failed_at, :ts)'
    values = {':ts': timestamp}

    if reason:
        update += ', create_failure_reason = if_not_exists(create_failure_reason, :why)'
        values[':why'] = reason[:1000]

    try:
        table.update_item(
            Key={'session_id': session_id},
            UpdateExpression=update,
            ConditionExpression='attribute_exists(session_id)',
            ExpressionAttributeValues=values,
        )
        print(f"Session {session_id} failed to build: {reason or 'no reason given'}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def _record(table, session_id, status, timestamp, outputs=None):
    """
    Write the new status, unless a later event has already been written.

    SNS does not promise order. Without the condition, a CREATE_IN_PROGRESS
    delivered late would overwrite the CREATE_COMPLETE that followed it and
    leave the player watching a spinner over a finished lab.
    """
    update = 'SET stack_status = :status, stack_status_at = :ts'
    values = {':status': status, ':ts': timestamp}

    if outputs is not None:
        update += ', stack_outputs = :outputs'
        values[':outputs'] = outputs

    try:
        table.update_item(
            Key={'session_id': session_id},
            UpdateExpression=update,
            ConditionExpression=(
                'attribute_exists(session_id) AND '
                '(attribute_not_exists(stack_status_at) OR stack_status_at < :ts)'
            ),
            ExpressionAttributeValues=values,
        )
        print(f"Session {session_id} is {status}")
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            # Either the session is gone, or we already know something newer.
            print(f"Ignored {status} for {session_id}: nothing newer to say")
            return False
        raise


def lambda_handler(event, context):
    table = boto3.resource('dynamodb').Table(os.environ['GAME_SESSIONS_TABLE'])
    handled = 0

    for record in event.get('Records', []):
        fields = _fields(record['Sns']['Message'])
        session_id = _session_id(fields)
        if not session_id:
            continue

        status = fields.get('ResourceStatus')
        timestamp = fields.get('Timestamp')
        if not status or not timestamp:
            continue

        if status in CREATION_FAILED:
            _note_failure(
                table, session_id, timestamp, fields.get('ResourceStatusReason')
            )

        outputs = None
        if status in COMPLETE:
            outputs = _outputs(fields.get('Namespace'), fields['StackName'])

        if _record(table, session_id, status, timestamp, outputs):
            handled += 1

    return {'handled': handled}
