"""查询接口 GET /api/papers 的契约测试。

真实目录用例检查页面真正会看到的全部结果；隔离目录用例检查排序与各类
目录故障，避免为了造异常而改动用户的原始资料。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support import sample_record, write_catalog


def test_real_catalog_returns_all_papers(real_client: TestClient) -> None:
    response = real_client.get("/api/papers")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 88
    items = payload["items"]
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))

    # 最近学年在前；同一学年内有日期的按日期降序，空日期在该学年末尾。
    assert ids[:5] == [
        "prob-2025-2026-midterm",
        "math-aii-20250625-final",
        "math-bi-20250116-final",
        "geo-2024-2025-s1-final",
        "phys-2024-2025-s2-midterm-ai",
    ]
    assert ids[-1] == "cv-2003-2004-s2-final-b"

    years = [int(item["academic_year"][:4]) for item in items]
    assert years == sorted(years, reverse=True)

    for year in set(years):
        group = [
            item
            for item in items
            if int(item["academic_year"][:4]) == year
        ]
        dated = [item["exam_date"] for item in group if item["exam_date"]]
        assert dated == sorted(dated, reverse=True)
        first_unknown = next(
            (index for index, item in enumerate(group) if item["exam_date"] is None),
            len(group),
        )
        assert all(item["exam_date"] is None for item in group[first_unknown:])


@pytest.mark.parametrize("keyword", ["高数", "高等数学"])
def test_course_family_aliases_match_all_math_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 14
    assert {item["course"] for item in payload["items"]} == {"高等数学"}


@pytest.mark.parametrize(
    "keyword",
    ["高等数学BⅠ", "高等数学BI", "高等数学B1", "高数B1", " 高等数学 b1 "],
)
def test_b1_aliases_match_b1_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 14
    assert all(item["course"] == "高等数学" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["高等数学AⅡ", "高等数学AII", "高等数学A2", "高数A2", "高数A下"],
)
def test_a2_aliases_match_a2_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 14
    assert all(item["course"] == "高等数学" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["高等数学（上）", "高等数学(上)", "高等数学上", "高数上"],
)
def test_up_aliases_match_up_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 14
    assert all(item["course"] == "高等数学" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["几何与线性代数", "几何与代数", "线性代数", "几何线代"],
)
def test_geometry_aliases_match_geometry_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 33
    assert all(item["course"] == "几何与线性代数" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["复变函数与积分变换", "复变函数", "复变"],
)
def test_complex_aliases_match_complex_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 12
    assert all(item["course"] == "复变函数与积分变换" for item in payload["items"])


@pytest.mark.parametrize("keyword", ["离散数学", "离散"])
def test_discrete_aliases_match_discrete_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 5
    assert all(item["course"] == "离散数学" for item in payload["items"])


@pytest.mark.parametrize("keyword", ["大学物理", "大物", "物理", "大学物理AII"])
def test_physics_aliases_match_physics_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 12
    assert all(item["course"] == "大学物理" for item in payload["items"])


def test_new_subjects_exam_type_counts(real_client: TestClient) -> None:
    def total(course: str, exam_type: str) -> int:
        return real_client.get(
            "/api/papers", params={"course": course, "exam_type": exam_type}
        ).json()["total"]

    assert total("几何与线性代数", "midterm") == 5
    assert total("几何与线性代数", "final") == 28
    assert total("复变函数与积分变换", "midterm") == 1
    assert total("复变函数与积分变换", "final") == 11
    assert total("离散数学", "final") == 5
    assert total("大学物理", "midterm") == 6
    assert total("大学物理", "final") == 6


@pytest.mark.parametrize(
    "keyword",
    [
        "概率论",
        "概率",
        "概统",
        "概率统计",
        "概率论与数理统计",
        "概率论与数理统计A",
        "概率论A",
    ],
)
def test_probability_aliases_match_all_probability_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 12
    assert all(item["course"] == "概率论与数理统计" for item in payload["items"])


def test_probability_course_and_exam_type_combined(
    real_client: TestClient,
) -> None:
    midterm = real_client.get(
        "/api/papers", params={"course": "概率论", "exam_type": "midterm"}
    ).json()
    final = real_client.get(
        "/api/papers", params={"course": "概率论", "exam_type": "final"}
    ).json()

    assert midterm["total"] == 6
    assert final["total"] == 6


def test_unknown_course_returns_empty_list(real_client: TestClient) -> None:
    payload = real_client.get("/api/papers", params={"course": "大学化学"}).json()

    assert payload == {"items": [], "total": 0}


def test_exam_type_filters_are_exclusive(real_client: TestClient) -> None:
    midterm = real_client.get("/api/papers", params={"exam_type": "midterm"}).json()
    final = real_client.get("/api/papers", params={"exam_type": "final"}).json()

    assert midterm["total"] == 25
    assert final["total"] == 63
    assert all(item["exam_type"] == "midterm" for item in midterm["items"])
    assert all(item["exam_type"] == "final" for item in final["items"])
    assert "math-bi-20221107-midterm" in [item["id"] for item in midterm["items"]]
    assert "math-bi-20250116-final" in [item["id"] for item in final["items"]]


def test_course_and_exam_type_combined(real_client: TestClient) -> None:
    payload = real_client.get(
        "/api/papers", params={"course": "高等数学BⅠ", "exam_type": "final"}
    ).json()

    assert payload["total"] == 7
    assert all(item["course"] == "高等数学" for item in payload["items"])
    assert all(item["exam_type"] == "final" for item in payload["items"])


def test_family_and_exam_type_combined(real_client: TestClient) -> None:
    payload = real_client.get(
        "/api/papers", params={"course": "高数", "exam_type": "final"}
    ).json()

    assert payload["total"] == 7
    assert all(item["exam_type"] == "final" for item in payload["items"])


def test_blank_exam_type_means_all(real_client: TestClient) -> None:
    payload = real_client.get("/api/papers", params={"exam_type": "   "}).json()

    assert payload["total"] == 88


def test_invalid_exam_type_returns_422(real_client: TestClient) -> None:
    response = real_client.get("/api/papers", params={"exam_type": "quiz"})

    assert response.status_code == 422
    assert "midterm" in response.json()["detail"]


def test_item_shape_hides_internal_fields(real_client: TestClient) -> None:
    item = real_client.get("/api/papers").json()["items"][0]

    assert set(item) == {
        "id",
        "title",
        "course",
        "academic_year",
        "exam_type",
        "exam_date",
        "download_url",
    }
    assert item["download_url"] == f"/api/papers/{item['id']}/download"
    assert "file_path" not in item


def test_unknown_date_sorted_last(
    sample_root: Path, make_client
) -> None:
    """同一学年内，有日期的在前，空日期最后。"""

    write_catalog(
        sample_root,
        [
            sample_record("no-date", "midterm", None, "期中样例.pdf"),
            sample_record("older", "midterm", "2022-11-07", "期中样例.pdf"),
            sample_record("newer", "final", "2024-11-07", "期中样例.pdf"),
        ],
    )
    items = make_client(sample_root).get("/api/papers").json()["items"]

    assert [item["id"] for item in items] == ["newer", "older", "no-date"]
    assert items[-1]["exam_date"] is None


def test_sorted_by_academic_year_then_exam_date(
    sample_root: Path, make_client
) -> None:
    write_catalog(
        sample_root,
        [
            sample_record(
                "year-2021",
                "midterm",
                "2021-11-08",
                "期中样例.pdf",
                academic_year="2021-2022",
            ),
            sample_record(
                "year-2024-null",
                "midterm",
                None,
                "期中样例.pdf",
                academic_year="2024-2025",
            ),
            sample_record(
                "year-2024-dated",
                "final",
                "2025-06-25",
                "期末样例.pdf",
                academic_year="2024-2025",
            ),
            sample_record(
                "year-2025-null",
                "midterm",
                None,
                "期中样例.pdf",
                academic_year="2025-2026",
            ),
        ],
    )
    items = make_client(sample_root).get("/api/papers").json()["items"]

    assert [item["id"] for item in items] == [
        "year-2025-null",
        "year-2024-dated",
        "year-2024-null",
        "year-2021",
    ]


def test_missing_catalog_returns_503(tmp_path: Path, make_client) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    response = make_client(root).get("/api/papers")

    assert response.status_code == 503
    assert "不存在" in response.json()["detail"]
    assert str(tmp_path) not in response.json()["detail"]


def test_broken_json_returns_503(tmp_path: Path, make_client) -> None:
    root = tmp_path / "broken"
    write_catalog(root, "{ 这不是 JSON")

    response = make_client(root).get("/api/papers")

    assert response.status_code == 503


def test_top_level_not_array_returns_503(tmp_path: Path, make_client) -> None:
    root = tmp_path / "object"
    write_catalog(root, {"items": []})

    response = make_client(root).get("/api/papers")

    assert response.status_code == 503


def test_invalid_field_value_returns_503(tmp_path: Path, make_client) -> None:
    root = tmp_path / "invalid-field"
    record = sample_record("bad-type", "quiz", "2024-11-07", "期中样例.pdf")
    write_catalog(root, [record])

    response = make_client(root).get("/api/papers")

    assert response.status_code == 503
    assert "字段不合法" in response.json()["detail"]


def test_absolute_file_path_returns_503(tmp_path: Path, make_client) -> None:
    root = tmp_path / "absolute"
    record = sample_record("absolute", "midterm", "2024-11-07", "期中样例.pdf")
    record["file_path"] = "C:/Windows/win.ini"
    write_catalog(root, [record])

    response = make_client(root).get("/api/papers")

    assert response.status_code == 503


def test_duplicate_ids_return_503(tmp_path: Path, make_client) -> None:
    root = tmp_path / "duplicate"
    record = sample_record("same-id", "midterm", "2024-11-07", "期中样例.pdf")
    write_catalog(root, [record, dict(record)])

    response = make_client(root).get("/api/papers")

    assert response.status_code == 503
    assert "重复" in response.json()["detail"]


def test_non_utf8_catalog_returns_503(tmp_path: Path, make_client) -> None:
    """解码失败必须按“目录不可用”返回 503，而不是 500。"""

    root = tmp_path / "non-utf8"
    write_catalog(root, b'[{"id": "broken"}, \xff\xfe]')

    response = make_client(root).get("/api/papers")

    assert response.status_code == 503
    assert "UTF-8" in response.json()["detail"]
    assert str(tmp_path) not in response.json()["detail"]
