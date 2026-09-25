"""Shared test setup.

⚠️ The point of this file is that the test suite cannot reach real AWS.

It is not hypothetical. Moving the prompt builder into lib/prompt.py left
test_ask.py stubbing `describe_location` on the handler while the real call had
moved to lib.prompt, so the stub silently stopped applying and the tests called
Amazon Location for real - on whatever credentials happened to be in the
environment. It failed with AccessDenied, and the error named the account id
and the IAM user.

A stub that stops matching its target is an ordinary mistake and will happen
again. What must not happen is that mistake reaching an AWS account, so the
credentials are replaced with fake ones for the whole session: boto3 signs with
these, gets rejected on arrival, and never touches the real account.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True, scope="session")
def _no_real_aws() -> None:
    """Point every AWS client at credentials that cannot work.

    autouse + session scope so it applies to every test without being asked
    for, including any added later that forgets to think about this.

    Both key variables are set because boto3 falls back through a chain -
    setting one alone would let it carry on to the shared credentials file.
    AWS_PROFILE is cleared for the same reason: a named profile would override
    these and put the real account back in play.
    """
    import os

    os.environ.pop("AWS_PROFILE", None)
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    # A region must be set or boto3 raises NoRegionError, which would obscure
    # the failure a test is actually about.
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")
