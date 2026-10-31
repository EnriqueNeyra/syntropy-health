"""Saved conversations: names and pins stay as the person left them, the limit never takes a pinned one, search finds what was
said, and exports and imports carry all of it."""

from __future__ import annotations

import io
import json
import zipfile

from app.store import chats

SAID = [{"role": "user", "content": "How did I sleep?"}, {"role": "assistant", "content": "Well, about **7 hours**.", "by": "m · Ollama"}]


def _save(client, chat_id="c_0123456789ab", title="How did I sleep?", messages=SAID, **more):
    r = client.put(f"/api/ai/chats/{chat_id}", json={"title": title, "messages": messages, **more})
    assert r.status_code == 200, r.text
    return r.json()


def _pid(client) -> str:
    return client.get("/api/profiles").json()["profiles"][0]["id"]


def test_lists_only_add_fields_and_stay_newest_first(client):
    _save(client, "c_aaaaaaaaaaaa")
    _save(client, "c_bbbbbbbbbbbb")
    client.patch("/api/ai/chats/c_aaaaaaaaaaaa", json={"pinned": True})
    listed = client.get("/api/ai/chats").json()["chats"]
    # The phone apps read this list, and take the first as the latest whether or not an older one is pinned.
    assert [c["id"] for c in listed] == ["c_bbbbbbbbbbbb", "c_aaaaaaaaaaaa"]
    assert {"id", "title", "created_at", "updated_at", "messages"} <= set(listed[0]) and listed[0]["messages"] == 2
    assert (listed[0]["pinned"], listed[1]["pinned"], listed[0]["title_set"]) == (False, True, False)
    got = client.get("/api/ai/chats/c_aaaaaaaaaaaa").json()
    assert got["pinned"] is True and got["messages"][1]["by"] == "m · Ollama"


def test_a_saved_conversation_never_overwrites_a_title_the_person_chose(client):
    _save(client)
    before = client.get("/api/ai/chats").json()["chats"][0]["updated_at"]
    changed = client.patch("/api/ai/chats/c_0123456789ab", json={"title": "  Sleep notes  "}).json()
    assert changed["title"] == "Sleep notes" and changed["title_set"] is True
    assert changed["updated_at"] == before       # renaming doesn't move it up the list
    saved = _save(client, title="How did I sleep?", messages=[*SAID, {"role": "user", "content": "And last week?"}])
    assert saved["title"] == "Sleep notes"
    assert client.get("/api/ai/chats/c_0123456789ab").json()["title"] == "Sleep notes"


def test_a_title_made_from_the_first_question_can_change_and_one_chosen_before_saving_stays(client):
    _save(client)
    assert _save(client, title="A better first question")["title"] == "A better first question"
    assert _save(client, "c_cccccccccccc", title="Named early", title_set=True)["title_set"] is True
    assert _save(client, "c_cccccccccccc", title="From the question")["title"] == "Named early"


def test_changing_a_conversation_checks_what_it_is_given(client):
    _save(client)
    assert client.patch("/api/ai/chats/c_0123456789ab", json={"title": "   "}).status_code == 400
    assert client.patch("/api/ai/chats/c_0123456789ab", json={"title": "x" * 121}).status_code == 422
    assert client.patch("/api/ai/chats/c_nothere00000", json={"pinned": True}).status_code == 404
    assert client.get("/api/ai/chats/c_0123456789ab").json()["title"] == "How did I sleep?"


def test_someone_elses_conversation_cant_be_renamed_pinned_or_exported(client):
    pid = _pid(client)
    chats.save_chat(pid, "acc_someone_else", "c_theirs000000", "Theirs", SAID)
    assert client.patch("/api/ai/chats/c_theirs000000", json={"title": "Mine now", "pinned": True}).status_code == 404
    assert client.get("/api/ai/chats/c_theirs000000/export").status_code == 404
    assert chats.get_chat(pid, "acc_someone_else", "c_theirs000000")["title"] == "Theirs"


def test_pinned_conversations_are_never_pushed_out(client, monkeypatch):
    monkeypatch.setattr(chats, "MAX_CHATS", 5)
    for i in range(5):
        _save(client, f"c_{i:012d}", title=f"Chat {i}")
    client.patch("/api/ai/chats/c_000000000000", json={"pinned": True})    # the oldest, next out
    _save(client, "c_new000000000", title="Newer")
    left = {c["title"] for c in client.get("/api/ai/chats").json()["chats"]}
    assert "Chat 0" in left and "Chat 1" not in left and len(left) == 5


def test_a_new_conversation_is_kept_even_when_every_other_one_is_pinned(client, monkeypatch):
    monkeypatch.setattr(chats, "MAX_CHATS", 3)
    for i in range(3):
        _save(client, f"c_{i:012d}", title=f"Chat {i}")
        client.patch(f"/api/ai/chats/c_{i:012d}", json={"pinned": True})
    _save(client, "c_one000000000", title="One")
    assert "One" in {c["title"] for c in client.get("/api/ai/chats").json()["chats"]}
    _save(client, "c_two000000000", title="Two")
    left = {c["title"] for c in client.get("/api/ai/chats").json()["chats"]}
    assert left == {"Chat 0", "Chat 1", "Chat 2", "Two"}


def test_an_old_pinned_conversation_is_still_in_the_list_past_a_hundred(client):
    pid, account = _pid(client), client.get("/api/status").json()["account"]["id"]
    for i in range(130):
        chats.save_chat(pid, account, f"c_{i:012d}", f"Chat {i}", SAID)
    client.patch("/api/ai/chats/c_000000000000", json={"pinned": True})
    assert any(c["id"] == "c_000000000000" and c["pinned"] for c in client.get("/api/ai/chats").json()["chats"])


