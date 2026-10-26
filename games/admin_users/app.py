"""
User administration, for people in the "admin" group.

One function behind several routes rather than one per endpoint, which is
the pattern everywhere else here. These operations are rare, cohesive and
share a policy; six functions would have been six log groups and six copies
of the same Cognito permissions.

Everything is decided from the groups the Cognito authorizer put on the
request. Hiding a button in the UI is not access control, so every route
re-checks, and the two guards that matter are at the bottom of this
docstring rather than left to the caller:

  * nobody may disable themselves
  * nobody may take their own admin away

Either one, done by the last administrator, locks every person out of the
admin area and leaves the AWS CLI as the only way back in.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# Import from Lambda Layer
import cors
from access_control import (
    ADMIN_GROUP,
    FULL_ACCESS_GROUP,
    get_identity,
    is_admin,
)

MANAGED_GROUPS = (FULL_ACCESS_GROUP, ADMIN_GROUP)


def _response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': cors.allowed_origin(),
        },
        'body': json.dumps(body)
    }


def _cognito():
    return boto3.client('cognito-idp')


def _pool():
    return os.environ['USER_POOL_ID']


def _access_table():
    return boto3.resource('dynamodb').Table(os.environ['GAME_ACCESS_TABLE'])


def _attribute(user, name):
    for attribute in user.get('Attributes', user.get('UserAttributes', [])):
        if attribute['Name'] == name:
            return attribute['Value']
    return None


def _groups_of(client, username):
    groups = client.admin_list_groups_for_user(
        UserPoolId=_pool(), Username=username
    ).get('Groups', [])
    return sorted(g['GroupName'] for g in groups)


# --- the routes -------------------------------------------------------------

def list_users(client):
    """
    Everyone in the pool, with the groups they belong to.

    One extra call per user for the groups: the list call does not carry
    them. Fine for a cohort, worth revisiting if this ever lists hundreds.
    """
    users = []
    kwargs = {'UserPoolId': _pool()}

    while True:
        page = client.list_users(**kwargs)
        for user in page.get('Users', []):
            username = user['Username']
            users.append({
                'email': _attribute(user, 'email') or username,
                'username': username,
                'status': user.get('UserStatus'),
                'enabled': user.get('Enabled', True),
                'created_at': user['UserCreateDate'].isoformat()
                if user.get('UserCreateDate') else None,
                'groups': _groups_of(client, username),
            })

        token = page.get('PaginationToken')
        if not token:
            break
        kwargs['PaginationToken'] = token

    users.sort(key=lambda u: u['email'])
    return _response(200, {'users': users})


def create_user(client, body):
    email = (body.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return _response(400, {'message': 'A valid email address is required'})

    groups = [g for g in body.get('groups', []) if g in MANAGED_GROUPS]

    try:
        client.admin_create_user(
            UserPoolId=_pool(),
            Username=email,
            UserAttributes=[
                {'Name': 'email', 'Value': email},
                # Verified on their behalf: the address is where the
                # temporary password is going, and an unverified one would
                # leave them unable to recover the account later.
                {'Name': 'email_verified', 'Value': 'true'},
            ],
            DesiredDeliveryMediums=['EMAIL'],
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'UsernameExistsException':
            return _response(409, {'message': f'{email} already has an account'})
        raise

    for group in groups:
        client.admin_add_user_to_group(
            UserPoolId=_pool(), Username=email, GroupName=group
        )

    return _response(201, {'email': email, 'groups': sorted(groups)})


def update_user(client, caller, target, body):
    """Enable/disable, and group membership. Both guarded against self."""
    if 'enabled' in body:
        enabled = bool(body['enabled'])
        if target == caller and not enabled:
            return _response(
                409, {'message': 'You cannot disable your own account'}
            )

        if enabled:
            client.admin_enable_user(UserPoolId=_pool(), Username=target)
        else:
            client.admin_disable_user(UserPoolId=_pool(), Username=target)

    if 'groups' in body:
        wanted = {g for g in body['groups'] if g in MANAGED_GROUPS}

        if target == caller and ADMIN_GROUP not in wanted:
            return _response(
                409, {'message': 'You cannot remove your own admin access'}
            )

        current = set(_groups_of(client, target)) & set(MANAGED_GROUPS)

        for group in wanted - current:
            client.admin_add_user_to_group(
                UserPoolId=_pool(), Username=target, GroupName=group
            )
        for group in current - wanted:
            client.admin_remove_user_from_group(
                UserPoolId=_pool(), Username=target, GroupName=group
            )

    return _response(200, {'email': target, 'groups': _groups_of(client, target)})


def reset_mfa(client, target):
    """
    Forget the authenticator app this user enrolled.

    What it is for is a lost or wiped phone: with the preference cleared,
    the pool asks them to enrol again at their next sign-in rather than for
    a code they can no longer produce.
    """
    client.admin_set_user_mfa_preference(
        UserPoolId=_pool(),
        Username=target,
        SoftwareTokenMfaSettings={'Enabled': False, 'PreferredMfa': False},
    )
    return _response(200, {'email': target, 'message': 'Authenticator reset'})


def list_grants(target):
    items = _access_table().query(
        KeyConditionExpression=Key('user_email').eq(target)
    ).get('Items', [])
    return _response(200, {'email': target, 'games': sorted(i['game_id'] for i in items)})


def set_grants(target, body):
    """Replace this user's per-game grants with the list given."""
    wanted = {g for g in body.get('games', []) if isinstance(g, str) and g}
    table = _access_table()

    current = {
        item['game_id']
        for item in table.query(
            KeyConditionExpression=Key('user_email').eq(target)
        ).get('Items', [])
    }

    with table.batch_writer() as batch:
        for game_id in wanted - current:
            batch.put_item(Item={'user_email': target, 'game_id': game_id})
        for game_id in current - wanted:
            batch.delete_item(Key={'user_email': target, 'game_id': game_id})

    return _response(200, {'email': target, 'games': sorted(wanted)})


# --- routing ----------------------------------------------------------------

def lambda_handler(event, context):
    caller, groups = get_identity(event)

    if not caller:
        return _response(401, {'message': 'Invalid or expired token'})

    # The authorizer proves who is calling, never what they may do.
    if not is_admin(groups):
        return _response(403, {'message': 'Administrator access required'})

    method = event.get('httpMethod')
    route = event.get('resource', '')
    target = (event.get('pathParameters') or {}).get('email')
    if target:
        target = target.strip().lower()

    try:
        body = json.loads(event['body']) if event.get('body') else {}
    except (TypeError, ValueError):
        return _response(400, {'message': 'Body is not valid JSON'})

    client = _cognito()

    try:
        if route == '/admin/users':
            if method == 'GET':
                return list_users(client)
            if method == 'POST':
                return create_user(client, body)

        elif route == '/admin/users/{email}' and method == 'PATCH':
            return update_user(client, caller, target, body)

        elif route == '/admin/users/{email}/reset-mfa' and method == 'POST':
            return reset_mfa(client, target)

        elif route == '/admin/users/{email}/games':
            if method == 'GET':
                return list_grants(target)
            if method == 'PUT':
                return set_grants(target, body)

        return _response(405, {'message': f'{method} {route} is not an admin route'})

    except ClientError as e:
        code = e.response['Error']['Code']
        if code == 'UserNotFoundException':
            return _response(404, {'message': f'No account for {target}'})

        print(f"Admin action failed: {e}")
        return _response(500, {
            'message': 'The directory refused that change',
            'details': e.response['Error']['Message'],
        })
