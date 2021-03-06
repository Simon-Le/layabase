import sqlalchemy
import pytest

import layabase
from layabase.testing import mock_sqlalchemy_health_datetime


@pytest.fixture
def controller():
    class TestTable:
        __tablename__ = "test"

        key = sqlalchemy.Column(sqlalchemy.String, primary_key=True)

    return layabase.CRUDController(TestTable)


@pytest.fixture
def disconnected_database(controller: layabase.CRUDController):
    _db = layabase.load("sqlite:///:memory:", [controller])
    _db.metadata.bind.dispose()
    yield _db


def test_get_all_when_db_down(
    disconnected_database, controller: layabase.CRUDController
):
    with pytest.raises(Exception) as exception_info:
        controller.get({})
    assert str(exception_info.value) == "Database could not be reached."


def test_get_when_db_down(disconnected_database, controller: layabase.CRUDController):
    with pytest.raises(Exception) as exception_info:
        controller.get_one({})
    assert str(exception_info.value) == "Database could not be reached."


def test_add_when_db_down(disconnected_database, controller: layabase.CRUDController):
    with pytest.raises(Exception) as exception_info:
        controller.post({"key": "my_key1", "mandatory": 1, "optional": "my_value1"})
    assert str(exception_info.value) == "Database could not be reached."


def test_post_many_when_db_down(
    disconnected_database, controller: layabase.CRUDController
):
    with pytest.raises(Exception) as exception_info:
        controller.post_many(
            [
                {"key": "my_key1", "mandatory": 1, "optional": "my_value1"},
                {"key": "my_key2", "mandatory": 1, "optional": "my_value1"},
            ]
        )
    assert str(exception_info.value) == "Database could not be reached."


def test_update_when_db_down(
    disconnected_database, controller: layabase.CRUDController
):
    with pytest.raises(Exception) as exception_info:
        controller.put({"key": "my_key1", "mandatory": 1, "optional": "my_value1"})
    assert str(exception_info.value) == "Database could not be reached."


def test_remove_when_db_down(
    disconnected_database, controller: layabase.CRUDController
):
    with pytest.raises(Exception) as exception_info:
        controller.delete({})
    assert str(exception_info.value) == "Database could not be reached."


def test_health_details_failure(
    disconnected_database, mock_sqlalchemy_health_datetime, monkeypatch
):
    monkeypatch.setattr(
        disconnected_database.metadata.bind.dialect, "do_ping", lambda x: False
    )
    assert layabase.check(disconnected_database) == (
        "fail",
        {
            "sqlite:select": {
                "componentType": "datastore",
                "status": "fail",
                "time": "2018-10-11T15:05:05.663979",
                "output": "Unable to ping database.",
            }
        },
    )


def test_health_details_failure_due_to_exception(
    disconnected_database, mock_sqlalchemy_health_datetime, monkeypatch
):
    def raise_exception(*args):
        raise Exception("This is the error")

    monkeypatch.setattr(
        disconnected_database.metadata.bind.dialect, "do_ping", raise_exception
    )
    assert layabase.check(disconnected_database) == (
        "fail",
        {
            "sqlite:select": {
                "componentType": "datastore",
                "status": "fail",
                "time": "2018-10-11T15:05:05.663979",
                "output": "This is the error",
            }
        },
    )
