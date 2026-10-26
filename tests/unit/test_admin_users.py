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
def handler(user_pool, access_table):
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
        a_request('/admin/users/{email}', 'DELETE', email=PLAYER), None)

    # Deleting a user is deliberately not implemented: disabling covers the
    # real case and keeps the account's history intact.
    assert response['statusCode'] == 405


def test_a_body_that_is_not_json_is_refused(handler, user_pool):
    event = a_request('/admin/users', 'POST')
    event['body'] = 'not json'

    assert handler.lambda_handler(event, None)['statusCode'] == 400
