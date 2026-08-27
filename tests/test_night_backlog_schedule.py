from pathlib import Path


ROOT = Path(__file__).parents[1]
SCHEDULE = "10 0,1,2,3,4,23 * * *"
OLD_SCHEDULE = "10 0,1,2,3,4,6,11,17,23 * * *"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_public_cron_example_is_night_only() -> None:
    cron = read("examples/cron.examples.md")
    assert SCHEDULE in cron
    assert OLD_SCHEDULE not in cron
    assert "Do not add 06:10, 11:10, or 17:10 routine runs" in cron


def test_packaged_guidance_matches_public_schedule() -> None:
    skill = read("skill/openclaw-discord-server-backup/SKILL.md")
    install = read(
        "skill/openclaw-discord-server-backup/references/customer-install.md"
    )
    for content in (skill, install):
        assert SCHEDULE in content
        assert OLD_SCHEDULE not in content


def test_worker_limits_remain_bounded() -> None:
    cron = read("examples/cron.examples.md")
    command = (
        "--max-entries 4 --max-batches 12 "
        "--max-batches-per-entry 5 --limit 100"
    )
    assert command in cron
    assert "instead of increasing batch limits or starting a daytime worker" in cron
