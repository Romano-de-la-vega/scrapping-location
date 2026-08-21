import json
from datetime import datetime, timedelta, timezone

from watcher import dispatcher


def test_parse_interval_minutes():
    assert dispatcher.parse_interval_minutes("every 5m") == 5
    assert dispatcher.parse_interval_minutes("10 minutes") == 10
    assert dispatcher.parse_interval_minutes("every 2h") == 120


def test_is_due_without_next_run():
    manifest = {"enabled": True, "schedule": "every 5m"}
    assert dispatcher.is_due(manifest, {"next_run_at": None}, datetime.now(timezone.utc))


def test_is_due_when_future():
    now = datetime.now(timezone.utc)
    manifest = {"enabled": True, "schedule": "every 5m"}
    state = {"next_run_at": (now + timedelta(minutes=5)).isoformat()}
    assert not dispatcher.is_due(manifest, state, now)


def test_disabled_follow_is_never_due():
    assert not dispatcher.is_due(
        {"enabled": False, "schedule": "every 5m"},
        {"next_run_at": None},
        datetime.now(timezone.utc),
    )


def test_dispatcher_status_reads_independent_follows(tmp_path):
    root = tmp_path / "follows"
    for follow_id, chat_id in (("paul", "111"), ("alice", "222")):
        follow_dir = root / follow_id
        follow_dir.mkdir(parents=True)
        (follow_dir / "follow.json").write_text(
            json.dumps(
                {
                    "id": follow_id,
                    "name": follow_id.title(),
                    "enabled": True,
                    "schedule": "every 5m",
                    "notify_every_run": False,
                    "run_llm_on_new": False,
                    "telegram": {"chat_id": chat_id},
                }
            ),
            encoding="utf-8",
        )

    result = dispatcher.dispatcher_status(root)
    assert result["registered"] == 2
    assert {item["follow_id"] for item in result["follows"]} == {"paul", "alice"}


def test_chunks_preserve_short_messages():
    assert dispatcher._chunks("hello") == ["hello"]