def test_search_finds_titles_and_what_was_said_and_not_field_names(client):
    _save(client, "c_aaaaaaaaaaaa", title="Glucose question", messages=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])
    _save(client, "c_bbbbbbbbbbbb", title="Labs", messages=[{"role": "user", "content": "What's my café glucose?"},
                                                            {"role": "assistant", "content": "Your **glucose** was 114 mg/dL\nin March."}])
    _save(client, "c_cccccccccccc", title="Sleep", messages=SAID)
    find = lambda q: client.get("/api/ai/chats", params={"q": q}).json()["chats"]
    assert [c["id"] for c in find("glucose")] == ["c_aaaaaaaaaaaa", "c_bbbbbbbbbbbb"]      # a title match first
    assert "snippet" not in find("glucose")[0] and "glucose" in find("glucose")[1]["snippet"]
    assert [c["id"] for c in find("GLUCOSE march")] == ["c_bbbbbbbbbbbb"]                  # every word, any case
    assert [c["id"] for c in find("cafe")] == ["c_bbbbbbbbbbbb"]                           # with or without the accent
    assert find("content") == [] and find("assistant") == [] and find("role") == []
    assert find("100%") == []
    assert len(client.get("/api/ai/chats", params={"q": "   "}).json()["chats"]) == 3       # nothing asked: the whole list
    assert client.get("/api/ai/chats", params={"q": "x" * 201}).status_code == 422


def test_search_only_sees_this_persons_and_this_accounts_conversations(client):
    pid = _pid(client)
    chats.save_chat(pid, "acc_someone_else", "c_theirs000000", "Private zebra", SAID)
    assert client.get("/api/ai/chats", params={"q": "zebra"}).json()["chats"] == []


def test_a_long_message_gives_a_snippet_around_the_word(client):
    long = "word " * 200 + "needle " + "word " * 200
    _save(client, messages=[{"role": "user", "content": "x"}, {"role": "assistant", "content": long}])
    snippet = client.get("/api/ai/chats", params={"q": "needle"}).json()["chats"][0]["snippet"]
    assert "needle" in snippet and snippet.startswith("…") and snippet.endswith("…") and len(snippet) <= 125


def test_a_conversation_exports_as_markdown(client):
    _save(client, title="Sleep, “notes”/2026: é")
    r = client.get("/api/ai/chats/c_0123456789ab/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"      # see export_chat
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment") and ".md" in disposition and "/" not in disposition.split("filename*")[0]
    assert r.text.startswith("# Sleep, “notes”/2026: é\n")
    assert "## You\n\nHow did I sleep?" in r.text and "## m · Ollama\n\nWell, about **7 hours**." in r.text
    assert "doesn't give medical advice" in r.text


def test_a_title_in_another_script_still_makes_a_file_name(client):
    _save(client, title="睡眠について")
    r = client.get("/api/ai/chats/c_0123456789ab/export")
    assert r.status_code == 200 and 'filename="Conversation.md"' in r.headers["content-disposition"]
    assert "filename*=UTF-8''%E7%9D%A1" in r.headers["content-disposition"]


def test_an_archive_carries_names_and_pins(client):
    from tests.test_exports import _bring_in as bring_in, _other
    pid, target = _pid(client), _other(client)
    _save(client)
    client.patch("/api/ai/chats/c_0123456789ab", json={"title": "Chosen", "pinned": True})
    raw = client.get("/api/export/archive", params={"profile": pid}).content
    assert bring_in(client, raw, target)["chats"] == 1
    moved = client.get("/api/ai/chats", params={"profile": target}).json()["chats"][0]
    assert (moved["title"], moved["pinned"], moved["title_set"]) == ("Chosen", True, True)


def test_an_older_archive_still_comes_in(client):
    from tests.test_exports import _bring_in as bring_in, _other
    pid, target = _pid(client), _other(client)
    _save(client)
    raw = client.get("/api/export/archive", params={"profile": pid}).content
    old = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as z, zipfile.ZipFile(old, "w") as out:
        for item in z.namelist():
            data = z.read(item)
            if item == "chats.ndjson":
                rows = [json.loads(line) for line in data.decode().splitlines()]
                for row in rows:
                    del row["pinned"], row["title_set"]
                data = "\n".join(json.dumps(r) for r in rows).encode()
            out.writestr(item, data)
    assert bring_in(client, old.getvalue(), target)["chats"] == 1
    moved = client.get("/api/ai/chats", params={"profile": target}).json()["chats"][0]
    assert (moved["pinned"], moved["title_set"]) == (False, False)
    # A title from before this is one made from the first question, so a later save can still change it.
    assert _save(client, moved["id"] if moved["id"].startswith("c_") else "c_0123456789ab", title="Newer")["title"] == "Newer"


def test_a_pinned_conversation_comes_in_even_when_the_person_is_full(client, monkeypatch):
    from tests.test_exports import _bring_in as bring_in, _other
    pid, target = _pid(client), _other(client)
    account = client.get("/api/status").json()["account"]["id"]
    _save(client)
    client.patch("/api/ai/chats/c_0123456789ab", json={"pinned": True})
    _save(client, "c_bbbbbbbbbbbb", title="Not pinned")
    monkeypatch.setattr(chats, "MAX_CHATS", 2)
    for i in range(2):
        chats.save_chat(target, account, f"c_t{i:011d}", f"T{i}", SAID)
    result = bring_in(client, client.get("/api/export/archive", params={"profile": pid}).content, target)
    assert (result["chats"], result["chats_skipped"]) == (1, 1)
    assert any(c["pinned"] for c in client.get("/api/ai/chats", params={"profile": target}).json()["chats"])
