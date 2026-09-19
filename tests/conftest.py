import pytest

from field_router.models import Engineer, Request, Scenario, Skill, TimeWindow, Transport


def job(id, start=600, end=720, duration=30, skill=Skill.LOCAL, **kwargs):
    return Request(id=id, region="test", date="2026-08-17", address=f"Address {id}",
                   windows=[TimeWindow(start=start, end=end)], duration_minutes=duration,
                   skill=skill, **kwargs)


def engineer(id="e1", start=540, end=1080, skills=None, transport=Transport.CAR, **kwargs):
    return Engineer(id=id, name=id, region="test", shift=TimeWindow(start=start,end=end),
                    skills=skills or [Skill.LOCAL], transport=transport, **kwargs)


@pytest.fixture
def make_scenario():
    def make(jobs, engineers=None):
        return Scenario(id="test", region="test", date="2026-08-17", office_address="Office",
                        requests=jobs, engineers=engineers or [engineer()])
    return make
