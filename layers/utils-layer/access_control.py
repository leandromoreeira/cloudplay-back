"""
Access control for the Games API.

Games are private by default:
- Users in the Cognito "full-access" group can see and play every game.
- Everyone else needs an explicit grant in the GameAccess table
  (one item per user_email/game_id pair).

The "admin" group is separate and orthogonal: it opens the admin area and
grants nothing in the catalogue. An administrator who should also play
every game belongs to both groups.

Identity comes from the claims the API Gateway Cognito authorizer attaches
to the request (already signature-verified by API Gateway), not from a
manual JWT decode.
"""

import os
import boto3

FULL_ACCESS_GROUP = "full-access"
ADMIN_GROUP = "admin"


def get_identity(event):
    """Return (email, groups) for the authenticated caller, or (None, []) if missing."""
    context = event.get('requestContext') or {}
    authorizer = context.get('authorizer') or {}
    claims = authorizer.get('claims') or {}
    email = claims.get('email')

    groups_claim = claims.get('cognito:groups', '')
    if isinstance(groups_claim, str):
        groups = [g for g in groups_claim.strip('[]').split(',') if g]
    else:
        groups = list(groups_claim or [])

    return email, groups


def has_full_access(groups):
    return FULL_ACCESS_GROUP in groups


def is_admin(groups):
    """
    Whether this user may manage other users.

    Deliberately says nothing about the catalogue: administering the
    platform and being allowed to play every game are two separate grants,
    so an admin who should also see every game is put in both groups.
    """
    return ADMIN_GROUP in groups


def _access_table():
    dynamodb = boto3.resource('dynamodb')
    return dynamodb.Table(os.environ['GAME_ACCESS_TABLE'])


def can_access_game(email, groups, game_id):
    """Whether this user is allowed to see/play the given game."""
    if has_full_access(groups):
        return True
    if not email:
        return False

    response = _access_table().get_item(Key={'user_email': email, 'game_id': game_id})
    return 'Item' in response


def get_accessible_game_ids(email, groups):
    """
    Set of game ids this user may see, or None if they may see every game
    (i.e. they're in the full-access group).
    """
    if has_full_access(groups):
        return None
    if not email:
        return set()

    response = _access_table().query(
        KeyConditionExpression='user_email = :email',
        ExpressionAttributeValues={':email': email}
    )
    return {item['game_id'] for item in response.get('Items', [])}
