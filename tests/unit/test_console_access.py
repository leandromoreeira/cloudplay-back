"""
Brokering a console session.

The federation dance has two traps worth pinning down: the session name is
what CloudTrail shows in the game account, and SessionDuration must never be
sent — these credentials come from role chaining, and the endpoint rejects it.
"""

import json
import urllib.parse
from io import BytesIO

import pytest


@pytest.fixture
def console():
    import console_access
    return console_access


@pytest.fixture
def credentials():
    return {
        'AccessKeyId': 'ASIAEXAMPLE',
        'SecretAccessKey': 'secret',
        'SessionToken': 'token',
    }


# --- what CloudTrail will show ----------------------------------------------

def test_an_ordinary_address_is_the_session_name(console):
    assert console.session_name_for('player@example.com') == 'player@example.com'


def test_characters_sts_will_not_take_are_replaced(console):
    assert console.session_name_for('a b/c@example.com') == 'a-b-c@example.com'


def test_a_long_address_is_truncated_but_stays_unique(console):
    local = 'a' * 70
    first = console.session_name_for(f'{local}1@example.com')
    second = console.session_name_for(f'{local}2@example.com')

    assert len(first) <= console.SESSION_NAME_MAX
    assert len(second) <= console.SESSION_NAME_MAX
    # Two addresses sharing a 64 character prefix must not collide.
    assert first != second


# --- assuming the player's role ---------------------------------------------

class FakeSts:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {'Credentials': {'AccessKeyId': 'A', 'SecretAccessKey': 'S', 'SessionToken': 'T'}}


def test_the_player_is_assumed_into_their_own_role(console):
    import team_role

    sts = FakeSts()
    console.player_credentials('111111111111', 'player@example.com', sts_client=sts)

    assert sts.calls[0]['RoleArn'] == f'arn:aws:iam::111111111111:role/{team_role.ROLE_NAME}'
    assert sts.calls[0]['RoleSessionName'] == 'player@example.com'


def test_no_duration_is_requested(console):
    """
    Role chaining caps the session at an hour, and asking for a duration at
    all makes the federation call fail afterwards.
    """
    sts = FakeSts()
    console.player_credentials('111111111111', 'player@example.com', sts_client=sts)

    assert 'DurationSeconds' not in sts.calls[0]


# --- the sign-in URL --------------------------------------------------------

@pytest.fixture
def federation(console, monkeypatch):
    """Stands in for signin.aws.amazon.com, recording what it was asked."""
    requested = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({'SigninToken': 'the-token'}).encode()

    def fake_urlopen(url, timeout=None):
        requested.append(url)
        return Response()

    monkeypatch.setattr(console.urllib.request, 'urlopen', fake_urlopen)
    return requested


def parsed(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def test_the_credentials_are_traded_for_a_signin_token(console, credentials, federation):
    console.signin_url(credentials, issuer='https://example.com/reconnect?game=game1')

    query = parsed(federation[0])
    assert query['Action'] == ['getSigninToken']
    assert json.loads(query['Session'][0]) == {
        'sessionId': 'ASIAEXAMPLE',
        'sessionKey': 'secret',
        'sessionToken': 'token',
    }


def test_the_token_request_never_asks_for_a_session_duration(console, credentials, federation):
    # "Do not use the SessionDuration HTTP parameter when you get temporary
    # credentials through role chaining. The operation will fail."
    console.signin_url(credentials, issuer='https://example.com/reconnect')

    assert 'SessionDuration' not in parsed(federation[0])


def test_the_url_signs_the_player_into_the_console(console, credentials, federation):
    url = console.signin_url(credentials, issuer='https://example.com/reconnect?game=game1')

    query = parsed(url)
    assert query['Action'] == ['login']
    assert query['SigninToken'] == ['the-token']
    assert query['Destination'] == [console.CONSOLE_HOME]
    # Where the console sends them when the hour is up.
    assert query['Issuer'] == ['https://example.com/reconnect?game=game1']


def test_the_console_lands_in_us_east_1(console):
    assert 'us-east-1' in console.CONSOLE_HOME
