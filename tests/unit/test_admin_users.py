"""
The admin area.

Half of these are about what an administrator may do; the other half are
about what nobody may do, including the two self-inflicted locks that would
leave the AWS CLI as the only way back in.
"""

import json

import pytest

from tests.conftest import ALLOWED_ORIGIN, given_user, load_handler

ADMIN = 'admin@example.com'
PLAYER = 'player@example.com'


@pytest.fixture
def handler(user_pool, access_table, games_table, progress_table):
    return load_handler('admin_users')


def a_request(route, method, email=None, body=None, groups='[admin]', caller=ADMIN):
    event = {
        'resource': route,
        'httpMethod': method,
        'pathParameters': {'email': email} if email else None,
        'body': json.dumps(body) if body is not None else None,
        'requestContext': {'authorizer': {'claims': {
            'email': caller, 'cognito:groups': groups,
        }}},
    }
    return event


def body_of(response):
    return json.loads(response['body'])


def groups_of(pool_id, email):
    import boto3
    return sorted(
        g['GroupName'] for g in boto3.client('cognito-idp').admin_list_groups_for_user(
            UserPoolId=pool_id, Username=email
        )['Groups']
    )


# --- the door ---------------------------------------------------------------

def test_a_request_without_an_identity_is_rejected(handler):
    response = handler.lambda_handler(
        {'resource': '/admin/users', 'httpMethod': 'GET', 'requestContext': {}}, None
    )

    assert response['statusCode'] == 401


def test_a_player_cannot_open_the_admin_area(handler, user_pool):
    given_user(user_pool, PLAYER)

    response = handler.lambda_handler(
        a_request('/admin/users', 'GET', groups='[]', caller=PLAYER), None
    )

    assert response['statusCode'] == 403


def test_full_access_is_not_admin(handler, user_pool):
    given_user(user_pool, PLAYER, groups=['full-access'])

    # Playing every game and administering the platform are separate
    # grants. Confusing them would hand the directory to every power user.
    response = handler.lambda_handler(
        a_request('/admin/users', 'GET', groups='[full-access]', caller=PLAYER), None
    )

    assert response['statusCode'] == 403


def test_a_refusal_still_carries_the_cors_header(handler):
    response = handler.lambda_handler(
        {'resource': '/admin/users', 'httpMethod': 'GET', 'requestContext': {}}, None
    )

    assert response['headers']['Access-Control-Allow-Origin'] == ALLOWED_ORIGIN


# --- listing ----------------------------------------------------------------

def test_the_listing_carries_each_users_groups(handler, user_pool):
    given_user(user_pool, ADMIN, groups=['admin', 'full-access'])
    given_user(user_pool, PLAYER)

    body = body_of(handler.lambda_handler(a_request('/admin/users', 'GET'), None))

    by_email = {u['email']: u for u in body['users']}
    assert by_email[ADMIN]['groups'] == ['admin', 'full-access']
    assert by_email[PLAYER]['groups'] == []
    assert by_email[PLAYER]['enabled'] is True


# --- creating ---------------------------------------------------------------

def test_a_new_user_can_be_created_straight_into_groups(handler, user_pool):
    response = handler.lambda_handler(a_request(
        '/admin/users', 'POST', body={'email': 'new@example.com',
                                      'groups': ['full-access']}
    ), None)

    assert response['statusCode'] == 201
    assert groups_of(user_pool, 'new@example.com') == ['full-access']


def test_an_address_that_is_not_one_is_refused(handler, user_pool):
    response = handler.lambda_handler(
        a_request('/admin/users', 'POST', body={'email': 'not-an-email'}), None
    )

    assert response['statusCode'] == 400


def test_creating_someone_twice_says_so(handler, user_pool):
    given_user(user_pool, PLAYER)

    response = handler.lambda_handler(
        a_request('/admin/users', 'POST', body={'email': PLAYER}), None
    )

    assert response['statusCode'] == 409


