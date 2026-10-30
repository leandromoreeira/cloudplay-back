"""
Turning a finished cleanup build into account state.

The build never writes state itself, so this is the only thing standing
between a build that times out and an account stuck in CLEANING forever.
"""

import pytest

from tests.conftest import ACCOUNT_A, load_handler, sent_emails


@pytest.fixture
def handler(accounts_table, sessions_table):
    return load_handler('cleanup_result')


BUILD_ID = 'aws-jam-account-cleanup:abc-123'
BUILD_ARN = f'arn:aws:codebuild:us-east-1:673000199571:build/{BUILD_ID}'


def build_event(status, account=ACCOUNT_A, with_target=True):
    """The shape EventBridge delivers for CodeBuild Build State Change."""
    variables = [{'name': 'GAME_ACCOUNTS_TABLE', 'value': 'test', 'type': 'PLAINTEXT'}]
    if with_target:
        variables.append({'name': 'TARGET_ACCOUNT', 'value': account, 'type': 'PLAINTEXT'})

    return {
        'source': 'aws.codebuild',
        'detail-type': 'CodeBuild Build State Change',
        'detail': {
            'build-status': status,
            'project-name': 'aws-jam-account-cleanup',
            'build-id': BUILD_ARN,
            'additional-information': {
                'environment': {'environment-variables': variables},
            },
        },
    }


def given(accounts_table, status='CLEANING'):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': status})


def status_of(accounts_table):
    return accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']['status']


def a_session(sessions_table, session_id='s-1', account_id=ACCOUNT_A, build_id=BUILD_ID):
    item = {
        'session_id': session_id,
        'user_name': 'player@example.com',
        'game_id': 'game1',
        'account_id': account_id,
        'active': False,
        'stack_status': 'CLEANUP_IN_PROGRESS',
    }
    if build_id:
        item['cleanup_build_id'] = build_id
    sessions_table.put_item(Item=item)
    return item


def session_status(sessions_table, session_id='s-1'):
    return sessions_table.get_item(Key={'session_id': session_id})['Item']['stack_status']


def test_a_wiped_account_goes_back_into_the_pool(accounts_table, handler):
    given(accounts_table)

    handler.lambda_handler(build_event('SUCCEEDED'), None)

    assert status_of(accounts_table) == 'AVAILABLE'


@pytest.mark.parametrize('status', ['FAILED', 'STOPPED', 'TIMED_OUT'])
def test_an_unhappy_ending_is_noted_and_left_for_another_go(
    accounts_table, handler, status
):
    given(accounts_table)

    handler.lambda_handler(build_event(status), None)

    item = accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']
    # Still CLEANING, which already keeps it out of the pool, and marked so
    # the janitor comes back for it. Most failures are AWS not having let
    # go of something yet, and that only needs time.
    assert item['status'] == 'CLEANING'
    assert item['cleanup_attempts'] == 1
    assert item['cleanup_failed_at']


def test_the_reason_says_which_build_died(accounts_table, handler):
    given(accounts_table)

    handler.lambda_handler(build_event('TIMED_OUT'), None)

    reason = accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']['failure_reason']
    assert 'TIMED_OUT' in reason


def test_the_last_attempt_is_the_one_that_quarantines(accounts_table, handler):
    given(accounts_table)
    accounts_table.update_item(
        Key={'account_id': ACCOUNT_A},
        UpdateExpression='SET cleanup_attempts = :n',
        ExpressionAttributeValues={':n': 2},
    )

    handler.lambda_handler(build_event('FAILED'), None)

    # Three failures is no longer slowness, it is something stuck, and that
    # needs a person.
    item = accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']
    assert item['status'] == 'QUARANTINED'
    assert '3 attempts' in item['failure_reason']


def test_a_wipe_that_took_two_goes_leaves_no_count_behind(accounts_table, handler):
    given(accounts_table)
    handler.lambda_handler(build_event('FAILED'), None)
    handler.lambda_handler(build_event('SUCCEEDED'), None)

    item = accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']
    # The count must not follow the account into the pool and shorten the
    # next session's patience.
    assert item['status'] == 'AVAILABLE'
    assert 'cleanup_attempts' not in item
    assert 'cleanup_failed_at' not in item


def test_a_released_account_releases_its_session_too(accounts_table, sessions_table, handler):
    given(accounts_table)
    a_session(sessions_table)

    handler.lambda_handler(build_event('SUCCEEDED'), None)

    # Without this the session reads CLEANUP_IN_PROGRESS forever and there is
    # no way to tell from the table whether the wipe ever finished.
    assert session_status(sessions_table) == 'CLEANUP_COMPLETE'


@pytest.mark.parametrize('status', ['FAILED', 'STOPPED', 'TIMED_OUT'])
def test_a_failed_wipe_says_so_on_the_session(accounts_table, sessions_table, handler, status):
    given(accounts_table)
    a_session(sessions_table)

    handler.lambda_handler(build_event(status), None)

    assert session_status(sessions_table) == 'CLEANUP_FAILED'


def test_earlier_sessions_in_the_same_account_are_left_alone(
    accounts_table, sessions_table, handler
):
    given(accounts_table)
    a_session(sessions_table, 'older', build_id='aws-jam-account-cleanup:a-previous-run')
    a_session(sessions_table, 'current')

    handler.lambda_handler(build_event('SUCCEEDED'), None)

    # An account has run many sessions; only the one this build was cleaning
    # up after is finished.
    assert session_status(sessions_table, 'current') == 'CLEANUP_COMPLETE'
    assert session_status(sessions_table, 'older') == 'CLEANUP_IN_PROGRESS'


def test_a_build_with_no_session_behind_it_still_frees_the_account(
    accounts_table, sessions_table, handler
):
    # Someone started the build by hand, or the session row is long gone.
    given(accounts_table)

    handler.lambda_handler(build_event('SUCCEEDED'), None)

    assert status_of(accounts_table) == 'AVAILABLE'


def test_a_retryable_failure_does_not_wake_anyone(accounts_table, handler, verified_sender):
    given(accounts_table)

    handler.lambda_handler(build_event('FAILED'), None)

    # An email per slow ENI would be noise, and noise is what gets ignored
    # on the day it matters.
    assert sent_emails() == []


def test_giving_up_reaches_a_human(accounts_table, handler, verified_sender):
    given(accounts_table)
    accounts_table.update_item(
        Key={'account_id': ACCOUNT_A},
        UpdateExpression='SET cleanup_attempts = :n',
        ExpressionAttributeValues={':n': 2},
    )

    handler.lambda_handler(build_event('FAILED'), None)

    assert len(sent_emails()) == 1
    assert ACCOUNT_A in sent_emails()[0].subject


def test_a_build_with_no_target_touches_nothing(accounts_table, handler):
    given(accounts_table)

    handler.lambda_handler(build_event('SUCCEEDED', with_target=False), None)

    assert status_of(accounts_table) == 'CLEANING'


def test_the_projects_placeholder_is_not_an_account(accounts_table, handler):
    # 'unset' is what the project definition carries when nobody overrode it,
    # i.e. somebody started the build by hand from the console.
    given(accounts_table)

    handler.lambda_handler(build_event('SUCCEEDED', account='unset'), None)

    assert status_of(accounts_table) == 'CLEANING'
