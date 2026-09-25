"""连续下降预警检测的接口级验证。

通过接口构造确定性跨年数据，覆盖：
- 多段下降序列分别生成独立预警
- 数据末尾的下降段（末尾下降）
- 缺失年份截断：缺届两侧的指标不得连成同一段
- 相同数值不算下降，后续重新下降另起一段
- 重复检测同一真实区间时更新既有活动预警而不是重复生成
- 人工关闭的预警不被无条件重新激活
"""

import json
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from app.core import get_db
from app.models import Base
from app.utils.warning_detector import detect_continuous_decline


# 每届10名毕业生中落实去向的人数，None 表示该届无毕业生记录
CONFIRMED_PER_YEAR = {
    2015: 9,     # 90.0
    2016: 8,     # 80.0
    2017: 7,     # 70.0，第一段下降结束（连续2届）
    2018: 7,     # 70.0，与上届持平，截断序列
    2019: None,  # 缺失年份，截断序列
    2020: 6,     # 60.0，缺失年份后另起一段
    2021: 5,     # 50.0
    2022: 4,     # 40.0
    2023: 3,     # 30.0
    2024: 2,     # 20.0，末尾下降持续到数据最后一届（连续4届）
}


class ContinuousDeclineDetectionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = cls.TestingSession()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        db = self.TestingSession()
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
        db.close()

        self.college_id = self._create_college()
        self.micro_major_id = self._create_micro_major()
        self._create_graduates()

    def _create_college(self):
        resp = self.client.post("/api/v1/colleges", json={"name": "测试学院", "code": "XY001"})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def _create_micro_major(self):
        resp = self.client.post("/api/v1/micro-majors", json={
            "name": "数据分析微专业",
            "code": "WZ001",
            "college_id": self.college_id,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def _create_graduates(self):
        seq = 0
        for year, confirmed in CONFIRMED_PER_YEAR.items():
            if confirmed is None:
                continue
            for i in range(10):
                seq += 1
                resp = self.client.post("/api/v1/graduates", json={
                    "student_id": f"S{year}{i:03d}",
                    "name": f"学生{seq:03d}",
                    "major": "计算机科学与技术",
                    "graduation_year": year,
                    "college_id": self.college_id,
                    "has_micro_major": True,
                    "micro_major_id": self.micro_major_id,
                    "destination_status": "已落实" if i < confirmed else "待登记",
                    "destination_type": "就业",
                    "is_aligned": True,
                })
                self.assertEqual(resp.status_code, 200, resp.text)

    def _run_detection(self):
        resp = self.client.post(
            "/api/v1/warnings/detect",
            params={"target_type": "micro_major", "target_id": self.micro_major_id},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _list_warnings(self):
        resp = self.client.get(
            "/api/v1/warnings",
            params={"target_type": "micro_major", "target_id": self.micro_major_id},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return sorted(resp.json()["data"], key=lambda w: w["start_year"])

    def _get_warning_detail(self, warning_id):
        resp = self.client.get(f"/api/v1/warnings/{warning_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_detects_segments_with_year_gap_and_trailing_decline(self):
        result = self._run_detection()
        self.assertEqual(result["warnings_count"], 2)

        warnings = self._list_warnings()
        self.assertEqual(len(warnings), 2)
        first, second = warnings

        # 第一段：2015-2017 连续2届下降（2018届持平截断），黄色预警
        self.assertEqual(first["warning_type"], "落实率连续下降")
        self.assertEqual(first["warning_level"], "黄色预警")
        self.assertEqual(first["status"], "预警中")
        self.assertEqual(first["start_year"], 2015)
        self.assertEqual(first["end_year"], 2017)
        self.assertEqual(first["decline_count"], 2)
        self.assertEqual(first["current_value"], 70.0)

        # 第二段：缺失的2019届截断序列，2020-2024 连续4届下降（末尾下降），红色预警
        self.assertEqual(second["warning_type"], "落实率连续下降")
        self.assertEqual(second["warning_level"], "红色预警")
        self.assertEqual(second["status"], "预警中")
        self.assertEqual(second["start_year"], 2020)
        self.assertEqual(second["end_year"], 2024)
        self.assertEqual(second["decline_count"], 4)
        self.assertEqual(second["current_value"], 20.0)

        # 明细：第一段覆盖2015-2017各届
        detail = self._get_warning_detail(first["id"])
        details = json.loads(detail["decline_details"])
        self.assertEqual([d["year"] for d in details], [2015, 2016, 2017])
        self.assertEqual([d["confirmed_rate"] for d in details], [90.0, 80.0, 70.0])

        # 明细：第二段从2020届开始，不包含缺失的2019届
        detail = self._get_warning_detail(second["id"])
        details = json.loads(detail["decline_details"])
        self.assertEqual([d["year"] for d in details], [2020, 2021, 2022, 2023, 2024])
        self.assertEqual([d["confirmed_rate"] for d in details], [60.0, 50.0, 40.0, 30.0, 20.0])

    def test_repeat_detection_updates_existing_warnings(self):
        first_run = self._run_detection()
        self.assertEqual(first_run["warnings_count"], 2)
        first_ids = sorted(w["id"] for w in first_run["warnings"])

        second_run = self._run_detection()
        self.assertEqual(second_run["warnings_count"], 2)
        second_ids = sorted(w["id"] for w in second_run["warnings"])

        # 同一真实区间重复检测：更新既有活动预警，id 不变，不重复生成
        self.assertEqual(first_ids, second_ids)

        warnings = self._list_warnings()
        self.assertEqual(len(warnings), 2)
        self.assertEqual(
            [(w["start_year"], w["end_year"], w["decline_count"]) for w in warnings],
            [(2015, 2017, 2), (2020, 2024, 4)],
        )
        self.assertTrue(all(w["status"] == "预警中" for w in warnings))

    def test_manually_closed_warning_is_not_reactivated(self):
        self._run_detection()
        warnings = self._list_warnings()
        self.assertEqual(len(warnings), 2)
        closed_id = warnings[0]["id"]
        active_id = warnings[1]["id"]

        # 人工关闭第一段预警
        resp = self.client.put(f"/api/v1/warnings/{closed_id}", json={"status": "已解决"})
        self.assertEqual(resp.status_code, 200, resp.text)

        # 重新检测同一真实区间：已关闭的预警不被重新激活，也不重复生成
        result = self._run_detection()
        self.assertEqual(result["warnings_count"], 1)
        returned_ids = [w["id"] for w in result["warnings"]]
        self.assertNotIn(closed_id, returned_ids)
        self.assertIn(active_id, returned_ids)

        warnings = self._list_warnings()
        self.assertEqual(len(warnings), 2)
        by_id = {w["id"]: w for w in warnings}
        self.assertEqual(by_id[closed_id]["status"], "已解决")
        self.assertEqual(by_id[closed_id]["start_year"], 2015)
        self.assertEqual(by_id[closed_id]["end_year"], 2017)
        self.assertEqual(by_id[active_id]["status"], "预警中")
        self.assertEqual(by_id[active_id]["start_year"], 2020)
        self.assertEqual(by_id[active_id]["end_year"], 2024)
        self.assertEqual(by_id[active_id]["decline_count"], 4)


class DetectContinuousDeclineUnitTests(unittest.TestCase):
    """直接针对连续下降序列检测函数的边界行为。"""

    @staticmethod
    def _data(rates_by_year):
        return [
            {
                "year": year,
                "confirmed_rate": rate,
                "aligned_rate": rate,
                "total_count": 10,
                "confirmed_count": 0,
                "aligned_count": 0,
            }
            for year, rate in rates_by_year
        ]

    def test_missing_year_truncates_sequence(self):
        data = self._data([
            (2018, 90.0), (2019, 80.0), (2020, 70.0),
            (2022, 60.0), (2023, 50.0),  # 缺2021届
        ])
        declines = detect_continuous_decline(data, "confirmed_rate")
        # 缺失年份截断：2022-2023 只有1次下降，不足阈值，不得与2018-2020连成一段
        self.assertEqual(
            [(start, end, count) for start, end, count, _ in declines],
            [(2018, 2020, 2)],
        )

    def test_equal_value_breaks_and_new_decline_starts_new_segment(self):
        data = self._data([
            (2020, 70.0), (2021, 70.0),  # 持平不算下降
            (2022, 60.0), (2023, 50.0),  # 重新下降另起一段
        ])
        declines = detect_continuous_decline(data, "confirmed_rate")
        self.assertEqual(
            [(start, end, count) for start, end, count, _ in declines],
            [(2021, 2023, 2)],
        )

    def test_multiple_segments_and_trailing_decline(self):
        data = self._data([
            (2016, 90.0), (2017, 80.0), (2018, 70.0),
            (2019, 75.0),  # 上升截断，同时成为下一段的起点
            (2020, 70.0), (2021, 65.0), (2022, 60.0),  # 末尾下降
        ])
        declines = detect_continuous_decline(data, "confirmed_rate")
        self.assertEqual(
            [(start, end, count) for start, end, count, _ in declines],
            [(2016, 2018, 2), (2019, 2022, 3)],
        )
        self.assertEqual([d["year"] for d in declines[0][3]], [2016, 2017, 2018])
        self.assertEqual([d["year"] for d in declines[1][3]], [2019, 2020, 2021, 2022])


if __name__ == "__main__":
    unittest.main()
