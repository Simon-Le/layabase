import pytest
import sqlalchemy

import layabase


@pytest.fixture
def controller():
    class TestTable:
        __tablename__ = "test"

        key = sqlalchemy.Column(
            sqlalchemy.String,
            primary_key=True,
            info={"layabase": {"interpret_star_character": True}},
        )

    controller = layabase.CRUDController(TestTable)
    layabase.load("sqlite:///:memory:", [controller])
    return controller


def test_get_like_operator_double_star(controller):
    controller.post_many([{"key": "my_key"}, {"key": "my_key2"}, {"key": "my_ey"}])
    assert [{"key": "my_key"}, {"key": "my_key2"}] == controller.get({"key": "*y_k*"})


def test_get_like_operators_and_values(controller):
    controller.post_many(
        [
            {"key": "my_key"},
            {"key": "my_key2"},
            {"key": "other"},
            {"key": "totoro"},
            {"key": "totoro2"},
        ]
    )
    assert controller.get({"key": ["my*", "totoro", "*th*"]}) == [
        {"key": "my_key"},
        {"key": "my_key2"},
        {"key": "other"},
        {"key": "totoro"},
    ]


def test_get_like_operator_star_at_start(controller):
    controller.post_many(
        [{"key": "my_key"}, {"key": "my_key2"}, {"key": "my_ey"}, {"key": "my_k"}]
    )
    assert [{"key": "my_k"}] == controller.get({"key": "*y_k"})


def test_get_like_operator_star_at_end(controller):
    controller.post_many(
        [
            {"key": "my_key"},
            {"key": "my_key2"},
            {"key": "my_ey"},
            {"key": "my_k"},
            {"key": "y_key"},
        ]
    )
    assert [{"key": "y_key"}] == controller.get({"key": "y_k*"})


def test_get_like_operator_no_star(controller):
    controller.post_many(
        [
            {"key": "my_key"},
            {"key": "my_key2"},
            {"key": "my_ey"},
            {"key": "my_k"},
            {"key": "y_key"},
        ]
    )
    assert [{"key": "my_key"}] == controller.get({"key": "my_key"})


def test_get_like_operator_no_star_no_result(controller):
    controller.post_many(
        [
            {"key": "my_key"},
            {"key": "my_key2"},
            {"key": "my_ey"},
            {"key": "my_k"},
            {"key": "y_key"},
        ]
    )
    assert [] == controller.get({"key": "y_k"})
