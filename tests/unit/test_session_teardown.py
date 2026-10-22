"""
The shared path from "session is over" to "account is being wiped".

Both the player's end button and the janitor go through here, so these are the
tests that say an abandoned session is cleaned up exactly like a finished one.
"""

import pytest

from tests.conftest import ACCOUNT_A, sent_emails


@pytest.fixture
def manager(accounts_table):
    from account_manager import get_account_manager
    return get_account_manager()


@pytest.fixture
def teardown():
    import session_teardown
    return session_teardown


def a_session(sessions_table, session_id='s-1', account_id=ACCOUNT_A):
    item = {
        'session_id': session_id,
        'user_name': 'player@example.com',
        'game_id': 'game-1',
        'active': True,
        'start_time': '2026-10-21T10:00:00Z',
        'stack_status': 'CREATE_COMPLETE',
    }
    if account_id:
        item['account_id'] = account_id
    sessions_table.put_item(Item=item)
    return item


def stored(sessions_table, session_id='s-1'):
    return sessions_table.get_item(Key={'session_id': session_id})['Item']


def account(accounts_table, account_id=ACCOUNT_A):
    return accounts_table.get_item(Key={'account_id': account_id})['Item']


def test_happy_path_closes_the_session_and_starts_the_build(
    sessions_table, accounts_table, manager, teardown, cleanup_project
):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    session = a_session(sessions_table)

    result = teardown.end_session(sessions_table, session, manager)

    assert result['outcome'] == teardown.STARTED
    assert result['account_id'] == ACCOUNT_A
    assert result['build_id']

    closed = stored(sessions_table)
    assert closed['active'] is False
    assert closed['stack_status'] == 'CLEANUP_IN_PROGRESS'
    assert closed['end_time']
    assert closed['cleanup_build_id'] == result['build_id']

    # Held, not released: the wipe has not happened yet.
    assert account(accounts_table)['status'] == 'CLEANING'


def test_the_build_is_told_which_account_to_wipe(
    sessions_table, accounts_table, manager, teardown, cleanup_project, monkeypatch
):
    """
    TARGET_ACCOUNT is the whole contract with the buildspec: it is both what
    the nuke is pointed at and what the build checks is CLEANING before it
    runs. moto drops environment overrides instead of storing them on the
    build, so this watches the call rather than reading it back.
    """
    import boto3

    calls = []
    real_client = boto3.client

    def spy(service_name, *args, **kwargs):
        client = real_client(service_name, *args, **kwargs)
        if service_name != 'codebuild':
            return client

        start_build = client.start_build

        def recording(**call):
            calls.append(call)
            return start_build(**call)

        client.start_build = recording
        return client

    monkeypatch.setattr(boto3, 'client', spy)

    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    teardown.end_session(sessions_table, a_session(sessions_table), manager)

    assert len(calls) == 1
    assert calls[0]['projectName'] == cleanup_project
    assert calls[0]['environmentVariablesOverride'] == [
        {'name': 'TARGET_ACCOUNT', 'value': ACCOUNT_A, 'type': 'PLAINTEXT'}
    ]


def test_a_reason_is_recorded_when_given(
    sessions_table, accounts_table, manager, teardown, cleanup_project
):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})

    teardown.end_session(sessions_table, a_session(sessions_table), manager, reason='expired')

    assert stored(sessions_table)['ended_reason'] == 'expired'


def test_no_reason_leaves_the_field_off(
    sessions_table, accounts_table, manager, teardown, cleanup_project
):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})

    teardown.end_session(sessions_table, a_session(sessions_table), manager)

    assert 'ended_reason' not in stored(sessions_table)


def test_a_session_with_no_account_just_closes(
    sessions_table, accounts_table, manager, teardown, cleanup_project
):
    session = a_session(sessions_table, account_id=None)

    result = teardown.end_session(sessions_table, session, manager)

    assert result['outcome'] == teardown.NO_ACCOUNT
    assert result['build_id'] is None
    assert stored(sessions_table)['active'] is False


@pytest.mark.parametrize('status', ['AVAILABLE', 'CLEANING', 'QUARANTINED'])
def test_an_account_that_is_not_in_use_is_left_alone(
    sessions_table, accounts_table, manager, teardown, cleanup_project, status
):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': status})
    session = a_session(sessions_table)

    result = teardown.end_session(sessions_table, session, manager)

    assert result['outcome'] == teardown.SKIPPED
    assert result['build_id'] is None
    # Someone else owns this account now; closing our session must not touch it.
    assert account(accounts_table)['status'] == status
    assert stored(sessions_table)['active'] is False


def test_an_account_whose_build_will_not_start_is_quarantined(
    sessions_table, accounts_table, manager, teardown, monkeypatch, verified_sender
):
    # No project of that name, which is what a deleted or renamed project
    # looks like from here.
    monkeypatch.setenv('CLEANUP_PROJECT_NAME', 'no-such-project')
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})

    result = teardown.end_session(sessions_table, a_session(sessions_table), manager)

    assert result['outcome'] == teardown.FAILED
    assert result['error']
    # Dirty and nobody coming to clean it: it must not go back into the pool.
    assert account(accounts_table)['status'] == 'QUARANTINED'
    assert stored(sessions_table)['active'] is False
    # ...and a human hears about it.
    assert len(sent_emails()) == 1
