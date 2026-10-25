"""
Stack events arriving on their own.

CloudFormation publishes to the topic start_game_session hands it, so these
cover what the handler keeps, what it throws away, and what it does when SNS
delivers out of order — which it is allowed to do.
"""

import pytest

from tests.conftest import ACCOUNT_A, load_handler

SESSION_ID = 'abc-123'
STACK = f'game-session-{SESSION_ID}'


class FakeCloudFormation:
    """Only ever asked for the outputs of a finished stack."""

    def __init__(self, outputs=None):
        self.describe_calls = 0
        self.outputs = outputs if outputs is not None else [
            {'OutputKey': 'BucketName', 'OutputValue': 'lab-bucket'},
        ]

    def describe_stacks(self, StackName):
        self.describe_calls += 1
        return {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Outputs': self.outputs}]}


@pytest.fixture
def cloudformation():
    return FakeCloudFormation()


@pytest.fixture
def handler(sessions_table, accounts_table, cloudformation, monkeypatch):
    import account_manager

    monkeypatch.setattr(
        account_manager.AccountManager, 'create_cross_account_clients',
        lambda self, account_id: cloudformation
    )
    return load_handler('stack_events')


def given_session(sessions_table, status='CREATE_IN_PROGRESS', status_at=None):
    item = {
        'session_id': SESSION_ID,
        'user_name': 'player@example.com',
        'game_id': 'game1',
        'account_id': ACCOUNT_A,
        'active': True,
        'start_time': '2026-10-25T10:00:00Z',
        'stack_status': status,
        'stack_outputs': {},
    }
    if status_at:
        item['stack_status_at'] = status_at
    sessions_table.put_item(Item=item)


def notification(**overrides):
    fields = {
        'StackId': f'arn:aws:cloudformation:us-east-1:{ACCOUNT_A}:stack/{STACK}/x',
        'Timestamp': '2026-10-25T10:05:00.000Z',
        'LogicalResourceId': STACK,
        'Namespace': ACCOUNT_A,
        'ResourceStatus': 'CREATE_COMPLETE',
        'ResourceType': 'AWS::CloudFormation::Stack',
        'StackName': STACK,
    }
    fields.update(overrides)
    body = '\n'.join(f"{k}='{v}'" for k, v in fields.items())
    return {'Records': [{'Sns': {'Message': body}}]}


def stored(sessions_table):
    return sessions_table.get_item(Key={'session_id': SESSION_ID})['Item']


# --- what it acts on --------------------------------------------------------

def test_a_finished_stack_updates_the_session_and_its_outputs(
    handler, sessions_table, cloudformation
):
    given_session(sessions_table)

    assert handler.lambda_handler(notification(), None) == {'handled': 1}

    session = stored(sessions_table)
    assert session['stack_status'] == 'CREATE_COMPLETE'
    assert session['stack_outputs'] == {'BucketName': 'lab-bucket'}
    # The notification carries the status but not the outputs, so exactly one
    # cross-account call per session, at the moment it completes.
    assert cloudformation.describe_calls == 1


def test_a_stack_still_building_costs_no_cross_account_call(
    handler, sessions_table, cloudformation
):
    given_session(sessions_table, status='STARTING')

    handler.lambda_handler(notification(ResourceStatus='CREATE_IN_PROGRESS'), None)

    assert stored(sessions_table)['stack_status'] == 'CREATE_IN_PROGRESS'
    assert cloudformation.describe_calls == 0


def test_a_failed_stack_is_recorded_too(handler, sessions_table):
    given_session(sessions_table)

    handler.lambda_handler(notification(ResourceStatus='ROLLBACK_IN_PROGRESS'), None)

    assert stored(sessions_table)['stack_status'] == 'ROLLBACK_IN_PROGRESS'


# --- what it throws away ----------------------------------------------------

def test_resource_level_events_are_ignored(handler, sessions_table):
    given_session(sessions_table)

    result = handler.lambda_handler(notification(
        ResourceType='AWS::S3::Bucket', LogicalResourceId='LabBucket'
    ), None)

    # Most of the traffic. They say nothing about the stack as a whole.
    assert result == {'handled': 0}
    assert stored(sessions_table)['stack_status'] == 'CREATE_IN_PROGRESS'


def test_a_nested_stack_is_not_mistaken_for_the_lab(handler, sessions_table):
    given_session(sessions_table)

    # A game that nests its validation lambdas: still a stack resource, but
    # its logical id is not the stack's own name.
    result = handler.lambda_handler(notification(
        LogicalResourceId='ValidationLambdas'
    ), None)

    assert result == {'handled': 0}
    assert stored(sessions_table)['stack_status'] == 'CREATE_IN_PROGRESS'


def test_a_stack_that_is_not_a_lab_is_ignored(handler, sessions_table):
    given_session(sessions_table)

    result = handler.lambda_handler(notification(
        StackName='some-other-stack', LogicalResourceId='some-other-stack'
    ), None)

    assert result == {'handled': 0}


def test_an_event_for_an_unknown_session_changes_nothing(handler, sessions_table):
    result = handler.lambda_handler(notification(), None)

    assert result == {'handled': 0}
    assert sessions_table.scan()['Items'] == []


# --- SNS does not promise order ---------------------------------------------

def test_a_late_event_cannot_undo_a_newer_one(handler, sessions_table):
    given_session(sessions_table, status='CREATE_COMPLETE',
                  status_at='2026-10-25T10:05:00.000Z')

    result = handler.lambda_handler(notification(
        ResourceStatus='CREATE_IN_PROGRESS', Timestamp='2026-10-25T10:01:00.000Z'
    ), None)

    # Delivered out of order, this would have put the player back in front of
    # a spinner over a lab that was already up.
    assert result == {'handled': 0}
    assert stored(sessions_table)['stack_status'] == 'CREATE_COMPLETE'


def test_events_in_order_are_applied_in_order(handler, sessions_table):
    given_session(sessions_table, status='STARTING')

    handler.lambda_handler(notification(
        ResourceStatus='CREATE_IN_PROGRESS', Timestamp='2026-10-25T10:01:00.000Z'
    ), None)
    handler.lambda_handler(notification(Timestamp='2026-10-25T10:05:00.000Z'), None)

    assert stored(sessions_table)['stack_status'] == 'CREATE_COMPLETE'


def test_several_events_in_one_delivery_are_all_read(handler, sessions_table):
    given_session(sessions_table, status='STARTING')
    batch = notification(ResourceStatus='CREATE_IN_PROGRESS',
                         Timestamp='2026-10-25T10:01:00.000Z')
    batch['Records'] += notification(Timestamp='2026-10-25T10:05:00.000Z')['Records']

    assert handler.lambda_handler(batch, None) == {'handled': 2}
    assert stored(sessions_table)['stack_status'] == 'CREATE_COMPLETE'