def test_an_invented_group_is_not_granted(handler, user_pool):
    handler.lambda_handler(a_request(
        '/admin/users', 'POST',
        body={'email': 'new@example.com', 'groups': ['admin', 'superuser']}
    ), None)

    # Only the two groups this app knows about are assignable here.
    assert groups_of(user_pool, 'new@example.com') == ['admin']


# --- changing someone else --------------------------------------------------

def test_a_user_can_be_disabled_and_enabled_again(handler, user_pool):
    given_user(user_pool, PLAYER)

    handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=PLAYER,
                  body={'enabled': False}), None)

    import boto3
    client = boto3.client('cognito-idp')
    assert client.admin_get_user(UserPoolId=user_pool, Username=PLAYER)['Enabled'] is False

    handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=PLAYER,
                  body={'enabled': True}), None)

    assert client.admin_get_user(UserPoolId=user_pool, Username=PLAYER)['Enabled'] is True


def test_groups_are_replaced_by_the_list_given(handler, user_pool):
    given_user(user_pool, PLAYER, groups=['full-access'])

    handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=PLAYER,
                  body={'groups': ['admin']}), None)

    assert groups_of(user_pool, PLAYER) == ['admin']


def test_an_unknown_user_is_a_404(handler, user_pool):
    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email='ghost@example.com',
                  body={'enabled': False}), None)

    assert response['statusCode'] == 404


# --- the two locks ----------------------------------------------------------

def test_an_admin_cannot_disable_themselves(handler, user_pool):
    given_user(user_pool, ADMIN, groups=['admin'])

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=ADMIN,
                  body={'enabled': False}), None)

    assert response['statusCode'] == 409

    import boto3
    assert boto3.client('cognito-idp').admin_get_user(
        UserPoolId=user_pool, Username=ADMIN)['Enabled'] is True


def test_an_admin_cannot_take_away_their_own_admin(handler, user_pool):
    given_user(user_pool, ADMIN, groups=['admin', 'full-access'])

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=ADMIN,
                  body={'groups': ['full-access']}), None)

    # The last administrator doing this locks everyone out of the admin
    # area, and the way back is the AWS CLI.
    assert response['statusCode'] == 409
    assert 'admin' in groups_of(user_pool, ADMIN)


def test_an_admin_may_still_change_their_other_groups(handler, user_pool):
    given_user(user_pool, ADMIN, groups=['admin'])

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=ADMIN,
                  body={'groups': ['admin', 'full-access']}), None)

    assert response['statusCode'] == 200
    assert groups_of(user_pool, ADMIN) == ['admin', 'full-access']


# --- the authenticator ------------------------------------------------------

def test_resetting_mfa_clears_the_enrolled_app(handler, user_pool):
    given_user(user_pool, PLAYER)

    response = handler.lambda_handler(
        a_request('/admin/users/{email}/reset-mfa', 'POST', email=PLAYER), None)

    assert response['statusCode'] == 200


# --- per-game grants --------------------------------------------------------

def test_grants_can_be_read_and_replaced(handler, user_pool, access_table):
    access_table.put_item(Item={'user_email': PLAYER, 'game_id': 'game1'})

    body = body_of(handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'GET', email=PLAYER), None))
    assert body['games'] == ['game1']

    handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'PUT', email=PLAYER,
                  body={'games': ['game2', 'game3']}), None)

    body = body_of(handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'GET', email=PLAYER), None))

    # Replaced, not merged: game1 is gone.
    assert body['games'] == ['game2', 'game3']


def test_an_empty_list_revokes_everything(handler, user_pool, access_table):
    access_table.put_item(Item={'user_email': PLAYER, 'game_id': 'game1'})

    handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'PUT', email=PLAYER,
                  body={'games': []}), None)

    body = body_of(handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'GET', email=PLAYER), None))
    assert body['games'] == []


# --- routing ----------------------------------------------------------------

