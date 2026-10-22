"""
Turning a finished cleanup build into account state.

The build never writes state itself, so this is the only thing standing
between a build that times out and an account stuck in CLEANING forever.
"""

import pytest

from tests.conftest import ACCOUNT_A, load_handler, sent_emails


@pytest.fixture
def handler(accounts_table):
    return load_handler('cleanup_result')


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
            'build-id': f'arn:aws:codebuild:us-east-1:673000199571:build/cleanup:{status}',
            'additional-information': {
                'environment': {'environment-variables': variables},
            },
        },
    }


def given(accounts_table, status='CLEANING'):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': status})


def status_of(accounts_table):
    return accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']['status']


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
