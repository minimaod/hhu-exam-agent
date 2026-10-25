"""下载接口 GET /api/papers/{paper_id}/download 的契约与安全检查。

真实文件用 SHA-256 与磁盘原件比对，避免只看 HTTP 200 就宣布“下载成功”。
异常用例全部在临时目录构造，不触碰用户原始 PDF。
"""

import hashlib
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from tests.support import (
    REAL_A2_FINAL,
    REAL_B1_2021_MIDTERM,
    REAL_C_2022_FINAL,
    REAL_CV_2021_MID,
    REAL_DM_2022_FINAL,
    REAL_ENG_2024_MID,
    REAL_ETHICS_2020_FINAL_A,
    REAL_FINAL,
    REAL_GEO_2024_FINAL,
    REAL_HIST_2016_FINAL_A,
    REAL_MARX_2023_FINAL,
    REAL_MIDTERM,
    REAL_PHYS_2021_FINAL,
    REAL_PHYS_2024_MID,
    REAL_PROB_2018_ENG,
    REAL_PROB_2021_FINAL,
    REAL_PROB_2021_MID,
    REAL_PY_2024_FINAL,
    REAL_TRANSFER_MATH_2021,
    REAL_UP_2014_MIDTERM,
    exclusive_file_lock,
    sample_record,
    sha256_of,
    write_catalog,
)


