"""
Asking the account whether it is empty, instead of believing the nuke.

The case these exist for is narrow and real: aws-nuke dropped a bucket it
could not read from its own inventory and exited 0. So what matters here is
not that a clean account passes — it is that an answer nobody could get
counts against the account rather than for it.
"""

import pytest
from botocore.exceptions import ClientError, UnknownServiceError

from account_sweep import (
    BUDGET_SECONDS, PROBE_CONFIG, PROBES, Probe, client_factory, leftovers,
)

ACCOUNT = '111111111111'


class FakeClient:
    """Answers one listing, or raises what a real client would raise."""

    def __init__(self, **answers):
        self._answers = answers

    def __getattr__(self, operation):
        answer = self._answers.get(operation, {})

        def call():
            if isinstance(answer, Exception):
                raise answer
            return answer

        return call


def denied(operation):
    return ClientError(
        {'Error': {'Code': 'AccessDenied', 'Message': 'explicit deny'}}, operation
    )


def clients(**by_service):
    return lambda service: by_service[service]


def test_an_empty_account_has_nothing_to_report():
    found = leftovers(clients(
        s3=FakeClient(list_buckets={'Buckets': []}),
        **{'vpc-lattice': FakeClient()},
    ))

    assert found == []


def test_a_bucket_is_reported_by_name():
    found = leftovers(clients(
        s3=FakeClient(list_buckets={'Buckets': [{'Name': 'jam-111111111111'}]}),
        **{'vpc-lattice': FakeClient()},
    ))

    assert found == ['S3 bucket jam-111111111111']


def test_access_denied_counts_as_dirty_not_as_clean():
    """The whole point. aws-nuke treated this as 'nothing there'."""
    found = leftovers(clients(
        s3=FakeClient(list_buckets=denied('ListBuckets')),
        **{'vpc-lattice': FakeClient()},
    ))

    assert len(found) == 1
    assert 'could not be checked' in found[0]
    assert 'S3 bucket' in found[0]


def test_a_service_the_runtime_has_never_heard_of_counts_as_dirty():
    # What an old Lambda SDK raises for vpc-lattice. A probe that cannot be
    # built is not a probe that passed.
    def client_for(service):
        if service == 'vpc-lattice':
            raise UnknownServiceError(
                service_name='vpc-lattice', known_service_names=['s3']
            )
        return FakeClient(list_buckets={'Buckets': []})

    found = leftovers(client_for)

    assert len(found) == 3
    assert all('could not be checked' in line for line in found)


def test_lattice_leftovers_are_reported():
    found = leftovers(clients(
        s3=FakeClient(list_buckets={'Buckets': []}),
        **{'vpc-lattice': FakeClient(
            list_service_networks={'items': [{'name': 'jam-network'}]},
            list_target_groups={'items': [{'name': 'lambda-tg'}]},
        )},
    ))

    assert found == [
        'VPC Lattice service network jam-network',
        'VPC Lattice target group lambda-tg',
    ]


def test_every_probe_is_reported_not_just_the_first():
    found = leftovers(clients(
        s3=FakeClient(list_buckets={'Buckets': [{'Name': 'one'}, {'Name': 'two'}]}),
        **{'vpc-lattice': FakeClient(
            list_services={'items': [{'name': 'svc'}]},
        )},
    ))

    # The quarantine mail is the only place a person sees this, so it has to
    # carry everything, not stop at the first thing found.
    assert len(found) == 3


def test_a_nameless_resource_is_still_reported():
    found = leftovers(clients(
        s3=FakeClient(list_buckets={'Buckets': [{}]}),
        **{'vpc-lattice': FakeClient()},
    ))

    assert found == ['S3 bucket ?']


def test_lattice_is_covered_because_aws_nuke_does_not_cover_it():
    # Pins the reason this module exists. aws-nuke v3.69.1 has no VPC
    # Lattice resource at all, and game150 builds six types of them.
    assert {p.service for p in PROBES} == {'s3', 'vpc-lattice'}


# --- the client factory -----------------------------------------------------

