"""通过接口验证连续下降预警检测：年份连续性、多段序列、末尾下降与重复检测。

数据约定：每个届次固定 20 名修读某微专业的毕业生，其中 k 人去向已落实，
因此该届落实率恒为 5*k（如 18 人落实 -> 90.0%）。所有人去向类型均为
"待落实"，对口就业率恒为 0.0 且不会触发对口率预警；库中无全省对照线，
因此接口返回的预警全部来自落实率连续下降检测。
"""

import json
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import get_db
from app.models import Base
from main import app

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

API = "/api/v1"
COHORT_SIZE = 20


class WarningDetectionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    # ---------- 数据构造 ----------

    def _create_micro_major(self, name, code):
        resp = self.client.post(f"{API}/colleges", json={"name": f"{name}学院", "code": f"C{code}"})
        self.assertEqual(resp.status_code, 200, resp.text)
        college_id = resp.json()["id"]
        resp = self.client.post(
            f"{API}/micro-majors",
            json={"name": name, "code": code, "college_id": college_id},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"], college_id

    def _add_cohort(self, mm_id, college_id, year, confirmed_count):
        """为指定届次导入 20 名毕业生，前 confirmed_count 人去向已落实。"""
        for i in range(COHORT_SIZE):
            resp = self.client.post(
                f"{API}/graduates",
                json={
                    "student_id": f"S{mm_id}x{year}x{i:02d}",
                    "name": f"学生{year}-{i}",
                    "major": "计算机科学与技术",
                    "graduation_year": year,
                    "college_id": college_id,
                    "has_micro_major": True,
                    "micro_major_id": mm_id,
                    "destination_status": "已落实" if i < confirmed_count else "待登记",
                    "destination_type": "待落实",
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)

    def _import_rates(self, mm_id, college_id, year_to_rate):
        """按 {届次: 落实率} 导入确定性数据，落实率须为 5 的倍数。"""
        for year, rate in year_to_rate.items():
            self.assertEqual(rate % 5, 0)
            self._add_cohort(mm_id, college_id, year, confirmed_count=rate // 5)

    # ---------- 接口封装 ----------

    def _detect(self, mm_id):
        resp = self.client.post(
            f"{API}/warnings/detect",
            params={"target_type": "micro_major", "target_id": mm_id},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _list_warnings(self, mm_id, **extra_params):
        params = {"target_type": "micro_major", "target_id": mm_id}
        params.update(extra_params)
        resp = self.client.get(f"{API}/warnings", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _get_warning_detail(self, warning_id):
        resp = self.client.get(f"{API}/warnings/{warning_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        detail = resp.json()
        detail["decline_details"] = json.loads(detail["decline_details"])
        return detail

    # ---------- 断言辅助 ----------

    def _assert_warning(self, warning, start_year, end_year, decline_count, level):
        self.assertEqual(warning["start_year"], start_year)
        self.assertEqual(warning["end_year"], end_year)
        self.assertEqual(warning["decline_count"], decline_count)
        self.assertEqual(warning["warning_level"], level)
        self.assertEqual(warning["warning_type"], "落实率连续下降")
        self.assertEqual(warning["indicator"], "confirmed_rate")

    def _assert_details(self, warning_id, expected_rates):
        """明细应包含下降段起点在内的全部数据点，届次连续且数值逐届严格下降。"""
        detail = self._get_warning_detail(warning_id)
        points = detail["decline_details"]
        self.assertEqual([p["confirmed_rate"] for p in points], [float(r) for r in expected_rates])
        years = [p["year"] for p in points]
        self.assertEqual(years, list(range(years[0], years[0] + len(years))))
        self.assertEqual(detail["start_year"], years[0])
        self.assertEqual(detail["end_year"], years[-1])
        self.assertEqual(detail["decline_count"], len(points) - 1)

    # ---------- 用例 ----------

    def test_missing_year_splits_decline_into_two_segments(self):
        """缺失届次必须截断序列：断档两侧各自成段，不得连成同一段。"""
        mm_id, college_id = self._create_micro_major("数据科学", "DS01")
        # 2022 届无毕业生记录；若误把断档两侧连成一段会得到 2019-2025 连续 6 届下降
        self._import_rates(mm_id, college_id, {
            2019: 90, 2020: 85, 2021: 80,
            2023: 75, 2024: 70, 2025: 65,
        })

        result = self._detect(mm_id)
        self.assertEqual(result["warnings_count"], 2)

        warnings = sorted(self._list_warnings(mm_id)["data"], key=lambda w: w["start_year"])
        self.assertEqual(len(warnings), 2)

        self._assert_warning(warnings[0], 2019, 2021, 2, "黄色预警")
        self._assert_details(warnings[0]["id"], [90, 85, 80])

        self._assert_warning(warnings[1], 2023, 2025, 2, "黄色预警")
        self._assert_details(warnings[1]["id"], [75, 70, 65])

        for w in warnings:
            self.assertFalse(w["start_year"] <= 2022 <= w["end_year"], "预警区间不应跨越缺失届次")

    def test_multiple_segments_and_trailing_decline(self):
        """回升截断后重新下降另起一段；下降持续到最后一届也能检出；阈值含义不变。"""
        mm_id, college_id = self._create_micro_major("智能制造", "IM01")
        self._import_rates(mm_id, college_id, {
            2018: 95, 2019: 90, 2020: 85, 2021: 80,  # 连续 3 届下降 -> 橙色
            2022: 90,                                 # 回升，截断序列
            2023: 85, 2024: 80,                       # 重新下降 2 届直至末尾 -> 黄色
        })

        result = self._detect(mm_id)
        self.assertEqual(result["warnings_count"], 2)

        warnings = sorted(self._list_warnings(mm_id)["data"], key=lambda w: w["start_year"])
        self.assertEqual(len(warnings), 2)

        self._assert_warning(warnings[0], 2018, 2021, 3, "橙色预警")
        self._assert_details(warnings[0]["id"], [95, 90, 85, 80])

        self._assert_warning(warnings[1], 2022, 2024, 2, "黄色预警")
        self._assert_details(warnings[1]["id"], [90, 85, 80])

    def test_equal_values_do_not_count_as_decline(self):
        """持平不算下降：序列在持平处截断，其后的重新下降另起一段。"""
        mm_id, college_id = self._create_micro_major("数字媒体", "DM01")
        self._import_rates(mm_id, college_id, {
            2020: 90, 2021: 80,  # 仅 1 届下降，不足黄色阈值
            2022: 80,            # 持平，截断序列
            2023: 70, 2024: 60,  # 重新下降 2 届 -> 黄色
        })

        result = self._detect(mm_id)
        self.assertEqual(result["warnings_count"], 1)

        warnings = self._list_warnings(mm_id)["data"]
        self.assertEqual(len(warnings), 1)
        self._assert_warning(warnings[0], 2022, 2024, 2, "黄色预警")
        self._assert_details(warnings[0]["id"], [80, 70, 60])

    def test_repeated_detection_updates_and_respects_closed_warnings(self):
        """重复检测更新既有活动预警；人工关闭后不被无条件重新激活。"""
        mm_id, college_id = self._create_micro_major("金融科技", "FT01")
        self._import_rates(mm_id, college_id, {
            2020: 95, 2021: 90, 2022: 85, 2023: 80, 2024: 75,  # 连续 4 届下降 -> 红色
        })

        first = self._detect(mm_id)
        self.assertEqual(first["warnings_count"], 1)
        warning_id = first["warnings"][0]["id"]

        # 重复检测同一真实区间：更新既有活动预警而不是重复生成
        second = self._detect(mm_id)
        self.assertEqual(second["warnings_count"], 1)
        self.assertEqual(second["warnings"][0]["id"], warning_id)

        listed = self._list_warnings(mm_id)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["active_count"], 1)
        self._assert_warning(listed["data"][0], 2020, 2024, 4, "红色预警")
        self._assert_details(warning_id, [95, 90, 85, 80, 75])

        # 人工关闭后重复检测：不得重新激活，也不得重复生成
        resp = self.client.put(f"{API}/warnings/{warning_id}", json={"status": "已解决"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "已解决")

        third = self._detect(mm_id)
        self.assertEqual(third["warnings_count"], 0)

        listed = self._list_warnings(mm_id)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["data"][0]["status"], "已解决")
        self.assertEqual(listed["data"][0]["decline_count"], 4)
        self.assertEqual(self._list_warnings(mm_id, status="预警中")["total"], 0)

        # 下降区间真实延长（新增 2025 届继续下降）则允许生成新区间的预警
        self._import_rates(mm_id, college_id, {2025: 70})
        fourth = self._detect(mm_id)
        self.assertEqual(fourth["warnings_count"], 1)
        new_warning_id = fourth["warnings"][0]["id"]
        self.assertNotEqual(new_warning_id, warning_id)

        listed = self._list_warnings(mm_id)
        self.assertEqual(listed["total"], 2)
        active = self._list_warnings(mm_id, status="预警中")["data"]
        self.assertEqual(len(active), 1)
        self._assert_warning(active[0], 2020, 2025, 5, "红色预警")
        self._assert_details(new_warning_id, [95, 90, 85, 80, 75, 70])
        closed = self._get_warning_detail(warning_id)
        self.assertEqual(closed["status"], "已解决")
        self.assertEqual((closed["start_year"], closed["end_year"]), (2020, 2024))


if __name__ == "__main__":
    unittest.main()
