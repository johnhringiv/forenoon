"""The OpenAI-compatible connections: the request, the answer, the errors, the run's choice of
model and the Settings page's checks. The servers are httpx mock transports."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.config import Llm
from app.connections import look_after
from app.llm import (
    BadAnswer,
    Connection,
    OpenAIBackend,
    Router,
    Unreachable,
    input_budget,
    key_env,
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
    return OpenAIBackend(connection, key, server.client())


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

    broken = OpenAIBackend(LAN, "", httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(Unreachable, match="ConnectError"):
        broken.structured("s", "u", SCHEMA, model="lan/qwen")
    for status, kind in ((401, Unreachable), (503, Unreachable), (400, BadAnswer)):
        with pytest.raises(kind, match=f"HTTP {status}"):
            backend(Server(status=status)).structured("s", "u", SCHEMA, model="lan/qwen")
    assert issubclass(Unreachable, Exception) and key_env("lan-box") == "FORENOON_LLM_KEY_LAN_BOX"


def test_a_model_is_checked_by_its_connections_model_list():
    router = Router({"lan": backend(Server(models=["qwen"]))})
    assert router.check("lan/qwen") == ""
    assert "does not list the model gemma" in router.check("lan/gemma")
    assert "no connection named 'other'" in router.check("other/qwen")
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
    store.db.execute(
        "INSERT INTO connections (name, address, context_tokens, output_tokens, free, created_at)"
        " VALUES ('lan', 'http://192.168.1.20:8080/v1', 65536, 8192, 1, '2026-10-17T00:00:00+00:00')"
    )
    store.db.commit()
    return store


def test_connections_come_from_the_settings_pages_table(tmp_path):
    assert Store(tmp_path / "empty.sqlite3").connections() == (), "the web app has not run yet"
    store = _migrated(tmp_path)
    (lan,) = store.connections()
    assert (lan.name, lan.context_tokens, lan.output_tokens, lan.free, lan.reasoning_off) == (
        "lan",
        65536,
        8192,
        True,
        True,
    )
    with pytest.raises(Exception, match="CHECK"):
        store.db.execute("INSERT INTO connections (name, address, created_at) VALUES ('anthropic', 'x', 'now')")
    with pytest.raises(Exception, match="CHECK"):
        store.db.execute("INSERT INTO connections (name, address, created_at) VALUES ('Bad Name', 'x', 'now')")


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


def test_the_scheduler_checks_connections_and_runs_the_tests_asked_for(tmp_path, monkeypatch):
    store = _migrated(tmp_path)
    monkeypatch.setenv("FORENOON_LLM_KEY_LAN", "sk-lan")
    server = Server([reply('{"word": "ready", "number": 7}')], models=["qwen"])
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    assert look_after(store, now, server.client()) == 1
    (row,) = store.connection_rows()
    assert row["key_set"] == 1 and row["error"] == "" and json.loads(row["models"]) == ["qwen"]
    assert look_after(store, now, server.client()) == 0, "not due again for a while"

    # A test asked for on the page is run at once, on the first listed model when none is chosen.
    store.db.execute("UPDATE connections SET test_requested_at = '2026-10-17T12:01:00+00:00'")
    store.db.commit()
    assert look_after(store, now, server.client()) == 1
    (row,) = store.connection_rows()
    assert row["test_result"].startswith("OK: qwen answered in") and "matching the schema" in row["test_result"]
    assert store.connection_tests() == []

    # A failed check keeps the last model list, and says why.
    store.db.execute("UPDATE connections SET checked_at = ''")
    store.db.commit()
    look_after(store, now, Server(status=503).client())
    (row,) = store.connection_rows()
    assert "HTTP" in row["error"] or "503" in row["error"]
    assert json.loads(row["models"]) == ["qwen"]


def test_a_test_reports_an_answer_that_was_not_as_asked(tmp_path):
    from app.connections import test_connection

    wrong = Server([reply('{"word": "hello", "number": 3}')])
    assert "not as asked" in test_connection(backend(wrong), "qwen")
    cut = Server([reply("{", finish="length")])
    assert test_connection(backend(cut), "qwen").startswith("Failed (the answer was unusable)")
    assert test_connection(backend(Server(status=503)), "qwen").startswith("Failed (could not reach it)")


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


def test_a_test_asked_for_while_one_runs_is_not_answered_by_the_older_one(tmp_path):
    store = _migrated(tmp_path)
    store.db.execute("UPDATE connections SET test_requested_at = '2026-10-17T12:00:00+00:00'")
    store.db.commit()
    (asked,) = store.connection_tests()
    # Clicked again while the first ran.
    store.db.execute("UPDATE connections SET test_requested_at = '2026-10-17T12:00:30+00:00', test_result = ''")
    store.db.commit()
    store.record_connection_test("lan", "OK: first", asked["test_requested_at"])
    (row,) = store.connection_rows()
    assert row["test_result"] == "" and len(store.connection_tests()) == 1, "the second is still to run"


def test_a_connection_found_down_is_not_tried_again_in_the_run():
    server = Server(status=503)
    router = Router({"lan": backend(server)})
    assert router.check("lan/qwen").startswith("lan:")
    with pytest.raises(Unreachable, match="not tried again this run"):
        router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(server.requests) == 1, "only the start-of-run look reached it"

    flaky = Server(status=503)
    router = Router({"lan": backend(flaky)})
    for _ in range(3):
        with pytest.raises(Unreachable):
            router.structured("s", "u", SCHEMA, model="lan/qwen")
    assert len(flaky.requests) == 1, "the first failed call is the last"


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


def test_a_check_does_not_overwrite_an_address_saved_while_it_ran(tmp_path):
    store = _migrated(tmp_path)
    store.record_connection_check("lan", "http://old/v1", True, "", ["qwen"], "2026-10-17T12:00:00+00:00")
    (row,) = store.connection_rows()
    assert row["checked_at"] == "" and row["models"] == "[]", "the row's address had changed"