@pytest.mark.parametrize(
    ("paper_id", "original"),
    [
        ("math-bi-20221107-midterm", REAL_MIDTERM),
        ("math-bi-20250116-final", REAL_FINAL),
        ("math-bi-20211108-midterm", REAL_B1_2021_MIDTERM),
        ("math-aii-20250625-final", REAL_A2_FINAL),
        ("math-up-2014-midterm", REAL_UP_2014_MIDTERM),
        ("prob-2018-2019-engineering-final", REAL_PROB_2018_ENG),
        ("prob-2021-2022-midterm", REAL_PROB_2021_MID),
        ("prob-2021-2022-final", REAL_PROB_2021_FINAL),
        ("geo-2024-2025-s1-final", REAL_GEO_2024_FINAL),
        ("cv-2021-2022-s2-midterm", REAL_CV_2021_MID),
        ("dm-2022-2023-s1-final", REAL_DM_2022_FINAL),
        ("phys-2024-2025-s2-midterm-ai", REAL_PHYS_2024_MID),
        ("c-2022-2023-s2-final", REAL_C_2022_FINAL),
        ("py-2024-2025-s2-final", REAL_PY_2024_FINAL),
        ("hist-2016-2017-s1-final-a", REAL_HIST_2016_FINAL_A),
        ("eng-2023-2024-s2-midterm-ii", REAL_ENG_2024_MID),
        ("ethics-2019-2020-s2-final-a", REAL_ETHICS_2020_FINAL_A),
        ("marx-2023-2024-s1-final-a", REAL_MARX_2023_FINAL),
        ("transfer-math-2021-b", REAL_TRANSFER_MATH_2021),
        ("phys-2021-2022-s1-final-aii", REAL_PHYS_2021_FINAL),
    ],
)
def test_download_matches_original_bytes(
    real_client: TestClient, paper_id: str, original: Path
) -> None:
    assert original.is_file(), f"真实资料缺失：{original}"

    response = real_client.get(f"/api/papers/{paper_id}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == original.read_bytes()
    assert hashlib.sha256(response.content).hexdigest() == sha256_of(original)


@pytest.mark.parametrize(
    ("paper_id", "original"),
    [
        ("math-bi-20221107-midterm", REAL_MIDTERM),
        ("math-bi-20250116-final", REAL_FINAL),
        ("math-bi-20211108-midterm", REAL_B1_2021_MIDTERM),
        ("math-aii-20250625-final", REAL_A2_FINAL),
        ("math-up-2014-midterm", REAL_UP_2014_MIDTERM),
        ("prob-2018-2019-engineering-final", REAL_PROB_2018_ENG),
        ("prob-2021-2022-midterm", REAL_PROB_2021_MID),
        ("prob-2021-2022-final", REAL_PROB_2021_FINAL),
        ("geo-2024-2025-s1-final", REAL_GEO_2024_FINAL),
        ("cv-2021-2022-s2-midterm", REAL_CV_2021_MID),
        ("dm-2022-2023-s1-final", REAL_DM_2022_FINAL),
        ("phys-2024-2025-s2-midterm-ai", REAL_PHYS_2024_MID),
        ("c-2022-2023-s2-final", REAL_C_2022_FINAL),
        ("py-2024-2025-s2-final", REAL_PY_2024_FINAL),
        ("hist-2016-2017-s1-final-a", REAL_HIST_2016_FINAL_A),
        ("eng-2023-2024-s2-midterm-ii", REAL_ENG_2024_MID),
        ("ethics-2019-2020-s2-final-a", REAL_ETHICS_2020_FINAL_A),
        ("marx-2023-2024-s1-final-a", REAL_MARX_2023_FINAL),
        ("transfer-math-2021-b", REAL_TRANSFER_MATH_2021),
        ("phys-2021-2022-s1-final-aii", REAL_PHYS_2021_FINAL),
    ],
)
def test_download_keeps_chinese_filename(
    real_client: TestClient, paper_id: str, original: Path
) -> None:
    response = real_client.get(f"/api/papers/{paper_id}/download")

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=utf-8''" in disposition
    encoded_name = disposition.split("filename*=utf-8''", 1)[1]
    assert unquote(encoded_name) == original.name


def test_download_url_from_list_is_usable(real_client: TestClient) -> None:
    item = real_client.get("/api/papers").json()["items"][0]

    response = real_client.get(item["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_every_registered_paper_downloads(real_client: TestClient) -> None:
    items = real_client.get("/api/papers").json()["items"]

    assert len(items) == 162
    for item in items:
        response = real_client.get(item["download_url"])
        assert response.status_code == 200, item["id"]
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")


def test_unknown_id_returns_404(real_client: TestClient) -> None:
    response = real_client.get("/api/papers/not-registered/download")

    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_missing_file_returns_404(sample_root: Path, make_client) -> None:
    (sample_root / "高数试卷" / "期中样例.pdf").unlink()
    client = make_client(sample_root)

    response = client.get("/api/papers/sample-midterm/download")

    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_path_traversal_is_rejected(tmp_path: Path, make_client) -> None:
    root = tmp_path / "papers"
    (root / "高数试卷").mkdir(parents=True)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\n% must not be served\n")
    write_catalog(
        root,
        [sample_record("escape", "midterm", "2024-11-07", "../../outside.pdf")],
    )

    response = make_client(root).get("/api/papers/escape/download")

    assert response.status_code == 404
    assert response.content != outside.read_bytes()
    assert str(tmp_path) not in response.json()["detail"]


def test_non_pdf_file_is_rejected(sample_root: Path, make_client) -> None:
    (sample_root / "高数试卷" / "说明.txt").write_text("不是试卷", encoding="utf-8")
    write_catalog(
        sample_root,
        [sample_record("text-file", "midterm", "2024-11-07", "说明.txt")],
    )

    response = make_client(sample_root).get("/api/papers/text-file/download")

    assert response.status_code == 404
    assert "PDF" in response.json()["detail"]


def test_unreadable_file_returns_404(sample_root: Path, make_client) -> None:
    """PDF 存在但被其他程序占用时，应在发送响应前返回明确错误。"""

    target = sample_root / "高数试卷" / "期中样例.pdf"

    with exclusive_file_lock(target):
        response = make_client(sample_root).get("/api/papers/sample-midterm/download")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "无法读取" in response.json()["detail"]
    assert str(target) not in response.json()["detail"]


def test_download_recovers_after_lock_released(sample_root: Path, make_client) -> None:
    """探针只做可读性检查，不应影响正常下载流程。"""

    target = sample_root / "高数试卷" / "期中样例.pdf"
    expected = target.read_bytes()

    with exclusive_file_lock(target):
        locked = make_client(sample_root).get("/api/papers/sample-midterm/download")
    assert locked.status_code == 404

    response = make_client(sample_root).get("/api/papers/sample-midterm/download")

    assert response.status_code == 200
    assert response.content == expected


def test_catalog_failure_returns_503_on_download(
    tmp_path: Path, make_client
) -> None:
    root = tmp_path / "no-catalog"
    root.mkdir()
    client = make_client(root)

    response = client.get("/api/papers/math-bi-20221107-midterm/download")

    assert response.status_code == 503
    assert "目录" in response.json()["detail"]
