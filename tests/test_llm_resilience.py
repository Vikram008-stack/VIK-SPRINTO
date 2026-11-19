"""Live lesson (2026-11-19): gemini-3.8-flash returned 503 'high demand'; one fallback was not enough."""
import pytest
from pydantic import BaseModel

from src.common.config import load_settings
from src.llm.gemini_client import BaseLlm, CallCtx, LlmError, RawCall, TransientLlmError
from src.pipeline import friendly_llm_error
from src.store.models import ReasonCode, Step


class Out(BaseModel):
    ok: bool


class Flaky(BaseLlm):
    def __init__(self, cfg, db, down):
        super().__init__(cfg, db, sleep=lambda _s: None)
        self.down, self.tried = down, []

    def _call_json(self, model, system, user, schema):
        self.tried.append(model)
        if model in self.down:
            raise TransientLlmError("server error: 503 UNAVAILABLE high demand")
        return RawCall(text='{"ok": true}', input_tokens=5, output_tokens=2)


def _cfg():
    cfg = load_settings().pipeline.llm.model_copy(deep=True)
    cfg.models["draft"] = "m1"
    cfg.fallback_model, cfg.fallback_models = "m2", ["m3"]
    return cfg


def test_falls_through_the_whole_chain(db):
    llm = Flaky(_cfg(), db, down={"m1", "m2"})
    r = llm.generate_json(CallCtx("x.io", Step.DRAFT), "draft", "s", "u", Out)
    assert r.parsed.ok and r.model == "m3"
    assert [m for m in dict.fromkeys(llm.tried)] == ["m1", "m2", "m3"]


def test_all_models_down_raises_and_message_is_plain(db):
    llm = Flaky(_cfg(), db, down={"m1", "m2", "m3"})
    with pytest.raises(LlmError) as e:
        llm.generate_json(CallCtx("x.io", Step.DRAFT), "draft", "s", "u2", Out)
    msg = friendly_llm_error(e.value)
    assert "overloaded" in msg and "{" not in msg


@pytest.mark.parametrize("detail,needle", [("429 RESOURCE_EXHAUSTED quota", "usage limit"),
                                           ("timeout after 25s", "too long"), ("weird", "try again")])
def test_friendly_messages(detail, needle):
    assert needle in friendly_llm_error(LlmError(ReasonCode.LLM_ERROR, detail))