def test_a_verb_the_area_does_not_have_is_refused(handler, user_pool):
    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'PUT', email=PLAYER), None)

    assert response['statusCode'] == 405


def test_a_body_that_is_not_json_is_refused(handler, user_pool):
    event = a_request('/admin/users', 'POST')
    event['body'] = 'not json'

    assert handler.lambda_handler(event, None)['statusCode'] == 400


# --- the catalogue the grants page needs ------------------------------------

def given_game(games_table, game_id, title='A game'):
    games_table.put_item(Item={
        'id': game_id, 'title': title, 'content': '<p>..</p>',
        'category': 'Compute', 'difficulty': 'Hard',
        'tasks': [{'id': 'task1', 'title': 'One', 'content': ''}],
    })


def test_the_catalogue_lists_every_game_in_order(handler, user_pool, games_table):
    for game_id in ('game10', 'game2', 'game1'):
        given_game(games_table, game_id)

    body = body_of(handler.lambda_handler(a_request('/admin/games', 'GET'), None))

    # Numeric order, like the player's catalogue: as text game10 would sit
    # between game1 and game2.
    assert [g['id'] for g in body['games']] == ['game1', 'game2', 'game10']


def test_the_catalogue_ignores_the_admins_own_access(handler, user_pool, games_table, access_table):
    given_game(games_table, 'game1')
    given_game(games_table, 'game2')

    # The caller is in admin and nothing else, with no grants of their own.
    # Granting games they cannot play themselves is the normal case.
    body = body_of(handler.lambda_handler(a_request('/admin/games', 'GET'), None))

    assert len(body['games']) == 2


def test_the_catalogue_carries_only_what_the_page_shows(handler, user_pool, games_table):
    given_game(games_table, 'game1')

    body = body_of(handler.lambda_handler(a_request('/admin/games', 'GET'), None))

    # Not the task list and not the HTML body: this page draws a row per
    # game, and the content of 160 games is a lot to send for nothing.
    assert set(body['games'][0]) == {'id', 'title', 'category', 'difficulty'}


def test_a_player_cannot_read_the_whole_catalogue_here(handler, user_pool, games_table):
    given_game(games_table, 'game1')

    response = handler.lambda_handler(
        a_request('/admin/games', 'GET', groups='[full-access]', caller=PLAYER), None)

    assert response['statusCode'] == 403


# --- addresses travel through a URL -----------------------------------------

def test_an_encoded_address_is_decoded_before_it_is_used(handler, user_pool, access_table):
    given_user(user_pool, PLAYER, groups=['full-access'])

    # The page has to encode the address to put it in a path, and API
    # Gateway passes path parameters through without decoding them. Left
    # alone, the @ arrives as %40: grants were written under a key nothing
    # reads back, and Cognito answered "no such user" for every change.
    encoded = PLAYER.replace('@', '%40')

    response = handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'PUT', email=encoded,
                  body={'games': ['game1']}), None)

    assert response['statusCode'] == 200
    stored = access_table.scan()['Items']
    assert [i['user_email'] for i in stored] == [PLAYER]


def test_an_encoded_address_also_reaches_cognito(handler, user_pool):
    given_user(user_pool, PLAYER)
    encoded = PLAYER.replace('@', '%40')

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=encoded,
                  body={'groups': ['full-access']}), None)

    assert response['statusCode'] == 200
    assert groups_of(user_pool, PLAYER) == ['full-access']


def test_the_self_guard_survives_encoding(handler, user_pool):
    given_user(user_pool, ADMIN, groups=['admin'])
    encoded = ADMIN.replace('@', '%40')

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'PATCH', email=encoded,
                  body={'enabled': False}), None)

    # Comparing the raw path parameter against the caller would have let an
    # admin disable themselves simply because %40 != @.
    assert response['statusCode'] == 409


# --- deleting an account ----------------------------------------------------

