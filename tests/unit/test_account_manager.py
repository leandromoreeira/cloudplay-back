"""The account pool's state machine, which is what keeps two players off one account."""

import pytest

from tests.conftest import ACCOUNT_A, ACCOUNT_B, ALERT_EMAIL, sent_emails


@pytest.fixture
def manager(accounts_table):
    from account_manager import get_account_manager
    return get_account_manager()


def put(table, account_id, status, **extra):
    table.put_item(Item={'account_id': account_id, 'status': status, **extra})


def item(table, account_id):
    return table.get_item(Key={'account_id': account_id})['Item']


def status_of(table, account_id):
    return item(table, account_id)['status']


# --- claiming ---------------------------------------------------------------

def test_claim_takes_an_available_account(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'AVAILABLE')

    assert manager.claim_account('session-1') == ACCOUNT_A

    claimed = item(accounts_table, ACCOUNT_A)
    assert claimed['status'] == 'IN_USE'
    assert claimed['session_id'] == 'session-1'


def test_claim_returns_none_when_the_pool_is_empty(manager):
    assert manager.claim_account('session-1') is None


@pytest.mark.parametrize('status', ['IN_USE', 'CLEANING', 'QUARANTINED'])
def test_claim_never_takes_an_account_that_is_not_available(accounts_table, manager, status):
    put(accounts_table, ACCOUNT_A, status)

    assert manager.claim_account('session-1') is None
    assert status_of(accounts_table, ACCOUNT_A) == status


def test_two_sessions_cannot_win_the_same_account(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'AVAILABLE')

    first = manager.claim_account('session-1')
    second = manager.claim_account('session-2')

    assert first == ACCOUNT_A
    assert second is None
    assert item(accounts_table, ACCOUNT_A)['session_id'] == 'session-1'


def test_claim_falls_through_to_the_next_free_account(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'AVAILABLE')
    put(accounts_table, ACCOUNT_B, 'AVAILABLE')

    claimed = {manager.claim_account('session-1'), manager.claim_account('session-2')}

    assert claimed == {ACCOUNT_A, ACCOUNT_B}
    assert manager.claim_account('session-3') is None


def test_claim_clears_a_stale_failure_reason(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'AVAILABLE', failure_reason='an old nuke blew up')

    manager.claim_account('session-1')

    assert 'failure_reason' not in item(accounts_table, ACCOUNT_A)


# --- transitions ------------------------------------------------------------

def test_cleaning_only_from_in_use(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'IN_USE')

    assert manager.mark_cleaning(ACCOUNT_A) is True
    assert status_of(accounts_table, ACCOUNT_A) == 'CLEANING'


@pytest.mark.parametrize('status', ['AVAILABLE', 'CLEANING', 'QUARANTINED'])
def test_cleaning_refused_from_anywhere_else(accounts_table, manager, status):
    put(accounts_table, ACCOUNT_A, status)

    assert manager.mark_cleaning(ACCOUNT_A) is False
    assert status_of(accounts_table, ACCOUNT_A) == status


@pytest.mark.parametrize('status', ['CLEANING', 'IN_USE'])
def test_available_from_cleaning_or_in_use(accounts_table, manager, status):
    put(accounts_table, ACCOUNT_A, status)

    assert manager.mark_available(ACCOUNT_A) is True
    assert status_of(accounts_table, ACCOUNT_A) == 'AVAILABLE'


def test_releasing_an_account_forgets_whose_it_was(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'CLEANING', session_id='session-1')

    manager.mark_available(ACCOUNT_A)

    # A free account carrying a session id reads as if something still held
    # it, which is the first thing anyone checks when the pool looks wrong.
    assert 'session_id' not in item(accounts_table, ACCOUNT_A)


def test_taking_an_account_for_teardown_keeps_whose_it_was(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'IN_USE', session_id='session-1')

    manager.mark_cleaning(ACCOUNT_A)

    # Still needed: this is what says which session the wipe belongs to.
    assert item(accounts_table, ACCOUNT_A)['session_id'] == 'session-1'


def test_a_quarantined_account_never_drifts_back_into_the_pool(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'QUARANTINED', failure_reason='nuke failed')

    assert manager.mark_available(ACCOUNT_A) is False
    assert status_of(accounts_table, ACCOUNT_A) == 'QUARANTINED'


@pytest.mark.parametrize('status', ['AVAILABLE', 'IN_USE', 'CLEANING', 'QUARANTINED'])
def test_quarantine_works_from_every_state(accounts_table, manager, status):
    put(accounts_table, ACCOUNT_A, status)

    assert manager.mark_quarantined(ACCOUNT_A, 'because') is True
    assert status_of(accounts_table, ACCOUNT_A) == 'QUARANTINED'
    assert item(accounts_table, ACCOUNT_A)['failure_reason'] == 'because'


# --- which role to assume ---------------------------------------------------

def test_role_name_comes_from_the_account_item(accounts_table, manager):
    put(accounts_table, ACCOUNT_A, 'AVAILABLE', role_name='SomeOtherRole')

    assert manager._role_name_for(ACCOUNT_A) == 'SomeOtherRole'


def test_role_name_falls_back_for_items_written_by_hand(accounts_table, manager):
    from account_manager import DEFAULT_ROLE_NAME
    put(accounts_table, ACCOUNT_A, 'AVAILABLE')

    assert manager._role_name_for(ACCOUNT_A) == DEFAULT_ROLE_NAME


# --- status report ----------------------------------------------------------

def test_status_report_is_sorted_and_flags_availability(accounts_table, manager):
    put(accounts_table, ACCOUNT_B, 'QUARANTINED', failure_reason='boom')
    put(accounts_table, ACCOUNT_A, 'AVAILABLE')

    report = manager.get_account_status()

    assert [a['account_id'] for a in report] == [ACCOUNT_A, ACCOUNT_B]
    assert report[0]['is_available'] is True
    assert report[1]['is_available'] is False
    assert report[1]['failure_reason'] == 'boom'


# --- the quarantine alert ---------------------------------------------------

def test_quarantine_emails_a_human(accounts_table, manager, verified_sender):
    put(accounts_table, ACCOUNT_A, 'IN_USE')

    manager.mark_quarantined(ACCOUNT_A, 'cleanup build TIMED_OUT')

    messages = sent_emails()
    assert len(messages) == 1
    assert ACCOUNT_A in messages[0].subject
    assert messages[0].source == ALERT_EMAIL
    assert messages[0].destinations['ToAddresses'] == [ALERT_EMAIL]


def test_quarantine_survives_ses_refusing_to_send(accounts_table, manager, monkeypatch):
    # No verified identity, which is what an un-clicked verification mail looks
    # like. The account still has to leave the pool.
    monkeypatch.setenv('ALERT_EMAIL', 'nobody-verified-this@example.com')
    put(accounts_table, ACCOUNT_A, 'IN_USE')

    assert manager.mark_quarantined(ACCOUNT_A, 'cleanup build FAILED') is True
    assert status_of(accounts_table, ACCOUNT_A) == 'QUARANTINED'
    assert sent_emails() == []


def test_no_alert_address_means_no_mail_and_no_error(accounts_table, manager, monkeypatch):
    monkeypatch.delenv('ALERT_EMAIL', raising=False)
    put(accounts_table, ACCOUNT_A, 'IN_USE')

    assert manager.mark_quarantined(ACCOUNT_A, 'cleanup build FAILED') is True
    assert sent_emails() == []
