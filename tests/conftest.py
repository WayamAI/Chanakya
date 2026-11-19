import json
from types import SimpleNamespace

import pytest

from chanakya.storage.db import connect


class FakeLLM:
    """Returns canned JSON responses in order."""

    def __init__(self, *responses):
        self.r = list(responses)
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        c = self.r.pop(0)
        c = c if isinstance(c, str) else json.dumps(c)
        return SimpleNamespace(content=c, message={"role": "assistant", "content": c})


@pytest.fixture
def con():
    return connect(":memory:")


@pytest.fixture
def offerings_dir(tmp_path):
    # Synthetic fixture text for tests only - not Wayam data.
    (tmp_path / "sample.md").write_text(
        "Acme Assist is a support automation product that reduces ticket backlog.\n"
        "We sell to fintech companies in India with 100 to 800 employees.\n"
        "Typical buyer: VP Customer Support.\n", encoding="utf-8")
    return tmp_path
