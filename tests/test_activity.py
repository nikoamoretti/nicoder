"""Turn OpenCode tool parts into a sentence a person can trust."""

from local_coder.general.activity import (
    activity_from_event,
    activity_from_messages,
    concrete_actions_from_messages,
    describe_tool,
    is_vague_activity,
)


def test_describe_bash_uses_the_command():
    assert describe_tool("bash", {
        "status": "running",
        "input": {"command": "ls -la && git status | head -5"},
        "title": "ls -la && git status | head -5",
    }) == "Running: ls -la && git status | head -5"


def test_describe_read_uses_short_path():
    phrase = describe_tool("read", {
        "status": "completed",
        "input": {"filePath": "/tmp/candidate/src/App.tsx"},
    })
    assert phrase == "Read App.tsx"


def test_activity_from_event_reads_nested_part():
    event = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {"status": "running", "input": {"command": "npm install"}},
            }
        },
    }
    assert activity_from_event(event) == "Running: npm install"


def test_activity_from_messages_when_waiting_for_tools():
    messages = [
        {"info": {"role": "assistant", "time": {"created": 1}}, "parts": []},
    ]
    phrase = activity_from_messages(messages)
    assert "tool call" in phrase.lower()
    assert is_vague_activity(phrase)


def test_activity_from_messages_uses_latest_tool():
    messages = [
        {
            "info": {"role": "assistant", "time": {"completed": 2}},
            "parts": [{
                "type": "tool",
                "tool": "read",
                "state": {"status": "completed", "input": {"filePath": "src/main.tsx"}},
            }],
        }
    ]
    assert activity_from_messages(messages) == "Read src/main.tsx"


def test_concrete_actions_lists_tools_in_order():
    messages = [
        {
            "info": {"role": "assistant"},
            "parts": [
                {"type": "tool", "tool": "read",
                 "state": {"status": "completed", "input": {"filePath": "package.json"}}},
                {"type": "tool", "tool": "bash",
                 "state": {"status": "running", "input": {"command": "npm run build"}}},
            ],
        }
    ]
    assert concrete_actions_from_messages(messages) == [
        "Read package.json",
        "Running: npm run build",
    ]
