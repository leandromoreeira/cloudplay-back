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
def test_every_unhappy_ending_quarantines(accounts_table, handler, status):
    given(accounts_table)

    handler.lambda_handler(build_event(status), None)

    assert status_of(accounts_table) == 'QUARANTINED'


def test_the_quarantine_says_which_build_died(accounts_table, handler):
    given(accounts_table)

    handler.lambda_handler(build_event('TIMED_OUT'), None)

    reason = accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']['failure_reason']
    assert 'TIMED_OUT' in reason


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


def test_a_quarantine_reaches_a_human(accounts_table, handler, verified_sender):
    given(accounts_table)

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
