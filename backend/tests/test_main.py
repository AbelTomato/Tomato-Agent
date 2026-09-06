from app.main import app, health


def test_fastapi_app_is_importable():
    assert app.title == "Tomato Agent Infrastructure"


async def test_health_response():
    assert await health() == {"status": "ok", "runtime": "user-implemented"}
