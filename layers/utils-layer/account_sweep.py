"""
Is the account actually empty, now that the nuke says it is?

The nuke's word is not enough, and this is not a theory. On 03/11/2026 a
player's bucket policy denied every principal, which denies CloudPlayGameRole
too. aws-nuke v3.68.3 lists S3 buckets by calling GetBucketTagging on each
one and, on any error that is not NoSuchTagSet, drops the bucket from the
list without logging it (resources/s3-bucket.go). The bucket never reached
the filters, so it was not even counted as filtered: the run reported
"0 nukeable", exited 0, and two of the three pool accounts went back into
rotation holding a bucket nobody could delete.

Two rules come out of that, and both matter more than the list of probes:

1. **This lives outside aws-nuke.** Checking with the tool that has the
   blind spot cannot find the blind spot. Running the nuke a second time in
   dry-run would have passed here.
2. **An error means dirty, never clean.** Treating AccessDenied as "nothing
   there" is the exact mistake being corrected. Anything a probe cannot
   answer counts against the account.

The probes are not an inventory of AWS. They cover what our own game
templates can create and the nuke cannot see — derived by crossing the
resource types in cloudraiser-games against aws-nuke's resource list, which
documents what it supports and nowhere documents what it does not. That
crossing is in the backend CLAUDE.md, with the four services still
uncovered here.
"""

from collections import namedtuple

from botocore.exceptions import BotoCoreError, ClientError

# service:     the boto3 client to build inside the game account
# operation:   a list call that needs no parent resource
# items:       where the answers live in the response
# name:        the field worth printing in the alert
# label:       what a human should read in the quarantine mail
Probe = namedtuple('Probe', 'service operation items name label')

PROBES = (
    # A game account's steady state is zero buckets, so one is enough to
    # fail. ListBuckets is also the one S3 call a locked-out bucket still
    # answers — every other call on it returns AccessDenied, which is why
    # the check counts buckets instead of inspecting them.
    Probe('s3', 'list_buckets', 'Buckets', 'Name', 'S3 bucket'),

    # aws-nuke has no VPC Lattice resources at all; game150 builds six
    # types of them. Listeners and the two association types cannot outlive
    # the service network or service they hang off, so these three cover
    # all six.
    Probe('vpc-lattice', 'list_service_networks', 'items', 'name',
          'VPC Lattice service network'),
    Probe('vpc-lattice', 'list_services', 'items', 'name',
          'VPC Lattice service'),
    Probe('vpc-lattice', 'list_target_groups', 'items', 'name',
          'VPC Lattice target group'),
)


def leftovers(client_for, probes=PROBES):
    """
    Everything still standing in the account, as lines a person can read.

    An empty list means the account is clean. `client_for` takes a service
    name and returns a boto3 client already inside the target account.

    Deliberately not paginated: the question is whether anything is left,
    and the first page answers it. Counting them exactly would cost calls
    and change no decision.
    """
    found = []

    for probe in probes:
        try:
            client = client_for(probe.service)
            answer = getattr(client, probe.operation)()
            found.extend(
                f'{probe.label} {_name_of(item, probe.name)}'
                for item in answer.get(probe.items, [])
            )
        except Exception as error:  # noqa: BLE001
            # Broad on purpose. ClientError covers AccessDenied and
            # BotoCoreError covers UnknownServiceError — the SDK of a
            # runtime older than the service — but the decision here is
            # "did anything stop us being sure", and a narrower catch would
            # let the rest escape the handler. An exception out of
            # cleanup_result leaves the account CLEANING with no
            # cleanup_failed_at, and the janitor only retries accounts that
            # have one: silently out of the pool forever, which is a worse
            # version of the bug this file exists to catch.
            found.append(
                f'{probe.label}: could not be checked ({error.__class__.__name__})'
            )

    return found


def _name_of(item, field):
    """What to call a leftover in the alert, whatever shape it arrived in."""
    if isinstance(item, dict):
        return item.get(field) or '?'
    return item


def client_factory(account_manager, account_id):
    """
    Build clients inside a game account, assuming the role once per service.

    The probes share two services between them, so without the cache this
    would be four AssumeRole calls to ask four questions.
    """
    cache = {}

    def client_for(service):
        if service not in cache:
            cache[service] = account_manager.create_cross_account_client(
                account_id, service
            )
        return cache[service]

    return client_for
