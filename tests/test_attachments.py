"""Files and images attached to a message (Nico, 2026-10-26: "accept files and images and be able to process them").

The app stages each file under `<data root>/attachments/<id>/` with the text macOS took from it and, for an image, a
smaller copy. The model reads about every file, sees the images when it can, and the chat keeps the names.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from local_coder import attachments as attached
from local_coder.agent.loop import AgentSession, _size
from local_coder.agent.tools import AccessPolicy, ToolBox
from local_coder.app_service import chat_request
from local_coder.general.chat_log import load_chat, load_free_chat
from local_coder.general.coordinator import run_general_request

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _stage(name: str, data: bytes, *, folder: str = "a1") -> Path:
    path = attached.attachments_dir() / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def files():
    shot = _stage("screenshot.png", PNG)
    small = _stage("screenshot.preview.jpg", b"\xff\xd8\xffsmall")
    words = _stage("screenshot.png.txt", b"Total crimes 2025")
    pdf = _stage("report.pdf", b"%PDF-1.7 ...")
    pdf_text = _stage("report.pdf.txt", b"Interstat annual report")
    csv = _stage("data.csv", b"commune,count\nBandol,12\n")
    return [
        {"path": str(shot), "name": "screenshot.png", "kind": "image", "text": str(words), "preview": str(small)},
        {"path": str(pdf), "name": "report.pdf", "kind": "pdf", "text": str(pdf_text)},
        {"path": str(csv), "name": "data.csv", "kind": "file"},
    ]


def test_only_files_the_app_staged_are_taken(files, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    rows = attached.parse([*files, {"path": str(outside), "name": "secret.txt"}, {"path": "/etc/hosts"}, "x"])
    assert [row.name for row in rows] == ["screenshot.png", "report.pdf", "data.csv"]
    assert rows[0].preview is not None and rows[1].text is not None


def test_the_model_reads_about_every_file(files):
    rows = attached.parse(files)
    shown = attached.note(rows, shown=True)
    assert "screenshot.png: an image at" in shown and "shown to you with this message" in shown
    assert "the words macOS read in it:" in shown
    assert "report.pdf: a PDF at" in shown and "its text:" in shown
    assert "data.csv:" in shown
    assert "this model cannot see images" in attached.note(rows, shown=False)


def test_images_go_as_data_urls_from_the_smaller_copy(files):
    urls = attached.image_urls(attached.parse(files))
    assert len(urls) == 1 and urls[0].startswith("data:image/jpeg;base64,")


def test_the_chat_keeps_the_names(files):
    rows = attached.parse(files)
    expected = "What is this?\n\nattached: screenshot.png, report.pdf, data.csv"
    assert attached.turn_text("What is this?", rows) == expected
    assert attached.turn_text("  ", rows[:1]) == "attached: screenshot.png"


def test_only_models_that_see_images_are_shown_them(monkeypatch):
    monkeypatch.setattr("local_coder.orcarouter.find_model",
                        lambda model_id: {"id": model_id, "vision": model_id == "qwen/qwen3.8-27b"})
    assert attached.sees_images("qwen/qwen3.8-27b", "orcarouter") is True
    assert attached.sees_images("text/only", "orcarouter") is False
    assert attached.sees_images("nemotron35-super", "nvidia") is False


def test_a_message_with_images_is_a_list_and_its_transcript_leaves_the_data_out(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    seen: list[list[dict]] = []

    def chat(messages, tools):
        seen.append(json.loads(json.dumps(messages)))
        return {"choices": [{"message": {"role": "assistant", "content": "A one-pixel image."}}]}

    box = ToolBox(AccessPolicy(home=home), cwd=home, run_tmp=tmp_path / "tmp")
    session = AgentSession(chat=chat, toolbox=box, system="s")
    url = "data:image/png;base64," + "A" * 50_000
    assert session.ask("What is in it?", images=[url]).answer == "A one-pixel image."
    sent = seen[0][-1]["content"]
    assert sent[0] == {"type": "text", "text": "What is in it?"}
    assert sent[1]["image_url"]["url"] == url
    assert _size(seen[0][-1]) < 10_000  # an image counts as a fixed size, not its data
    saved = json.loads(session.save(tmp_path / "t.json").read_text())
    assert "AAAA" not in json.dumps(saved) and "(image left out)" in json.dumps(saved)


def _fake_llm(monkeypatch, reply: str) -> dict[str, object]:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": reply}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("local_coder.app_service.ensure_server", lambda wait_s=90: {"ok": True})
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return captured


def test_a_question_with_files_tells_the_model_and_keeps_the_names(files, monkeypatch):
    captured = _fake_llm(monkeypatch, "Bandol has 12.")
    result = chat_request(None, "", chat_id="chat-files-0001", attachments=files)
    assert result["summary"] == "Bandol has 12."
    sent = json.dumps(captured["json"])
    assert "Look at the attached files." in sent and "data.csv" in sent and "report.pdf.txt" in sent
    turns = load_free_chat("chat-files-0001")["turns"]
    assert turns[0]["text"] == "attached: screenshot.png, report.pdf, data.csv"


def test_a_change_with_an_image_shows_it_to_a_model_that_sees(files, tmp_path, monkeypatch):
    project = tmp_path / "web"
    (project / "src").mkdir(parents=True)
    (project / "package.json").write_text('{"name":"web"}\n', encoding="utf-8")
    monkeypatch.setattr("local_coder.attachments.sees_images", lambda model_id, provider: True)
    seen: list[list[dict]] = []

    def agent(messages, tools):
        seen.append(json.loads(json.dumps(messages)))
        return {"choices": [{"message": {"role": "assistant", "content": "It shows a table; nothing to change."}}]}

    run_general_request(project, "Make the page look like this screenshot", request_id="req-img", agent_chat=agent,
                        start_preview_after=False, attachments=files[:1])
    first = next(message for message in seen[0] if message["role"] == "user" and isinstance(message["content"], list))
    assert first["content"][0]["text"].startswith("Make the page look like this screenshot")
    assert first["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert load_chat(project)["turns"][0]["text"].endswith("attached: screenshot.png")
