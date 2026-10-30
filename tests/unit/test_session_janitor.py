"""
The sweep that frees accounts held by sessions nobody ended.

With three accounts in the pool, one player closing their tab is a third of
the capacity gone until someone notices, which is what this exists to stop.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ACCOUNT_A, ACCOUNT_B, load_handler


@pytest.fixture
def janitor(sessions_table, accounts_table, cleanup_project, monkeypatch):
    """The handler reads MAX_SESSION_HOURS at import, so it is loaded per test."""
    monkeypatch.setenv('MAX_SESSION_HOURS', '3')
    return load_handler('session_janitor')


def hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')


def a_session(sessions_table, session_id, age_hours=1, active=True,
              account_id=ACCOUNT_A, start_time=None):
    item = {
        'session_id': session_id,
        'user_name': 'player@example.com',
        'game_id': 'game-1',
        'active': active,
        'stack_status': 'CREATE_COMPLETE',
    }
    if start_time is not None:
        item['start_time'] = start_time
    elif age_hours is not None:
        item['start_time'] = hours_ago(age_hours)
    if account_id:
        item['account_id'] = account_id
    sessions_table.put_item(Item=item)
    return item


def stored(sessions_table, session_id):
    return sessions_table.get_item(Key={'session_id': session_id})['Item']


def status_of(accounts_table, account_id):
    return accounts_table.get_item(Key={'account_id': account_id})['Item']['status']


def test_a_session_past_the_cap_is_torn_down(sessions_table, accounts_table, janitor):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    a_session(sessions_table, 'old', age_hours=5)

    result = janitor.lambda_handler({}, None)

    assert result['checked'] == 1
    assert len(result['expired']) == 1
    assert stored(sessions_table, 'old')['active'] is False
    assert 'janitor' in stored(sessions_table, 'old')['ended_reason']
    assert status_of(accounts_table, ACCOUNT_A) == 'CLEANING'


def test_a_session_inside_the_cap_is_left_running(sessions_table, accounts_table, janitor):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    a_session(sessions_table, 'young', age_hours=1)

    result = janitor.lambda_handler({}, None)

    assert result['expired'] == []
    assert stored(sessions_table, 'young')['active'] is True
    assert status_of(accounts_table, ACCOUNT_A) == 'IN_USE'


def test_an_already_closed_session_is_never_touched(sessions_table, accounts_table, janitor):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'AVAILABLE'})
    a_session(sessions_table, 'done', age_hours=99, active=False)

    result = janitor.lambda_handler({}, None)

    assert result['checked'] == 0
    assert status_of(accounts_table, ACCOUNT_A) == 'AVAILABLE'


@pytest.mark.parametrize('start_time', [None, '', 'not a timestamp'])
def test_a_session_whose_age_cannot_be_read_is_left_alone(
    sessions_table, accounts_table, janitor, start_time
):
    # Better a session that outstays its welcome than one torn down under a
    # player because a field was unreadable.
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    a_session(sessions_table, 'odd', age_hours=None, start_time=start_time)

    result = janitor.lambda_handler({}, None)

    assert result['expired'] == []
    assert stored(sessions_table, 'odd')['active'] is True


def test_only_the_old_ones_go(sessions_table, accounts_table, janitor):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    accounts_table.put_item(Item={'account_id': ACCOUNT_B, 'status': 'IN_USE'})
    a_session(sessions_table, 'old', age_hours=6, account_id=ACCOUNT_A)
    a_session(sessions_table, 'young', age_hours=2, account_id=ACCOUNT_B)

    result = janitor.lambda_handler({}, None)

    assert result['checked'] == 2
    assert [e['session_id'] for e in result['expired']] == ['old']
    assert status_of(accounts_table, ACCOUNT_A) == 'CLEANING'
    assert status_of(accounts_table, ACCOUNT_B) == 'IN_USE'


def test_the_cap_is_configurable(sessions_table, accounts_table, cleanup_project, monkeypatch):
    monkeypatch.setenv('MAX_SESSION_HOURS', '1')
    handler = load_handler('session_janitor')
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    a_session(sessions_table, 'two-hours', age_hours=2)

    assert len(handler.lambda_handler({}, None)['expired']) == 1


def test_one_broken_session_does_not_stop_the_sweep(sessions_table, accounts_table, janitor):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})
    accounts_table.put_item(Item={'account_id': ACCOUNT_B, 'status': 'IN_USE'})
    a_session(sessions_table, 'boom', age_hours=6, account_id=ACCOUNT_A)
    a_session(sessions_table, 'fine', age_hours=6, account_id=ACCOUNT_B)

    real_end_session = janitor.session_teardown.end_session

    def explode_on_the_first(table, session, manager, reason=None):
        if session['session_id'] == 'boom':
            raise RuntimeError('something went wrong reading this one')
        return real_end_session(table, session, manager, reason=reason)

    janitor.session_teardown.end_session = explode_on_the_first
    try:
        result = janitor.lambda_handler({}, None)
    finally:
        janitor.session_teardown.end_session = real_end_session

    # The point of the sweep is the accounts the *other* sessions are holding.
    assert [e['session_id'] for e in result['expired']] == ['fine']
    assert status_of(accounts_table, ACCOUNT_B) == 'CLEANING'


# --- retrying a wipe that failed --------------------------------------------
#
# The waiting happens here rather than inside the build: an ENI left by a
# Lambda in a VPC takes tens of minutes to be released, and a CodeBuild
# container sleeping through that is billed by the minute.

def _long_ago(minutes):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)) \
        .isoformat().replace('+00:00', 'Z')


def a_failed_cleanup(accounts_table, account_id=ACCOUNT_A, attempts=1, minutes_ago=30):
    accounts_table.put_item(Item={
        'account_id': account_id,
        'status': 'CLEANING',
        'cleanup_attempts': attempts,
        'cleanup_failed_at': _long_ago(minutes_ago),
        'failure_reason': 'cleanup build FAILED: some-build',
    })


def test_a_failed_cleanup_is_tried_again(janitor, accounts_table):
    a_failed_cleanup(accounts_table)

    result = janitor.lambda_handler({}, None)

    assert result['retried_cleanups'] == [ACCOUNT_A]
    item = accounts_table.get_item(Key={'account_id': ACCOUNT_A})['Item']
    # The marker is cleared so the next failure writes a fresh one; the
    # count stays, because that is what decides when to give up.
    assert 'cleanup_failed_at' not in item
    assert item['cleanup_attempts'] == 1


def test_a_failure_too_recent_is_left_alone(janitor, accounts_table):
    a_failed_cleanup(accounts_table, minutes_ago=2)

    result = janitor.lambda_handler({}, None)

    # Retrying immediately just burns another build against an ENI that AWS
    # has not released yet.
    assert result['retried_cleanups'] == []
    assert accounts_table.get_item(
        Key={'account_id': ACCOUNT_A})['Item']['cleanup_failed_at']


def test_enough_failures_quarantine_it(janitor, accounts_table, verified_sender):
    a_failed_cleanup(accounts_table, attempts=3)

    result = janitor.lambda_handler({}, None)

    assert result['quarantined'] == [ACCOUNT_A]
    assert accounts_table.get_item(
        Key={'account_id': ACCOUNT_A})['Item']['status'] == 'QUARANTINED'


def test_an_account_merely_cleaning_is_not_disturbed(janitor, accounts_table):
    # No cleanup_failed_at: a build is running right now, and starting a
    # second one on the same account would be the worst thing here.
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'CLEANING'})

    assert janitor.lambda_handler({}, None)['retried_cleanups'] == []


def test_a_healthy_pool_is_untouched(janitor, accounts_table):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'AVAILABLE'})
    accounts_table.put_item(Item={'account_id': ACCOUNT_B, 'status': 'IN_USE'})

    result = janitor.lambda_handler({}, None)

    assert result['retried_cleanups'] == [] and result['quarantined'] == []