class CountingManager:
    def __init__(self):
        self.calls = []
        self.configs = []

    def create_cross_account_client(self, account_id, service_name, config=None):
        self.calls.append((account_id, service_name))
        self.configs.append(config)
        return FakeClient()


def test_the_role_is_assumed_once_per_service_not_once_per_question():
    manager = CountingManager()

    leftovers(client_factory(manager, ACCOUNT))

    # Four probes, two services. Without the cache this would be four
    # AssumeRole round trips to answer four questions.
    assert manager.calls == [(ACCOUNT, 's3'), (ACCOUNT, 'vpc-lattice')]


def test_a_probe_may_be_added_without_touching_the_loop():
    found = leftovers(
        clients(sqs=FakeClient(list_queues={'QueueUrls': ['https://q/one']})),
        probes=(Probe('sqs', 'list_queues', 'QueueUrls', 'Name', 'SQS queue'),),
    )

    # QueueUrls holds plain strings, not dicts. A listing whose shape is not
    # the usual one must still name what it found.
    assert found == ['SQS queue https://q/one']


def test_a_probe_that_blows_up_reports_instead_of_escaping():
    class Exploding:
        def list_buckets(self):
            raise RuntimeError('something nobody predicted')

    found = leftovers(
        clients(s3=Exploding()),
        probes=(PROBES[0],),
    )

    # Letting this escape would leave the account CLEANING with no
    # cleanup_failed_at, and the janitor only comes back for accounts that
    # have one — out of the pool forever, silently.
    assert found == ['S3 bucket: could not be checked (RuntimeError)']


def test_the_probes_do_not_wait_the_way_botocore_would():
    manager = CountingManager()

    leftovers(client_factory(manager, ACCOUNT))

    # A minute of read timeout, retried, outlives the function. Hanging is
    # the one outcome that must not happen here: an exception escaping
    # cleanup_result leaves the account CLEANING with no cleanup_failed_at,
    # and nothing comes back for it.
    assert all(config is PROBE_CONFIG for config in manager.configs)
    assert PROBE_CONFIG.connect_timeout <= 5
    assert PROBE_CONFIG.read_timeout <= 10
    assert PROBE_CONFIG.retries['max_attempts'] <= 2


# --- the budget -------------------------------------------------------------

class Clock:
    """A clock the test moves, so no test ever waits for one."""

    def __init__(self, step=0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        reading = self.now
        self.now += self.step
        return reading


def test_a_sweep_that_runs_long_reports_instead_of_running_on():
    # Half a minute per reading: the first probe fits inside the budget and
    # the rest do not.
    found = leftovers(
        clients(
            s3=FakeClient(list_buckets={'Buckets': [{'Name': 'jam-1'}]}),
            **{'vpc-lattice': FakeClient()},
        ),
        now=Clock(step=30),
    )

    # What was learned before the clock ran out is kept, and the rest counts
    # against the account rather than being passed over in silence.
    assert found[0] == 'S3 bucket jam-1'
    assert len(found) == 4
    assert all('out of time' in line for line in found[1:])


def test_running_out_of_time_immediately_clears_nobody():
    found = leftovers(
        clients(
            s3=FakeClient(list_buckets={'Buckets': []}),
            **{'vpc-lattice': FakeClient()},
        ),
        now=Clock(step=60),
    )

    # An empty list is the one answer that releases an account. A sweep that
    # never ran must never produce it.
    assert len(found) == 4
    assert found != []


def test_a_quick_sweep_spends_none_of_its_budget():
    found = leftovers(
        clients(
            s3=FakeClient(list_buckets={'Buckets': []}),
            **{'vpc-lattice': FakeClient()},
        ),
        now=Clock(step=0.01),
    )

    assert found == []


def test_the_budget_leaves_the_function_room_to_write_the_answer():
    # cleanup_result is given 120s in template.yaml. The budget has to be
    # far enough under it that the last probe, the two AssumeRole calls and
    # the DynamoDB write all still fit.
    assert BUDGET_SECONDS <= 60
