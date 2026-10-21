"""
Turns the outcome of an account cleanup build into account state.

The build is only responsible for wiping the account. This owns the state
transition, because a build that times out or gets stopped never reaches its
own last step, and an account stuck in CLEANING is an account permanently out
of the pool.
"""

# Import from Lambda Layer
from account_manager import get_account_manager


def _target_account(detail):
    """The account the build was asked to wipe, from its env var overrides."""
    env = detail.get('additional-information', {}).get('environment', {})

    for variable in env.get('environment-variables', []):
        if variable.get('name') == 'TARGET_ACCOUNT':
            return variable.get('value')

    return None


def lambda_handler(event, context):
    detail = event.get('detail', {})
    status = detail.get('build-status')
    build_id = detail.get('build-id')

    account_id = _target_account(detail)
    if not account_id or account_id == 'unset':
        print(f"Build {build_id} finished as {status} with no TARGET_ACCOUNT, nothing to do")
        return

    account_manager = get_account_manager()

    if status == 'SUCCEEDED':
        account_manager.mark_available(account_id)
        print(f"Account {account_id} wiped, back in the pool")
        return

    account_manager.mark_quarantined(
        account_id,
        f"cleanup build {status}: {build_id}"
    )
    print(f"Account {account_id} quarantined after cleanup build {status}")
