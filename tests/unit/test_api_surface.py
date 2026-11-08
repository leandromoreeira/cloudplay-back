"""
Does the API actually expose what the handlers answer?

A handler can grow a route and still be unreachable: the method has to exist
on the API Gateway resource, and the preflight has to admit it. On
07/11/2026 DELETE /admin/users/{email} had been written, tested, permitted
in IAM and deployed, and was answered by nothing — the template had no event
for it and Cors.AllowMethods did not list it. From the browser that reads as
a CORS error, which sends you looking at headers instead of at routing.

Unit tests cannot catch that: they call lambda_handler directly and never
pass through API Gateway. This reads the template instead.
"""

import ast
import re
from pathlib import Path

import pytest
import yaml

BACK = Path(__file__).resolve().parent.parent.parent
TEMPLATE = BACK / 'template.yaml'


class Tolerant(yaml.SafeLoader):
    """SafeLoader that shrugs at !Ref, !Sub, !GetAtt and !If."""


Tolerant.add_multi_constructor('!', lambda loader, suffix, node: None)


@pytest.fixture(scope='module')
def template():
    return yaml.load(TEMPLATE.read_text(), Loader=Tolerant)


def api_events(template):
    """Every (path, METHOD) the template puts on the API."""
    found = set()
    for resource in template.get('Resources', {}).values():
        if resource.get('Type') != 'AWS::Serverless::Function':
            continue
        for event in (resource.get('Properties', {}).get('Events') or {}).values():
            if event.get('Type') != 'Api':
                continue
            properties = event.get('Properties', {})
            found.add((properties['Path'], properties['Method'].upper()))
    return found


def allowed_methods(template):
    """Cors.AllowMethods, which is what the preflight answers with."""
    for resource in template.get('Resources', {}).values():
        if resource.get('Type') != 'AWS::Serverless::Api':
            continue
        raw = resource.get('Properties', {}).get('Cors', {}).get('AllowMethods', '')
        return {m.strip() for m in raw.strip("'").split(',') if m.strip()}
    return set()


def _literal(node, name):
    """The string in `name == '...'`, or None."""
    if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
        return None
    if not (isinstance(node.left, ast.Name) and node.left.id == name):
        return None
    value = node.comparators[0]
    return value.value if isinstance(value, ast.Constant) else None


def _parts(test, name):
    """Literals compared against `name` in a test, across an `and`."""
    tests = test.values if isinstance(test, ast.BoolOp) else [test]
    return [found for t in tests if (found := _literal(t, name))]


def handler_routes(source):
    """
    Every (route, METHOD) a handler answers, read off its own branching.

    Covers both shapes the code uses: the route and the method in one test,
    and a route test with the methods nested inside it.
    """
    found = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If):
            continue
        routes = _parts(node.test, 'route')
        if not routes:
            continue

        methods = _parts(node.test, 'method')
        if not methods:
            # Only the body. An elif is an If inside orelse, so walking the
            # whole node would hand this route the methods of every branch
            # below it.
            methods = [
                m for statement in node.body
                for inner in ast.walk(statement) if isinstance(inner, ast.If)
                for m in _parts(inner.test, 'method')
            ]

        found.update((route, method) for route in routes for method in methods)
    return found


def handlers():
    for app in sorted((BACK / 'games').glob('*/app.py')):
        routes = handler_routes(app.read_text())
        if routes:
            yield app.parent.name, routes


def test_there_is_a_handler_worth_checking():
    # If the routing is ever rewritten in a shape this cannot read, these
    # tests would pass by finding nothing. This is the canary.
    assert dict(handlers())


def test_every_route_a_handler_answers_is_on_the_api(template):
    declared = api_events(template)

    missing = {
        (name, route, method)
        for name, routes in handlers()
        for route, method in routes
        if (route, method) not in declared
    }

    # Unreachable from outside: API Gateway answers an unconfigured method
    # with 403 MissingAuthenticationToken, and the browser calls it CORS.
    assert not missing, f"answered by a handler, not exposed by the API: {missing}"


def test_the_preflight_admits_every_method_in_use(template):
    allowed = allowed_methods(template)

    missing = {
        method
        for _, routes in handlers()
        for _, method in routes
        if method not in allowed
    }

    # The browser never sends the real request when the preflight leaves the
    # method out, so the route being wired up correctly is not enough.
    assert not missing, f"used by a handler, absent from Cors.AllowMethods: {missing}"
    assert 'OPTIONS' in allowed


def test_the_allowed_origin_is_never_a_star(template):
    assert '*' not in (allowed_methods(template) or {'GET'})
    for resource in template.get('Resources', {}).values():
        if resource.get('Type') != 'AWS::Serverless::Api':
            continue
        cors = resource.get('Properties', {}).get('Cors', {})
        # AllowOrigin is an !If the loader turns into None; what must never
        # happen is a literal star written in by hand.
        assert cors.get('AllowOrigin') != "'*'"
