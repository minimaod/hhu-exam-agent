"""查询接口 GET /api/papers 的契约测试。

真实目录用例检查页面真正会看到的全部结果；隔离目录用例检查排序与各类
目录故障，避免为了造异常而改动用户的原始资料。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.papers.courses import detect_course
from tests.support import sample_record, write_catalog


def test_detect_course_from_free_text() -> None:
    assert detect_course("我要复习转专业考试") == "转专业"
    assert detect_course("我要复习高数") == "高等数学"
    assert detect_course("找线代期末试卷") == "几何与线性代数"
    assert detect_course("我要复习C语言") == "C语言程序设计"
    assert detect_course("我要复习马原") == "马克思主义基本原理"
    # 没提课程、或同时提了两门课时不猜，交给调度模型澄清。
    assert detect_course("给我期末试卷") is None
    assert detect_course("高数和物理的卷子") is None
    assert detect_course("") is None


def test_real_catalog_returns_all_papers(real_client: TestClient) -> None:
    response = real_client.get("/api/papers")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 162
    items = payload["items"]
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))

    # 最近学年在前；同一学年内有日期的按日期降序，空日期在该学年末尾。
    assert ids[:5] == [
        "prob-2025-2026-midterm",
        "transfer-automation-2025",
        "math-aii-20250625-final",
        "math-bi-20250116-final",
        "transfer-english-2024",
    ]
    # 学年未标注的记录（马原题库、无年份的转专业资料）排在最后。
    assert ids[-11:] == [
        "marx-set-01",
        "marx-set-02",
        "marx-set-03",
        "marx-set-04",
        "marx-set-05",
        "marx-set-06",
        "marx-set-07",
        "marx-set-08",
        "marx-set-09",
        "marx-set-10",
        "transfer-english-sources",
    ]

    dated_items = [item for item in items if item["academic_year"][:4].isdigit()]
    years = [int(item["academic_year"][:4]) for item in dated_items]
    assert years == sorted(years, reverse=True)

    for year in set(years):
        group = [
            item
            for item in dated_items
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

    assert payload["total"] == 13
    assert all(item["course"] == "大学物理" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["中国近现代史纲要", "中国近代史纲要", "近代史", "史纲"],
)
def test_history_aliases_match_history_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 11
    assert all(item["course"] == "中国近现代史纲要" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["大学英语", "英语", "大学英语I", "大学英语II"],
)
def test_english_aliases_match_english_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 5
    assert all(item["course"] == "大学英语" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["思想道德与法治", "思想道德修养与法律基础", "思修"],
)
def test_ethics_aliases_match_ethics_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 7
    assert all(item["course"] == "思想道德与法治" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["马克思主义基本原理", "马克思主义基本原理概论", "马原"],
)
def test_marx_aliases_match_marx_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 11
    assert all(item["course"] == "马克思主义基本原理" for item in payload["items"])


@pytest.mark.parametrize("keyword", ["转专业", "转专业考试"])
def test_transfer_aliases_match_transfer_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 10
    assert all(item["course"] == "转专业" for item in payload["items"])
    assert all(item["exam_type"] == "other" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["C语言", "C语言程序设计", "C程序设计", "C"],
)
def test_c_aliases_match_c_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 9
    assert all(item["course"] == "C语言程序设计" for item in payload["items"])


@pytest.mark.parametrize(
    "keyword",
    ["Python", "Python语言", "Python程序设计", "Python语言程序设计"],
)
def test_python_aliases_match_python_papers(
    real_client: TestClient, keyword: str
) -> None:
    payload = real_client.get("/api/papers", params={"course": keyword}).json()

    assert payload["total"] == 20
    assert all(item["course"] == "Python语言程序设计" for item in payload["items"])


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
    assert total("大学物理", "final") == 7
    assert total("C语言程序设计", "midterm") == 5
    assert total("C语言程序设计", "final") == 4
    assert total("Python语言程序设计", "midterm") == 10
    assert total("Python语言程序设计", "final") == 10
    assert total("中国近现代史纲要", "final") == 11
    assert total("大学英语", "midterm") == 1
    assert total("大学英语", "final") == 4
    assert total("思想道德与法治", "final") == 7
    assert total("马克思主义基本原理", "final") == 11
    assert total("转专业", "other") == 10


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
    other = real_client.get("/api/papers", params={"exam_type": "other"}).json()

    assert midterm["total"] == 41
    assert final["total"] == 111
    assert other["total"] == 10
    assert all(item["exam_type"] == "midterm" for item in midterm["items"])
    assert all(item["exam_type"] == "final" for item in final["items"])
    assert all(item["exam_type"] == "other" for item in other["items"])
    assert all(item["course"] == "转专业" for item in other["items"])
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

    assert payload["total"] == 162


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