def test_deleting_removes_the_account_and_its_grants(handler, user_pool, access_table):
    import boto3

    given_user(user_pool, PLAYER)
    for game_id in ('game1', 'game2'):
        access_table.put_item(Item={'user_email': PLAYER, 'game_id': game_id})

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'DELETE', email=PLAYER), None)

    assert response['statusCode'] == 200
    assert body_of(response)['grants_removed'] == 2
    assert access_table.scan()['Items'] == []

    with pytest.raises(boto3.client('cognito-idp').exceptions.UserNotFoundException):
        boto3.client('cognito-idp').admin_get_user(UserPoolId=user_pool, Username=PLAYER)


def test_deleting_leaves_someone_elses_grants_alone(handler, user_pool, access_table):
    given_user(user_pool, PLAYER)
    access_table.put_item(Item={'user_email': PLAYER, 'game_id': 'game1'})
    access_table.put_item(Item={'user_email': 'other@example.com', 'game_id': 'game1'})

    handler.lambda_handler(a_request('/admin/users/{email}', 'DELETE', email=PLAYER), None)

    assert [i['user_email'] for i in access_table.scan()['Items']] == ['other@example.com']


def test_an_admin_cannot_delete_themselves(handler, user_pool):
    import boto3

    given_user(user_pool, ADMIN, groups=['admin'])

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'DELETE', email=ADMIN), None)

    # The same reasoning as the other two guards: the last administrator
    # doing this leaves the AWS CLI as the only way back in.
    assert response['statusCode'] == 409
    assert boto3.client('cognito-idp').admin_get_user(
        UserPoolId=user_pool, Username=ADMIN)['Username'] == ADMIN


def test_deleting_an_unknown_account_is_a_404(handler, user_pool):
    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'DELETE', email='ghost@example.com'), None)

    assert response['statusCode'] == 404


def test_a_player_cannot_delete_anyone(handler, user_pool):
    given_user(user_pool, PLAYER, groups=['full-access'])

    response = handler.lambda_handler(
        a_request('/admin/users/{email}', 'DELETE', email=PLAYER,
                  groups='[full-access]', caller=PLAYER), None)

    assert response['statusCode'] == 403


# --- how far the player got in what they were granted -----------------------

def test_the_grants_say_where_the_player_stands_in_each_game(
    handler, user_pool, access_table, progress_table
):
    for game_id in ('game1', 'game2', 'game3'):
        access_table.put_item(Item={'user_email': PLAYER, 'game_id': game_id})
    progress_table.put_item(Item={
        'user_email': PLAYER, 'game_id': 'game1', 'playing': True,
    })
    progress_table.put_item(Item={
        'user_email': PLAYER, 'game_id': 'game2',
        'ever_completed_at': '2026-11-01T10:00:00Z',
    })

    body = body_of(handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'GET', email=PLAYER), None))

    # game3 is absent rather than listed as todo: the catalogue listing
    # already applies that default, and sending a row per untouched game
    # would be 173 of them to say nothing.
    assert body['progress'] == {'game1': 'in_progress', 'game2': 'done'}
    assert body['games'] == ['game1', 'game2', 'game3']


def test_a_player_who_has_played_nothing_reports_nothing(
    handler, user_pool, access_table
):
    access_table.put_item(Item={'user_email': PLAYER, 'game_id': 'game1'})

    body = body_of(handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'GET', email=PLAYER), None))

    assert body['progress'] == {}


def test_progress_covers_games_that_were_never_granted(
    handler, user_pool, access_table, progress_table
):
    progress_table.put_item(Item={
        'user_email': PLAYER, 'game_id': 'game9',
        'ever_completed_at': '2026-11-01T10:00:00Z',
    })

    body = body_of(handler.lambda_handler(
        a_request('/admin/users/{email}/games', 'GET', email=PLAYER), None))

    # A full-access player holds no grant rows at all, and a grant can be
    # revoked after the fact. Progress is what happened, not what is
    # allowed, so the two lists are independent on purpose.
    assert body['games'] == []
    assert body['progress'] == {'game9': 'done'}
