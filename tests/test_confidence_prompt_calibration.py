from __future__ import annotations

import re

from app.ai import originator
from app.ai.originator import SYSTEM_PROMPT


def test_system_prompt_no_longer_bare_zero_to_one():
    # 19 Aug 2026: the entire confidence-scoring instruction used to be the
    # bare JSON-schema clause '"confidence": 0-1' with no calibration
    # guidance at all -- confirmed the likely cause of Claude/OpenAI landing
    # on structurally different scales from the same three-word instruction.
    assert "full 0.0-1.0 range" in SYSTEM_PROMPT
    assert "tails" in SYSTEM_PROMPT


def test_system_prompt_gives_relative_not_single_value_anchors():
    # The rewrite deliberately avoids a literal "0.3 = uncertain"-style
    # single-value-to-adjective mapping (the anchor-bias failure mode) --
    # it describes range boundaries tied to multi-word qualitative
    # descriptions instead. This guards against a future edit accidentally
    # reintroducing a sticky single-number anchor.
    assert not re.search(r"0\.3\s*=\s*\w", SYSTEM_PROMPT)
    assert not re.search(r"0\.8\s*=\s*\w", SYSTEM_PROMPT)
    assert "below 0.3" in SYSTEM_PROMPT
    assert "above 0.8" in SYSTEM_PROMPT


def test_json_schema_confidence_field_still_present():
    # The rewrite adds a calibration paragraph but must not remove or alter
    # the JSON schema clause the parser depends on.
    assert '"confidence": 0-1' in SYSTEM_PROMPT


def test_system_prompt_is_the_single_shared_constant_for_both_providers():
    # Both providers must read the identical instruction -- a per-provider
    # variant would reintroduce the exact cross-provider comparability
    # problem this change exists to fix. Confirmed by source inspection
    # rather than mocking each provider's HTTP call: both _call_openai and
    # _call_claude reference the bare SYSTEM_PROMPT name, and there is
    # exactly one SYSTEM_PROMPT assignment in the module.
    source = originator.__file__
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert text.count("SYSTEM_PROMPT = (") == 1
    assert '"role": "system", "content": SYSTEM_PROMPT' in text
    assert '"system": SYSTEM_PROMPT' in text
