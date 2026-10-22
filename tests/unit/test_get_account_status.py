"""
The pool report. Worth its own tests mainly because this handler used to
decode the bearer token by hand instead of trusting the authorizer.
"""

import json

import pytest

from tests.conftest import ACCOUNT_A, ACCOUNT_B, load_handler


@pytest.fixture
def handler(accounts_table):
    return load_handler('get_account_status')


def request_from(email):
    claims = {'email': email} if email else {}
    return {
        'headers': {},
        'requestContext': {'authorizer': {'claims': claims}},
    }


def body_of(response):
    return json.loads(response['body'])


def test_a_caller_without_claims_gets_401(accounts_table, handler):
    response = handler.lambda_handler(request_from(None), None)

    assert response['statusCode'] == 401


def test_the_report_counts_every_state(accounts_table, handler):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'AVAILABLE'})
    accounts_table.put_item(Item={'account_id': ACCOUNT_B, 'status': 'CLEANING'})
    accounts_table.put_item(Item={'account_id': '333333333333', 'status': 'QUARANTINED'})
    accounts_table.put_item(Item={'account_id': '444444444444', 'status': 'IN_USE'})

    summary = body_of(handler.lambda_handler(request_from('a@example.com'), None))['summary']

    assert summary['total_accounts'] == 4
    assert summary['available_accounts'] == 1
    assert summary['busy_accounts'] == 3
    assert summary['cleaning_accounts'] == 1
    assert summary['quarantined_accounts'] == 1
    assert summary['all_busy'] is False


def test_a_pool_with_nothing_free_says_so(accounts_table, handler):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})

    summary = body_of(handler.lambda_handler(request_from('a@example.com'), None))['summary']

    assert summary['all_busy'] is True


def test_a_quarantined_account_reports_why(accounts_table, handler):
    accounts_table.put_item(
        Item={'account_id': ACCOUNT_A, 'status': 'QUARANTINED', 'failure_reason': 'nuke TIMED_OUT'}
    )

    accounts = body_of(handler.lambda_handler(request_from('a@example.com'), None))['accounts']

    assert accounts[0]['failure_reason'] == 'nuke TIMED_OUT'
