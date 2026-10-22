"""
Turning a game session into a console session in the account it runs in.

The player signs in to CloudPlay with Cognito; Cognito never hands out AWS
credentials. This is the broker: the backend decides whether the person owns
an active session in that account, and only then assumes HengHaTeamRole and
trades those credentials for a console sign-in URL.

Sessions last an hour and cannot last longer. The Lambda's own credentials
come from its execution role, so assuming another role from here is role
chaining, which AWS caps at one hour — and the federation endpoint refuses
the SessionDuration parameter outright when the credentials came that way.
The way around it is the Issuer below: the console sends the player there
when the session expires, and that page asks for a fresh URL.
"""

import hashlib
import json
import re
import urllib.parse
import urllib.request

import boto3

import team_role

FEDERATION_ENDPOINT = 'https://signin.aws.amazon.com/federation'
CONSOLE_HOME = 'https://us-east-1.console.aws.amazon.com/console/home?region=us-east-1'

# The sign-in token has to be used within 15 minutes of being issued.
FEDERATION_TIMEOUT_SECONDS = 10

SESSION_NAME_MAX = 64
_DISALLOWED_IN_SESSION_NAME = re.compile(r'[^\w+=,.@-]')


def session_name_for(email):
    """
    What CloudTrail in the game account will show for everything the player does.

    STS allows [\\w+=,.@-] and 64 characters. An address too long for that is
    truncated with a hash of the original, so two long addresses sharing a
    prefix still get different names.
    """
    cleaned = _DISALLOWED_IN_SESSION_NAME.sub('-', email)

    if len(cleaned) <= SESSION_NAME_MAX:
        return cleaned

    digest = hashlib.sha256(email.encode()).hexdigest()[:8]
    return f"{cleaned[:SESSION_NAME_MAX - len(digest) - 1]}-{digest}"


def player_credentials(account_id, email, sts_client=None):
    """
    Assume the player's role in their game account.

    No DurationSeconds on purpose: this is role chaining, so anything over an
    hour fails, and passing it at all makes the federation call below fail.
    """
    sts = sts_client or boto3.client('sts')

    return sts.assume_role(
        RoleArn=f'arn:aws:iam::{account_id}:role/{team_role.ROLE_NAME}',
        RoleSessionName=session_name_for(email),
    )['Credentials']


def signin_url(credentials, issuer, destination=CONSOLE_HOME):
    """
    Trade temporary credentials for a console sign-in URL.

    The result is a bearer credential: anyone holding it is signed in as the
    player. It must not be logged, stored, or put in a query string that ends
    up in an access log.
    """
    session = json.dumps({
        'sessionId': credentials['AccessKeyId'],
        'sessionKey': credentials['SecretAccessKey'],
        'sessionToken': credentials['SessionToken'],
    })

    token_request = (
        f'{FEDERATION_ENDPOINT}'
        f'?Action=getSigninToken'
        f'&Session={urllib.parse.quote_plus(session)}'
    )

    with urllib.request.urlopen(token_request, timeout=FEDERATION_TIMEOUT_SECONDS) as response:
        signin_token = json.loads(response.read().decode())['SigninToken']

    return (
        f'{FEDERATION_ENDPOINT}'
        f'?Action=login'
        f'&Issuer={urllib.parse.quote_plus(issuer)}'
        f'&Destination={urllib.parse.quote_plus(destination)}'
        f'&SigninToken={urllib.parse.quote_plus(signin_token)}'
    )
