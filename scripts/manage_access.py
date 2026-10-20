#!/usr/bin/env python3
"""
Admin CLI to manage per-user game access.

Games are private by default:
- Users in the Cognito "full-access" group can see and play every game.
- Everyone else needs an explicit grant, one row per (email, game_id), in the
  GameAccess DynamoDB table.

Examples:
  python manage_access.py grant-full-access --email alice@example.com
  python manage_access.py revoke-full-access --email alice@example.com
  python manage_access.py grant-game --email bob@example.com --game-id game1
  python manage_access.py revoke-game --email bob@example.com --game-id game1
  python manage_access.py list --email bob@example.com
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

FULL_ACCESS_GROUP = "full-access"


def resolve_user_pool_id(cognito_client, stack_name):
    paginator = cognito_client.get_paginator('list_user_pools')
    expected_name = f"{stack_name}-user-pool"
    for page in paginator.paginate(MaxResults=60):
        for pool in page['UserPools']:
            if pool['Name'] == expected_name:
                return pool['Id']
    raise SystemExit(f"Could not find a user pool named '{expected_name}'. Pass --user-pool-id explicitly.")


def grant_full_access(cognito_client, user_pool_id, email):
    cognito_client.admin_add_user_to_group(
        UserPoolId=user_pool_id,
        Username=email,
        GroupName=FULL_ACCESS_GROUP,
    )
    print(f"✅ {email} added to the '{FULL_ACCESS_GROUP}' group (can see/play every game)")


def revoke_full_access(cognito_client, user_pool_id, email):
    cognito_client.admin_remove_user_from_group(
        UserPoolId=user_pool_id,
        Username=email,
        GroupName=FULL_ACCESS_GROUP,
    )
    print(f"✅ {email} removed from the '{FULL_ACCESS_GROUP}' group")


def grant_game(access_table, email, game_id):
    access_table.put_item(Item={'user_email': email, 'game_id': game_id})
    print(f"✅ {email} can now see/play game '{game_id}'")


def revoke_game(access_table, email, game_id):
    access_table.delete_item(Key={'user_email': email, 'game_id': game_id})
    print(f"✅ Removed {email}'s access to game '{game_id}'")


def list_access(cognito_client, user_pool_id, access_table, email):
    try:
        user = cognito_client.admin_list_groups_for_user(UserPoolId=user_pool_id, Username=email)
        groups = [g['GroupName'] for g in user['Groups']]
    except ClientError as e:
        if e.response['Error']['Code'] == 'UserNotFoundException':
            print(f"⚠️  {email} was not found in the user pool")
            groups = []
        else:
            raise

    if FULL_ACCESS_GROUP in groups:
        print(f"{email} is in '{FULL_ACCESS_GROUP}' — can see/play every game")
        return

    response = access_table.query(
        KeyConditionExpression='user_email = :email',
        ExpressionAttributeValues={':email': email},
    )
    game_ids = [item['game_id'] for item in response.get('Items', [])]

    print(f"{email} is restricted. Groups: {groups or 'none'}")
    print(f"Granted games: {game_ids or 'none'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--stack-name', default='aws-jam', help="SAM stack name (default: aws-jam)")
    parser.add_argument('--user-pool-id', help="Cognito User Pool id (auto-resolved from --stack-name if omitted)")
    parser.add_argument('--region', default='us-east-1')

    subparsers = parser.add_subparsers(dest='command', required=True)

    for command in ('grant-full-access', 'revoke-full-access'):
        p = subparsers.add_parser(command)
        p.add_argument('--email', required=True)

    for command in ('grant-game', 'revoke-game'):
        p = subparsers.add_parser(command)
        p.add_argument('--email', required=True)
        p.add_argument('--game-id', required=True)

    p = subparsers.add_parser('list')
    p.add_argument('--email', required=True)

    args = parser.parse_args()

    cognito_client = boto3.client('cognito-idp', region_name=args.region)
    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    access_table = dynamodb.Table(f"{args.stack_name}-Game-Access")

    user_pool_id = args.user_pool_id
    if user_pool_id is None and args.command != 'grant-game' and args.command != 'revoke-game':
        user_pool_id = resolve_user_pool_id(cognito_client, args.stack_name)

    try:
        if args.command == 'grant-full-access':
            grant_full_access(cognito_client, user_pool_id, args.email)
        elif args.command == 'revoke-full-access':
            revoke_full_access(cognito_client, user_pool_id, args.email)
        elif args.command == 'grant-game':
            grant_game(access_table, args.email, args.game_id)
        elif args.command == 'revoke-game':
            revoke_game(access_table, args.email, args.game_id)
        elif args.command == 'list':
            if user_pool_id is None:
                user_pool_id = resolve_user_pool_id(cognito_client, args.stack_name)
            list_access(cognito_client, user_pool_id, access_table, args.email)
    except ClientError as e:
        print(f"❌ {e.response['Error']['Message']}")
        sys.exit(1)


if __name__ == '__main__':
    main()
