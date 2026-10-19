"""The OpenAI-compatible connections: how they are set in the environment, the request, the
answer, the errors, the run's choice of model and the Settings page's Test button. The servers
are httpx mock transports."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.config import Llm
from app.llm import (
    BadAnswer,
    Connection,
    OpenAIBackend,
    Router,
    Unreachable,
    connections_from_env,
    input_budget,
    models_for_run,
    strict_schema,
)
from app.store import Store

SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "note": {"type": "string"}},
                "required": ["id"],
            },
        }
    },
    "required": ["scores"],
}
LAN = Connection("lan", "http://192.168.1.20:8080/v1", context_tokens=16384, output_tokens=4096)


def reply(content, finish="stop", prompt=100, completion=20, cached=0):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


class Server:
    """Answers chat completions from a list, in turn; lists `models`; records each request."""

    def __init__(self, answers=(), models=("qwen", "llama"), status=200):
        self.answers, self.models, self.status, self.requests = list(answers), list(models), status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="nope")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in self.models]})
        answer = self.answers.pop(0)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def body(self, n=0):
        return json.loads(self.requests[n].content)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def backend(server, connection=LAN, key=""):
    return OpenAIBackend(replace(connection, key=key) if key else connection, server.client())


def test_strict_schema_closes_objects_and_makes_optional_fields_nullable():
    strict = strict_schema(SCHEMA)
    item = strict["properties"]["scores"]["items"]
    assert strict["additionalProperties"] is False and item["additionalProperties"] is False
    assert item["required"] == ["id", "note"] and item["properties"]["note"]["type"] == ["string", "null"]
    assert item["properties"]["id"]["type"] == "string"
    assert SCHEMA["properties"]["scores"]["items"]["required"] == ["id"], "the original is left alone"


def test_a_request_asks_for_the_schema_and_turns_reasoning_off_on_a_local_server():
    server = Server([reply('{"scores": [{"id": "a", "note": null}]}', cached=40)])
    got = backend(server, key="sk-lan").structured(
        "Rank.", "<candidates/>", SCHEMA, model="lan/qwen", shared="<interests/>"
    )
    body = server.body()
    assert server.requests[0].url == "http://192.168.1.20:8080/v1/chat/completions"
    assert server.requests[0].headers["authorization"] == "Bearer sk-lan"
    assert body["model"] == "qwen" and body["max_tokens"] == 4096 and body["temperature"] == 0
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # The shared part first, for servers that cache a repeated opening; the schema in the prompt too.
    assert body["messages"][1]["content"] == "<interests/><candidates/>"
    assert '"additionalProperties": false' in body["messages"][0]["content"]
    # The null strict mode needed for an optional field is gone again.
    assert got.data == {"scores": [{"id": "a"}]}
    assert (got.input_tokens, got.cache_read_tokens, got.output_tokens) == (60, 40, 20)
    assert got.priced is False, "a server you run costs nothing"


def test_openai_itself_gets_its_own_token_limit_and_no_template_options():
    server = Server([reply('{"scores": []}')])
    openai = Connection("openai", "https://api.openai.com/v1", free=False)
    got = backend(server, openai, key="sk-x").structured("s", "u", SCHEMA, model="openai/gpt-6-mini")
    body = server.body()
    assert "max_tokens" not in body and body["max_completion_tokens"] == openai.output_tokens
    assert "chat_template_kwargs" not in body and got.priced is True
    # No key: no Authorization header at all, for a server that needs none.
    server = Server([reply('{"scores": []}')])
    backend(server).structured("s", "u", SCHEMA, model="lan/qwen")
    assert "authorization" not in server.requests[0].headers


def test_reasoning_left_in_the_text_and_a_fence_are_removed():
    server = Server([reply('<think>which first?</think>\n```json\n{"scores": [{"id": "b"}]}\n```')])
    assert backend(server).structured("s", "u", SCHEMA, model="lan/qwen").data == {"scores": [{"id": "b"}]}


def test_an_answer_that_is_not_json_is_asked_for_once_more_saying_what_was_wrong():
    server = Server([reply("Sure! Here are the scores."), reply('{"scores": []}', prompt=150, completion=10)])
    got = backend(server).structured("s", "u", SCHEMA, model="lan/qwen")
    again = server.body(1)["messages"]
    assert again[-2] == {"role": "assistant", "content": "Sure! Here are the scores."}
    assert "not valid JSON" in again[-1]["content"]
    assert got.data == {"scores": []} and (got.input_tokens, got.output_tokens) == (250, 30), "both calls counted"

    server = Server([reply("no"), reply("[1, 2]")])
    with pytest.raises(BadAnswer, match="not a JSON object"):
        backend(server).structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(server.requests) == 2, "only one retry"


def test_an_answer_cut_off_at_the_limit_is_bad_and_not_asked_again():
    server = Server([reply('{"scores": [{"id": "a"', finish="length")])
    with pytest.raises(BadAnswer, match="cut off at 4096 tokens; raise the answer limit"):
        backend(server).structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(server.requests) == 1


def test_errors_say_whether_the_connection_or_the_answer_failed():
    def down(request):
        raise httpx.ConnectError("refused")

    broken = OpenAIBackend(LAN, httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(Unreachable, match="ConnectError"):
        broken.structured("s", "u", SCHEMA, model="lan/qwen")
    for status, kind in ((401, Unreachable), (503, Unreachable), (400, BadAnswer)):
        with pytest.raises(kind, match=f"HTTP {status}"):
            backend(Server(status=status)).structured("s", "u", SCHEMA, model="lan/qwen")


def test_a_model_is_checked_by_its_connections_model_list():
    router = Router({"lan": backend(Server(models=["qwen"]))})
    assert router.check("lan/qwen") == ""
    assert "does not list the model gemma" in router.check("lan/gemma")
    assert "no connection 'other' for the model other/qwen: set FORENOON_LLM_OTHER_URL" in router.check("other/qwen")
    unread = Router({}, {"other": "FORENOON_LLM_OTHER_CONTEXT: give a whole number from 16384 to 2000000"})
    assert "FORENOON_LLM_OTHER_CONTEXT: give a whole number" in unread.check("other/qwen"), "why it is missing"
    assert "set FORENOON_ANTHROPIC_API_KEY" in router.check("anthropic/claude-haiku-5-5")
    assert Router({"lan": backend(Server(status=500))}).check("lan/qwen").startswith("lan:")


def test_a_run_moves_to_the_fallback_only_when_a_connection_cannot_be_reached():
    up = Router({"lan": backend(Server(models=["qwen"])), "spare": backend(Server(models=["small"]))})
    cfg = Llm(model="lan/qwen", fallback_model="spare/small")
    assert models_for_run(cfg, up) == (cfg, "")

    down = Router({"lan": backend(Server(status=503)), "spare": backend(Server(models=["small"]))})
    used, note = models_for_run(Llm(model="lan/qwen", choose_model="lan/qwen", fallback_model="spare/small"), down)
    assert used.model == used.choose_model == "spare/small"
    assert note.startswith("lan/qwen could not be reached") and note.endswith("so spare/small was used")
    assert note.count("could not be reached") == 1, "said once for both settings"

    both = Router({"lan": backend(Server(status=503)), "spare": backend(Server(status=503))})
    used, note = models_for_run(cfg, both)
    assert used.model == "lan/qwen" and "nor the fallback spare/small" in note
    used, note = models_for_run(Llm(model="lan/qwen"), both)
    assert "there is no fallback model" in note
    assert models_for_run(cfg, None) == (cfg, ""), "no connections at all is reported elsewhere"


def test_batches_are_sized_to_a_small_context():
    from app.rank import batch_size
    from app.read import Reading, _batches

    router = Router(
        {"lan": backend(Server(), Connection("lan", "http://x/v1", context_tokens=8192, output_tokens=2048))}
    )
    cfg = Llm(model="lan/qwen", batch_size=80)
    assert input_budget(router, "lan/qwen") == 8192 - 2048 - 512, "the whole answer limit is kept free"
    assert input_budget(router, "anthropic/claude-haiku-5-5") is None
    assert batch_size(cfg, router, "x" * 4000) < 80 and batch_size(cfg, router, "") >= 1
    assert batch_size(cfg, None, "x" * 4000) == 80, "Anthropic keeps the setting"

    long = Reading("a", "A", "S", 5000, " ".join(["word"] * 5000))
    batches = _batches([long, Reading("b", "B", "S", 10, "ten words")], limit=2000)
    assert [len(b) for b in batches] == [1, 1] and len(batches[0][0].text.split()) < 1500


def _migrated(tmp_path) -> Store:
    store = Store(tmp_path / "forenoon.sqlite3")
    for migration in sorted((Path(__file__).parent.parent / "web" / "migrations").glob("*.sql")):
        store.db.executescript(migration.read_text())
    store.db.commit()
    return store


def test_calls_to_a_server_you_run_cost_nothing_and_stay_outside_the_token_cap(tmp_path):
    from app.llm import Reply

    store = _migrated(tmp_path)
    store.log_llm_reply("rank", "lan/qwen", Reply({}, 9000, 4000, priced=False), "ok")
    store.log_llm_reply("rank", "anthropic/claude-haiku-5-5", Reply({}, 1000, 500), "ok")
    store.log_llm_error("rank", "lan/qwen", Unreachable("lan: down"))
    store.log_llm_error("rank", "lan/qwen", BadAnswer("lan: cut off"))
    assert store.llm_tokens_since("2000-01-01") == 1500
    rows = store.llm_call_rows()
    assert rows[0]["cost"] == 0.0 and rows[1]["cost"] > 0
    assert [r["status"] for r in rows[2:]] == ["unreachable", "error"]
    assert store.llm_usage_since("2000-01-01")["unpriced"] == 0


def test_a_server_you_run_is_not_held_back_by_the_daily_token_cap(tmp_path):
    from app.config import Source, User
    from app.llm import free
    from app.rank import rank
    from app.sources import Item

    answer = {"scores": [{"id": "a", "score": 9, "reason": "Compilers."}, {"id": "b", "score": 1, "reason": "No."}]}
    server = Server([reply(json.dumps(answer))])
    router = Router({"lan": backend(server)})
    paid = Router({"openai": backend(Server(), Connection("openai", "https://api.openai.com/v1"))})
    assert free(router, "lan/qwen") and not free(paid, "openai/gpt-6-mini"), "OpenAI itself is always paid"
    assert not free(router, "anthropic/claude-haiku-5-5") and not free(None, "lan/qwen")
    store = Store(tmp_path / "forenoon.sqlite3")
    items = [Item(i, f"https://x/{i}", Source("S"), i.upper(), "", None, "") for i in ("a", "b")]
    cfg = Llm(model="lan/qwen", daily_token_cap=0)
    ranking = rank(cfg, router, store, User("john", "John", ()), items, slots=1, today_start="2026-10-17")
    assert ranking.choices["a"].score == 9 and not ranking.note, "a cap of 0 does not stop a free server"


def test_a_malformed_address_or_model_list_is_unreachable_not_a_crash():
    bad = OpenAIBackend(Connection("lan", "http://192.168.1.20:80o0/v1"))
    with pytest.raises(Unreachable):
        bad.model_ids()
    with pytest.raises(Unreachable):
        bad.structured("s", "u", SCHEMA, model="lan/qwen")
    assert Router({"lan": bad}).check("lan/qwen").startswith("lan:")

    class Null(Server):
        def __call__(self, request):
            return httpx.Response(200, json={"object": "list", "data": None})

    assert backend(Null()).model_ids() == []

    class Odd(Server):
        def __call__(self, request):
            return httpx.Response(200, json={"data": "qwen"})

    with pytest.raises(Unreachable, match="no list of models"):
        backend(Odd()).model_ids()


def test_openai_gets_no_temperature_and_a_local_answer_limit_leaves_room_for_the_prompt():
    server = Server([reply('{"scores": []}')])
    backend(server, Connection("openai", "https://api.openai.com/v1"), "k").structured(
        "s", "u", SCHEMA, model="openai/o5"
    )
    assert "temperature" not in server.body()

    small = Connection("lan", "http://x/v1", context_tokens=8192, output_tokens=4096)
    server = Server([reply('{"scores": []}')])
    backend(server, small).structured("s", "x" * 18000, SCHEMA, model="lan/qwen")
    body = server.body()
    assert body["temperature"] == 0 and 256 <= body["max_tokens"] < 4096, "prompt and answer fit the context"


def test_a_failed_call_logs_the_tokens_it_used(tmp_path):
    server = Server([reply("no", prompt=500, completion=40), reply("still no", prompt=600, completion=50)])
    with pytest.raises(BadAnswer) as failed:
        backend(server).structured("s", "u", SCHEMA, model="lan/qwen")
    assert (failed.value.input_tokens, failed.value.output_tokens, failed.value.priced) == (1100, 90, False)
    store = _migrated(tmp_path)
    store.log_llm_error("rank", "lan/qwen", failed.value)
    (row,) = store.llm_call_rows()
    assert (row["input_tokens"], row["output_tokens"], row["status"], row["cost"]) == (1100, 90, "error", 0.0)


def test_a_connection_found_down_is_not_tried_again_in_the_run():
    server = Server(status=503)
    router = Router({"lan": backend(server)})
    assert router.check("lan/qwen").startswith("lan:")
    with pytest.raises(Unreachable, match="not tried again this run"):
        router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(server.requests) == 1, "only the start-of-run look reached it"

    # Refused: down for the rest of the run, for every reader.
    calls = []

    def refused(request):
        calls.append(request)
        raise httpx.ConnectError("refused")

    router = Router({"lan": OpenAIBackend(LAN, httpx.Client(transport=httpx.MockTransport(refused)))})
    for _ in range(3):
        with pytest.raises(Unreachable):
            router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(calls) == 1, "the first refused call is the last"

    # A server error loses only that call: the next is tried.
    erring = Server(status=503)
    router = Router({"lan": backend(erring)})
    for _ in range(2):
        with pytest.raises(Unreachable) as lost:
            router.structured("s", "u", SCHEMA, model="lan/qwen")
        assert not lost.value.down
    assert len(erring.requests) == 2

    # One call too slow loses only itself; a second in a row means too slow for this run.
    slow = []

    def timeout(request):
        slow.append(request)
        raise httpx.ReadTimeout("slow")

    router = Router({"lan": OpenAIBackend(LAN, httpx.Client(transport=httpx.MockTransport(timeout)))})
    downs = []
    for _ in range(3):
        with pytest.raises(Unreachable) as lost:
            router.structured("s", "u", SCHEMA, model="lan/qwen")
        downs.append(lost.value.down)
    assert downs == [False, True, True] and len(slow) == 2


def test_a_rate_limit_loses_one_call_not_the_connection():
    from app.llm import LlmError

    server = Server(status=429)
    router = Router({"lan": backend(server)})
    with pytest.raises(LlmError) as limited:
        router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert not isinstance(limited.value, (Unreachable, BadAnswer))
    with pytest.raises(LlmError):
        router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(server.requests) == 2, "tried again"


def test_only_a_server_on_this_network_can_be_free():
    from app.llm import private_host

    for address in (
        "http://192.168.1.20:8080/v1",
        "http://10.0.0.5/v1",
        "http://localhost:11434/v1",
        "http://[::1]:8080/v1",
        "http://gpu-box:8000/v1",
        "http://studio.local/v1",
    ):
        assert private_host(address), address
    for address in ("https://openrouter.ai/api/v1", "https://api.openai.com/v1", "http://8.8.8.8/v1"):
        assert not private_host(address), address
    hosted = OpenAIBackend(Connection("router", "https://openrouter.ai/api/v1", free=True))
    assert hosted.priced, "ticked free, but on the internet: still counted"
    assert not OpenAIBackend(LAN).priced


def test_replies_that_are_not_objects_or_wrap_the_json_are_handled():
    class Listy(Server):
        def __call__(self, request):
            return httpx.Response(200, json=[])

    with pytest.raises(BadAnswer, match="not a JSON object"):
        backend(Listy()).structured("s", "u", SCHEMA, model="lan/qwen")
    odd_usage = reply('{"scores": []}')
    odd_usage["usage"] = []
    assert backend(Server([odd_usage])).structured("s", "u", SCHEMA, model="lan/qwen").input_tokens == 0
    # The opening <think> was in the prompt; prose before a fence.
    for content in ('which first?</think>\n{"scores": []}', 'Here you go:\n```json\n{"scores": []}\n```'):
        assert backend(Server([reply(content)])).structured("s", "u", SCHEMA, model="lan/qwen").data == {"scores": []}


def test_an_article_cut_to_fit_a_small_context_is_marked_as_cut():
    from app.read import Reading, _batches, _listing

    long = Reading("a", "A", "S", 2000, " ".join(["word"] * 2000))
    (batch,) = _batches([long], limit=1000)
    listed = json.loads(_listing(batch, 2500).removeprefix("<reading>\n").removesuffix("\n</reading>"))
    assert listed[0]["truncated"] is True and listed[0]["words"] == 2000
    whole = Reading("b", "B", "S", 10, " ".join(["word"] * 10))
    listed = json.loads(_listing([whole], 2500).removeprefix("<reading>\n").removesuffix("\n</reading>"))
    assert listed[0]["truncated"] is False


def test_a_tailnet_server_can_be_free():
    from app.llm import private_host

    assert private_host("http://100.101.102.103:8080/v1") and private_host("http://gpu.tail1234.ts.net/v1")


def test_which_failures_take_a_connection_out_for_the_run():
    def raising(exc):
        def handler(request):
            raise exc

        return OpenAIBackend(LAN, httpx.Client(transport=httpx.MockTransport(handler)))

    for exc, down in (
        (httpx.ConnectTimeout("dropped"), True),
        (httpx.ConnectError("refused"), True),
        (httpx.ReadTimeout("slow"), False),
        (httpx.RemoteProtocolError("cut"), False),
    ):
        with pytest.raises(Unreachable) as failed:
            raising(exc).structured("s", "u", SCHEMA, model="lan/qwen")
        assert failed.value.down is down, type(exc).__name__
    with pytest.raises(Unreachable) as refused_key:
        backend(Server(status=401)).structured("s", "u", SCHEMA, model="lan/qwen")
    assert refused_key.value.down
    with pytest.raises(Unreachable) as forbidden:
        backend(Server(status=403)).structured("s", "u", SCHEMA, model="lan/qwen")
    assert not forbidden.value.down, "a 403 is often about the request, such as moderation"
    # Once down, every later call says so, flagged down too.
    router = Router({"lan": raising(httpx.ConnectError("refused"))})
    for _ in range(2):
        with pytest.raises(Unreachable) as again:
            router.structured("s", "u", SCHEMA, model="lan/qwen")
        assert again.value.down


def test_a_pass_stops_after_two_calls_in_a_row_that_could_not_reach_the_model(tmp_path):
    from app.config import Source, User
    from app.rank import rank
    from app.sources import Item

    slow = []

    def timeout(request):
        slow.append(request)
        raise httpx.ReadTimeout("slow")

    router = Router({"lan": OpenAIBackend(LAN, httpx.Client(transport=httpx.MockTransport(timeout)))})
    items = [Item(f"i{n}", f"https://x/{n}", Source("S"), f"T{n}", "", None, "") for n in range(10)]
    ranking = rank(
        Llm(model="lan/qwen", batch_size=2),
        router,
        Store(tmp_path / "f.sqlite3"),
        User("john", "John", ()),
        items,
        slots=3,
        today_start="2026-10-17",
    )
    assert len(slow) == 2, "stopped after two of the five batches: too slow for this run"
    assert ranking.choices == {} and "model call failed" in ranking.note, "newest first, saying why"


def test_the_start_of_run_check_sees_the_whole_model_list():
    many = [f"m{n}" for n in range(600)]
    assert Router({"lan": backend(Server(models=many))}).check("lan/m550") == ""


def test_two_timeouts_in_a_row_end_the_reading_pass_too_and_only_timeouts_count(tmp_path):
    from app.config import User
    from app.read import Reading, read

    slow = []

    def timeout(request):
        slow.append(request)
        raise httpx.ReadTimeout("slow")

    router = Router({"lan": OpenAIBackend(LAN, httpx.Client(transport=httpx.MockTransport(timeout)))})
    readings = [Reading(f"r{n}", f"T{n}", "S", 900, " ".join(["word"] * 900)) for n in range(30)]
    store = Store(tmp_path / "f.sqlite3")
    assert read(Llm(model="lan/qwen"), router, store, User("john", "John", ()), readings, "2026-10-17") == {}
    assert len(slow) == 2, "three batches of ten: stopped after two"
    with pytest.raises(Unreachable, match="timed out twice in a row"):
        router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(slow) == 2, "and the rest of the run does not try it"

    # Server errors are not slowness: each call is tried.
    erring = Server(status=503)
    router = Router({"lan": backend(erring)})
    for _ in range(3):
        with pytest.raises(Unreachable):
            router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(erring.requests) == 3


def test_an_article_cut_to_fit_leaves_room_for_its_links():
    from app.read import CHARS_PER_TOKEN, TOKENS_PER_ARTICLE, Reading, _batches, _tokens

    links = [{"site": "example.com", "text": "the paper " * 5, "context": "as shown in " * 20} for _ in range(8)]
    long = Reading("a", "A", "S", 5000, " ".join(["word"] * 5000), links)
    ((cut,),) = _batches([long], limit=3000)
    sent = _tokens(cut.text) + len(json.dumps(cut.links)) // CHARS_PER_TOKEN + TOKENS_PER_ARTICLE
    assert sent <= 3000 and len(cut.links) == 8


def test_connections_are_set_in_the_environment():
    found, problems = connections_from_env(
        {
            "FORENOON_LLM_LAN_URL": "http://192.168.1.20:8888/v1",
            "FORENOON_LLM_LAN_KEY": "sk-lan",
            "FORENOON_LLM_LAN_CONTEXT": "65536",
            "FORENOON_LLM_OPENAI_URL": "https://api.openai.com/v1",
            "FORENOON_LLM_OPENAI_OUTPUT": "8192",
            "FORENOON_LLM_OPENAI_FREE": "0",
            "FORENOON_LLM_GPU_BOX_URL": "http://gpu-box:8000/v1",
            "FORENOON_LLM_GPU_BOX_REASONING": "1",
            "FORENOON_LLM_GPU_BOX_TIMEOUT": "900",
            "FORENOON_LLM_KEY_LAN": "an old name, ignored",
            "FORENOON_ANTHROPIC_API_KEY": "not a connection",
        }
    )
    assert problems == {}
    gpu, lan, openai = found
    assert (lan.name, lan.address, lan.key, lan.context_tokens, lan.output_tokens) == (
        "lan",
        "http://192.168.1.20:8888/v1",
        "sk-lan",
        65536,
        16384,
    )
    assert lan.free and lan.reasoning_off and lan.timeout == 300
    assert (openai.output_tokens, openai.free, openai.key) == (8192, False, "")
    assert (gpu.name, gpu.reasoning_off, gpu.timeout) == ("gpu_box", False, 900)
    assert "sk-lan" not in repr(lan), "the key is kept out of logs"


def test_a_connection_that_cannot_be_read_is_left_out_and_says_why():
    found, problems = connections_from_env(
        {
            "FORENOON_LLM_A_URL": "192.168.1.20:8080",
            "FORENOON_LLM_B_URL": "http://x:80o0/v1",
            "FORENOON_LLM_C_URL": "http://x/v1",
            "FORENOON_LLM_C_CONTEXT": "8000",
            "FORENOON_LLM_D_URL": "http://x/v1",
            "FORENOON_LLM_D_CONTEXT": "20000",
            "FORENOON_LLM_D_OUTPUT": "20000",
            "FORENOON_LLM_ANTHROPIC_URL": "http://x/v1",
            "FORENOON_LLM_OK_URL": "http://x/v1",
        }
    )
    assert [c.name for c in found] == ["ok"]
    assert sorted(problems) == ["a", "anthropic", "b", "c", "d"]
    assert problems["a"].startswith("FORENOON_LLM_A_URL: give the API's address")
    assert problems["c"].startswith("FORENOON_LLM_C_CONTEXT: give a whole number from 16384")
    assert problems["d"].startswith("FORENOON_LLM_D_OUTPUT must be less than FORENOON_LLM_D_CONTEXT")
    assert problems["anthropic"].startswith("FORENOON_LLM_ANTHROPIC_URL: the name")


def test_a_test_reports_each_way_a_model_can_fail():
    from app.model_test import test_model

    def router(server):
        return Router({"lan": backend(server)})

    assert test_model(router(Server([reply('{"word": "ready", "number": 7}')])), "lan/qwen").startswith(
        "OK: answered in"
    )
    assert "not as asked" in test_model(router(Server([reply('{"word": "hello", "number": 3}')])), "lan/qwen")
    cut = Server([reply("{", finish="length")])
    assert test_model(router(cut), "lan/qwen").startswith("Failed (the answer was unusable)")
    assert test_model(router(Server(status=503)), "lan/qwen").startswith("Failed (could not reach it)")
    # The server answers whatever it has loaded, but a run would not use a model it does not list.
    unlisted = Server([reply('{"word": "ready", "number": 7}')], models=["unsloth/Qwen3.8-27B-GGUF"])
    assert "lan does not list the model qwen" in test_model(router(unlisted), "lan/qwen")
    assert test_model(router(Server()), "gpu/qwen").startswith("Failed (not set up)")
    assert "set FORENOON_LLM_GPU_URL" in test_model(router(Server()), "gpu/qwen")


def test_with_no_connections_set_each_model_names_the_variable_it_needs(monkeypatch):
    from app import model_test

    monkeypatch.setattr(model_test, "backend_from_env", lambda llm: None)
    monkeypatch.setattr(model_test, "connections_from_env", lambda: ((), {}))
    monkeypatch.setattr(model_test, "unknown_settings", list)
    cfg = Llm(model="lan/qwen", fallback_model="anthropic/claude-haiku-5-5")
    lines = model_test.test_models(cfg)
    assert lines[0].endswith(": set FORENOON_LLM_LAN_URL") and "(not set up)" in lines[0]
    assert lines[1].endswith(": set FORENOON_ANTHROPIC_API_KEY")


def test_the_test_asked_for_on_the_page_is_run_and_answered(tmp_path, monkeypatch):
    from app import model_test

    store = _migrated(tmp_path)
    cfg = Llm(model="lan/qwen", fallback_model="anthropic/claude-haiku-5-5")
    assert not model_test.run_asked(store, cfg), "none asked for"
    store.db.execute("INSERT INTO model_tests (id, requested_at) VALUES (1, '2026-10-17T12:00:00Z')")
    store.db.commit()
    assert store.model_test_asked() == "2026-10-17T12:00:00Z"

    server = Server([reply('{"word": "ready", "number": 7}')])
    monkeypatch.delenv("FORENOON_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(model_test, "backend_from_env", lambda llm: Router({"lan": backend(server)}, {}))
    monkeypatch.setattr(
        model_test, "connections_from_env", lambda: ((), {"x": "FORENOON_LLM_X_URL: give the API's address"})
    )
    assert model_test.run_asked(store, cfg)
    row = store.db.execute("SELECT * FROM model_tests").fetchone()
    lines = row["result"].split("\n")
    assert row["answered"] == row["requested_at"] and row["tested_at"] and store.model_test_asked() == ""
    assert lines[0] == "Not read: FORENOON_LLM_X_URL: give the API's address"
    assert lines[1].startswith("lan/qwen (ranking and choosing): OK: answered in")
    assert lines[2] == (
        "anthropic/claude-haiku-5-5 (fallback): Failed (not set up): no connection 'anthropic'"
        " for the model anthropic/claude-haiku-5-5: set FORENOON_ANTHROPIC_API_KEY"
    )
    assert [r["purpose"] for r in store.llm_call_rows()] == ["test"], "the call made is logged like any other"

    # Asked for again while one ran: the older answer does not count for it.
    store.db.execute("UPDATE model_tests SET requested_at = '2026-10-17T12:00:30Z', answered = ''")
    store.db.commit()
    store.record_model_test("2026-10-17T12:00:00Z", "an older answer")
    assert store.model_test_asked() == "2026-10-17T12:00:30Z", "the second is still to run"


def test_the_scheduler_tests_the_models_as_just_saved_not_its_own_day_old_settings(tmp_path, monkeypatch):
    from app import cli, model_test

    store = _migrated(tmp_path)
    store.db.execute("INSERT INTO settings (key, value, updated_at) VALUES ('llm.model', 'lan/qwen', 'now')")
    store.db.execute("INSERT INTO model_tests (id, requested_at) VALUES (1, '2026-10-17T12:00:00+00:00')")
    store.db.commit()
    store.close()
    monkeypatch.setenv("FORENOON_DATA_DIR", str(tmp_path))
    tested = []
    monkeypatch.setattr(
        model_test, "test_models", lambda llm, store=None, progress=None: tested.append(llm.model) or ["ok"]
    )
    stale = cli.load()
    stale = cli.dataclasses.replace(stale, llm=replace(stale.llm, model="anthropic/claude-haiku-5-5"))
    assert cli._test_models(stale) and tested == ["lan/qwen"]
    assert not cli._test_models(stale), "answered: nothing more to run"


def test_connection_names_are_strict_and_problems_belong_to_their_own_connection():
    found, problems = connections_from_env(
        {
            "FORENOON_LLM_lan_URL": "http://x/v1",
            "FORENOON_LLM_LAN__URL": "http://x/v1",
            "FORENOON_LLM__X_URL": "http://x/v1",
            "FORENOON_LLM_URL": "http://x/v1",
            "FORENOON_LLM_EMPTY_URL": "  ",
            "FORENOON_LLM_LAN_BOX_URL": "http://x/v1",
            "FORENOON_LLM_LAN_BOX_CONTEXT": "8000",
        }
    )
    assert found == () and len(problems) == 5, "an empty address is not set, the rest are named"
    assert set(problems) == {"lan", "lan_", "_x", "FORENOON_LLM_URL", "lan_box"}
    router = Router({}, problems)
    assert "FORENOON_LLM_lan_URL: the name must be capital" in router.check("lan/qwen"), "the likely mistake"
    assert "LAN_BOX" not in router.check("lan/qwen"), "LAN_BOX's problem is not LAN's"
    assert "FORENOON_LLM_LAN_BOX_CONTEXT" in router.check("lan_box/qwen")


def test_a_refused_key_is_not_quoted_back():
    class Refusing(Server):
        def __call__(self, request):
            return httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-ab***xyz"}})

    with pytest.raises(Unreachable) as refused:
        backend(Refusing()).structured("s", "u", SCHEMA, model="lan/qwen")
    assert "sk-ab" not in str(refused.value) and "the key was refused" in str(refused.value)


def test_a_bad_env_line_reaches_the_run_even_with_no_other_connection(monkeypatch):
    from app.llm import backend_from_env

    for var in [v for v in __import__("os").environ if v.startswith("FORENOON_LLM_")]:
        monkeypatch.delenv(var)
    monkeypatch.delenv("FORENOON_ANTHROPIC_API_KEY", raising=False)
    assert backend_from_env(Llm()) is None, "nothing set: newest first, as before"
    monkeypatch.setenv("FORENOON_LLM_LAN_URL", "192.168.1.20:8080/v1")
    router = backend_from_env(Llm(model="lan/qwen"))
    with pytest.raises(Unreachable, match="FORENOON_LLM_LAN_URL: give the API's address"):
        router.structured("s", "u", SCHEMA, model="lan/qwen")


def test_a_misspelt_or_left_over_setting_is_said_but_stops_nothing():
    from app.llm import unknown_settings

    env = {
        "FORENOON_LLM_LAN_URL": "http://x/v1",
        "FORENOON_LLM_LAN_TIMOUT": "900",
        "FORENOON_LLM_LAN_BOX_KEY": "left when its address was taken out",
        "FORENOON_LLM_GPU_URL": "http://x/v1",
        "FORENOON_LLM_GPU_CONTEXT": "65536",
        "FORENOON_LLM_PW_URL": "http://me:secret@x/v1",
    }
    found, problems = connections_from_env(env)
    assert [c.name for c in found] == ["gpu", "lan"], "a stray variable stops no connection"
    assert unknown_settings(env) == ["FORENOON_LLM_LAN_BOX_KEY", "FORENOON_LLM_LAN_TIMOUT"]
    assert unknown_settings({**env, "FORENOON_LLM_OLD_KEY": ""}) == unknown_settings(env), "empty is not set"
    assert "no user name or password" in problems["pw"] and "secret" not in problems["pw"]


def test_a_test_asked_for_again_stops_the_one_running_and_one_that_breaks_is_answered(tmp_path, monkeypatch):
    from app import model_test

    store = _migrated(tmp_path)
    store.db.execute("INSERT INTO model_tests (id, requested_at) VALUES (1, '2026-10-17T12:00:00+00:00')")
    store.db.commit()
    seen = []

    def models(cfg, store_, progress):
        progress(["lan/qwen (ranking): OK"])
        row = store.db.execute("SELECT started, answered, result FROM model_tests").fetchone()
        seen.append(tuple(row))
        # Asked for again, as the web page does, while this one runs.
        store.db.execute(
            "UPDATE model_tests SET requested_at = '2026-10-17T12:00:09+00:00', answered = '', started = '', result = ''"
        )
        store.db.commit()
        progress(["lan/qwen (ranking): OK", "anthropic/claude-haiku-5-5 (fallback): OK"])
        raise AssertionError("not reached: the next model is not asked")

    monkeypatch.setattr(model_test, "test_models", models)
    assert model_test.run_asked(store, Llm())
    assert seen == [("2026-10-17T12:00:00+00:00", "", "lan/qwen (ranking): OK")], "lines shown as they come"
    assert store.model_test_asked() == "2026-10-17T12:00:09+00:00", "the new one is next, untouched"
    assert store.db.execute("SELECT result FROM model_tests").fetchone()[0] == ""

    def broken(cfg, store_, progress):
        raise RuntimeError("disk full")

    monkeypatch.setattr(model_test, "test_models", broken)
    assert model_test.run_asked(store, Llm())
    assert store.model_test_asked() == "", "answered, so not run again every few seconds"
    assert (
        "The test could not finish: RuntimeError: disk full"
        in store.db.execute("SELECT result FROM model_tests").fetchone()[0]
    )


def test_the_anthropic_test_is_bounded_and_leaves_the_runs_client_alone():
    from app.llm import AnthropicBackend

    run = AnthropicBackend(Llm(timeout=60.0), "sk-ant-test")
    quick = run.bounded(120.0)
    assert quick._client.timeout == 60.0 and quick._client.max_retries == 0, "never longer than a run waits"
    assert run.bounded(30.0)._client.timeout == 30.0
    assert run._client.max_retries == 1 and run._client.timeout == 60.0


def test_the_schedulers_look_for_a_test_needs_no_table_and_makes_no_database(tmp_path, monkeypatch):
    from app import cli

    odd = tmp_path / "100% sure? #1"
    odd.mkdir()
    monkeypatch.setenv("FORENOON_DATA_DIR", str(odd))
    cfg = cli.from_env()
    assert not cli._test_models(cfg) and not cfg.db_path.exists(), "no database: none made"
    Store(cfg.db_path).close()
    assert not cli._test_models(cfg), "no model_tests table yet"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["100% sure? #1"], "nothing made at a cut-short path"


def test_the_reading_pass_sees_a_long_articles_ending_and_is_not_fooled_by_the_gap():
    import json as _json

    from app.read import Reading, _listing, _validated

    long_one = Reading("a", "Long", "Blog", 5000, "word " * 2500, [], "and that is how it ends.")
    cut_one = Reading("b", "Cut", "Blog", 5000, "word " * 2500, [], "and then the text stops in the mid")
    whole = Reading("c", "Short", "Blog", 300, "word " * 300, [])
    listed = _json.loads(_listing([long_one, whole], 30_000).split("\n")[1])
    assert listed[0]["ending"] == "and that is how it ends." and "ending" not in listed[1]
    answer = {
        "articles": [
            {
                "id": i,
                "score": 5,
                "reason": "r",
                "complete": "suspect",
                "problem": "Text is truncated mid-article.",
                "links": [],
            }
            for i in ("a", "b", "c")
        ]
    }
    verdicts = _validated(answer, [long_one, cut_one, whole])
    # The gap between beginning and ending is not a cut; an ending that stops mid-word is.
    assert verdicts["a"].complete == "ok" and verdicts["a"].problem == ""
    assert verdicts["b"].complete == "suspect" and verdicts["c"].complete == "suspect"
