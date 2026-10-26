"""
The one origin allowed to read this API.

It used to be a star in twenty-nine places. A star means any page on the
internet can call this API with a player's browser and read the answer, and
the answers here include the catalogue, a player's progress, and the state
of their AWS account.

API Gateway's own responses need the same header and are not produced by any
of these handlers — the 401 from the Cognito authorizer is the one that
matters, because without the header the browser hides it and the front's
refresh-and-retry never sees a status to react to. Those are set as
GatewayResponses in template.yaml.
"""

import os


def allowed_origin():
    """
    Read at call time, not import: the tests set it per test, and a Lambda
    container outlives any single one of them.
    """
    return os.environ['ALLOWED_ORIGIN']
