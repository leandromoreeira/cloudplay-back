"""
Games are private by default, so these cover the two ways in: the
full-access group, and an explicit per-game grant.
"""

import pytest

from tests.conftest import load_handler


@pytest.fixture
def access():
    import access_control
    return access_control


def event_with(claims):
    return {'requestContext': {'authorizer': {'claims': claims}}}


# --- reading the identity off the request -----------------------------------

def test_groups_arrive_as_a_bracketed_string(access):
    email, groups = access.get_identity(
        event_with({'email': 'a@example.com', 'cognito:groups': '[full-access,admins]'})
    )

    assert email == 'a@example.com'
    assert groups == ['full-access', 'admins']


def test_groups_can_also_arrive_as_a_list(access):
    _, groups = access.get_identity(
        event_with({'email': 'a@example.com', 'cognito:groups': ['full-access']})
    )

    assert groups == ['full-access']


def test_a_user_in_no_group_has_no_groups(access):
    email, groups = access.get_identity(event_with({'email': 'a@example.com'}))

    assert email == 'a@example.com'
    assert groups == []


@pytest.mark.parametrize('event', [{}, {'requestContext': {}}, {'requestContext': {'authorizer': None}}])
def test_a_request_without_claims_has_no_identity(access, event):
    assert access.get_identity(event) == (None, [])


# --- who may play what ------------------------------------------------------

def test_full_access_opens_every_game(access, access_table):
    assert access.can_access_game('a@example.com', ['full-access'], 'any-game') is True


def test_a_granted_game_is_playable(access, access_table):
    access_table.put_item(Item={'user_email': 'a@example.com', 'game_id': 'game-1'})

    assert access.can_access_game('a@example.com', [], 'game-1') is True


def test_a_game_without_a_grant_is_not(access, access_table):
    access_table.put_item(Item={'user_email': 'a@example.com', 'game_id': 'game-1'})

    assert access.can_access_game('a@example.com', [], 'game-2') is False


def test_another_users_grant_does_not_carry_over(access, access_table):
    access_table.put_item(Item={'user_email': 'someone@example.com', 'game_id': 'game-1'})

    assert access.can_access_game('a@example.com', [], 'game-1') is False


def test_no_email_means_no_access(access, access_table):
    assert access.can_access_game(None, [], 'game-1') is False


# --- what the listing should show -------------------------------------------

def test_full_access_means_no_filtering_at_all(access, access_table):
    # None, not the set of every id: the caller takes it as "show everything".
    assert access.get_accessible_game_ids('a@example.com', ['full-access']) is None


def test_a_restricted_user_sees_only_their_grants(access, access_table):
    access_table.put_item(Item={'user_email': 'a@example.com', 'game_id': 'game-1'})
    access_table.put_item(Item={'user_email': 'a@example.com', 'game_id': 'game-2'})
    access_table.put_item(Item={'user_email': 'other@example.com', 'game_id': 'game-3'})

    assert access.get_accessible_game_ids('a@example.com', []) == {'game-1', 'game-2'}


def test_a_user_with_no_grants_sees_nothing(access, access_table):
    assert access.get_accessible_game_ids('a@example.com', []) == set()


def test_an_anonymous_caller_sees_nothing(access, access_table):
    assert access.get_accessible_game_ids(None, []) == set()
