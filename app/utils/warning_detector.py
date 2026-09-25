import json
from typing import List, Dict, Tuple, Optional
from sqlalchemy.orm import Session
from datetime import datetime

from app.models import (
    Graduate,
    College,
    MicroMajor,
    ProvinceReferenceLine,
    Warning,
    DestinationStatus,
    DestinationType,
    WarningType,
    WarningLevel,
    WarningStatus,
)
from app.utils.stats_calculator import calculate_group_stats


DECLINE_THRESHOLD_YELLOW = 2
DECLINE_THRESHOLD_ORANGE = 3
DECLINE_THRESHOLD_RED = 4


GAP_THRESHOLD_YELLOW = 5.0
GAP_THRESHOLD_ORANGE = 10.0
GAP_THRESHOLD_RED = 15.0


def calculate_yearly_indicators(
    db: Session,
    target_type: str,
    target_id: int,
) -> List[Dict]:
    years = db.query(Graduate.graduation_year).distinct().order_by(
        Graduate.graduation_year
    ).all()
    years = [y[0] for y in years]

    yearly_data = []
    for year in years:
        query = db.query(Graduate).filter(Graduate.graduation_year == year)

        if target_type == "micro_major":
            query = query.filter(
                Graduate.has_micro_major == True,
                Graduate.micro_major_id == target_id,
            )
        elif target_type == "college":
            query = query.filter(Graduate.college_id == target_id)

        graduates = query.all()
        if not graduates:
            continue

        stats = calculate_group_stats(graduates)

        yearly_data.append({
            "year": year,
            "confirmed_rate": stats.confirmed_rate,
            "aligned_rate": stats.aligned_rate,
            "total_count": stats.total_count,
            "confirmed_count": stats.confirmed_count,
            "aligned_count": stats.aligned_count,
        })

    return yearly_data


def get_province_reference_line(db: Session, year: int, indicator: str) -> Optional[ProvinceReferenceLine]:
    return db.query(ProvinceReferenceLine).filter(
        ProvinceReferenceLine.graduation_year == year,
        ProvinceReferenceLine.indicator == indicator,
    ).first()


def detect_continuous_decline(
    yearly_data: List[Dict],
    indicator: str,
) -> List[Tuple[int, int, int, List[Dict]]]:
    if len(yearly_data) < 2:
        return []

    declines = []
    current_start = 0
    current_decline_count = 0

    def collect_segment(end_index: int) -> None:
        if current_decline_count >= DECLINE_THRESHOLD_YELLOW:
            declines.append((
                yearly_data[current_start]["year"],
                yearly_data[end_index]["year"],
                current_decline_count,
                yearly_data[current_start:end_index + 1],
            ))

    for i in range(1, len(yearly_data)):
        prev_point = yearly_data[i - 1]
        curr_point = yearly_data[i]

        # 只有年份相邻（相差1届）且指标严格变小才算连续下降；
        # 年份断档或数值持平/上升都会截断当前序列，之后的下降另起一段。
        years_consecutive = curr_point["year"] == prev_point["year"] + 1
        is_decline = years_consecutive and curr_point[indicator] < prev_point[indicator]

        if is_decline:
            if current_decline_count == 0:
                current_start = i - 1
            current_decline_count += 1
        else:
            collect_segment(i - 1)
            current_decline_count = 0

    collect_segment(len(yearly_data) - 1)

    return declines


def determine_decline_level(decline_count: int) -> WarningLevel:
    if decline_count >= DECLINE_THRESHOLD_RED:
        return WarningLevel.RED
    elif decline_count >= DECLINE_THRESHOLD_ORANGE:
        return WarningLevel.ORANGE
    else:
        return WarningLevel.YELLOW


def determine_gap_level(gap: float) -> WarningLevel:
    if gap >= GAP_THRESHOLD_RED:
        return WarningLevel.RED
    elif gap >= GAP_THRESHOLD_ORANGE:
        return WarningLevel.ORANGE
    else:
        return WarningLevel.YELLOW


def detect_below_province_line(
    db: Session,
    yearly_data: List[Dict],
    indicator: str,
) -> List[Tuple[int, float, float, float]]:
    below_entries = []

    for data in yearly_data:
        reference_line = get_province_reference_line(db, data["year"], indicator)
        if not reference_line:
            continue

        current_value = data[indicator]
        if current_value < reference_line.threshold:
            gap = reference_line.threshold - current_value
            below_entries.append((
                data["year"],
                current_value,
                reference_line.threshold,
                gap,
            ))

    return below_entries


def get_target_name(db: Session, target_type: str, target_id: int) -> str:
    if target_type == "micro_major":
        obj = db.query(MicroMajor).filter(MicroMajor.id == target_id).first()
        return obj.name if obj else "未知微专业"
    elif target_type == "college":
        obj = db.query(College).filter(College.id == target_id).first()
        return obj.name if obj else "未知学院"
    return "未知"


def find_active_overlapping_warning(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    indicator: str,
    start_year: int,
    end_year: int,
) -> Optional[Warning]:
    """查找同一对象同一指标下、区间与检测区间重叠的活动预警。"""
    return db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
        Warning.status == WarningStatus.ACTIVE,
        Warning.start_year <= end_year,
        Warning.end_year >= start_year,
    ).order_by(Warning.id).first()


def find_closed_interval_warning(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    indicator: str,
    start_year: int,
    end_year: int,
) -> Optional[Warning]:
    """查找同一真实区间上已被人工关闭（已解决/已忽略）的预警。"""
    return db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
        Warning.status != WarningStatus.ACTIVE,
        Warning.start_year == start_year,
        Warning.end_year == end_year,
    ).order_by(Warning.id).first()


def create_warning(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    warning_level: WarningLevel,
    indicator: str,
    current_value: float,
    start_year: int,
    end_year: int,
    decline_count: int,
    decline_details: List[Dict],
    province_value: Optional[float] = None,
    gap: Optional[float] = None,
    description: Optional[str] = None,
) -> Optional[Warning]:
    # 同一真实区间已被人工关闭的预警不被无条件重新激活
    closed = find_closed_interval_warning(
        db, target_type, target_id, warning_type, indicator, start_year, end_year
    )
    if closed:
        return None

    # 同一真实区间（或与之重叠的既有活动预警）重复检测时更新而非重复生成
    existing = find_active_overlapping_warning(
        db, target_type, target_id, warning_type, indicator, start_year, end_year
    )
    if existing:
        existing.current_value = current_value
        existing.start_year = start_year
        existing.end_year = end_year
        existing.decline_count = decline_count
        existing.decline_details = json.dumps(decline_details, ensure_ascii=False)
        existing.warning_level = warning_level
        existing.province_value = province_value
        existing.gap = gap
        existing.description = description
        db.flush()
        return existing

    target_name = get_target_name(db, target_type, target_id)

    warning = Warning(
        warning_type=warning_type,
        warning_level=warning_level,
        status=WarningStatus.ACTIVE,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        indicator=indicator,
        current_value=current_value,
        province_value=province_value,
        gap=gap,
        start_year=start_year,
        end_year=end_year,
        decline_count=decline_count,
        decline_details=json.dumps(decline_details, ensure_ascii=False),
        description=description,
    )
    db.add(warning)
    db.flush()
    return warning


def run_warning_detection_for_target(
    db: Session,
    target_type: str,
    target_id: int,
) -> List[Warning]:
    yearly_data = calculate_yearly_indicators(db, target_type, target_id)
    if not yearly_data:
        return []

    created_warnings = []

    for indicator, warning_type, label in [
        ("confirmed_rate", WarningType.CONFIRMED_RATE_DECLINE, "去向落实率"),
        ("aligned_rate", WarningType.ALIGNED_RATE_DECLINE, "对口就业率"),
    ]:
        declines = detect_continuous_decline(yearly_data, indicator)
        for start_year, end_year, decline_count, sequence in declines:
            level = determine_decline_level(decline_count)
            current_value = sequence[-1][indicator]
            description = (
                f"{label}自{start_year}届至{end_year}届连续{decline_count}届下降，"
                f"从{sequence[0][indicator]:.2f}%降至{current_value:.2f}%，"
                f"累计下降{sequence[0][indicator] - current_value:.2f}个百分点。"
            )

            warning = create_warning(
                db=db,
                target_type=target_type,
                target_id=target_id,
                warning_type=warning_type,
                warning_level=level,
                indicator=indicator,
                current_value=current_value,
                start_year=start_year,
                end_year=end_year,
                decline_count=decline_count,
                decline_details=sequence,
                description=description,
            )
            if warning is not None:
                created_warnings.append(warning)

    for indicator, label in [
        ("confirmed_rate", "去向落实率"),
        ("aligned_rate", "对口就业率"),
    ]:
        below_entries = detect_below_province_line(db, yearly_data, indicator)
        for year, current_value, threshold, gap in below_entries:
            level = determine_gap_level(gap)
            description = (
                f"{year}届{label}为{current_value:.2f}%，"
                f"低于全省预警阈值{threshold:.2f}%，"
                f"差距为{gap:.2f}个百分点。"
            )

            warning = create_warning(
                db=db,
                target_type=target_type,
                target_id=target_id,
                warning_type=WarningType.BELOW_PROVINCE_LINE,
                warning_level=level,
                indicator=indicator,
                current_value=current_value,
                start_year=year,
                end_year=year,
                decline_count=1,
                decline_details=[d for d in yearly_data if d["year"] == year],
                province_value=threshold,
                gap=gap,
                description=description,
            )
            if warning is not None:
                created_warnings.append(warning)

    return created_warnings


def run_full_warning_detection(db: Session) -> Dict:
    all_warnings = []

    micro_majors = db.query(MicroMajor).all()
    for mm in micro_majors:
        warnings = run_warning_detection_for_target(db, "micro_major", mm.id)
        all_warnings.extend(warnings)

    colleges = db.query(College).all()
    for college in colleges:
        warnings = run_warning_detection_for_target(db, "college", college.id)
        all_warnings.extend(warnings)

    db.commit()

    return {
        "total_warnings": len(all_warnings),
        "micro_major_warnings": sum(1 for w in all_warnings if w.target_type == "micro_major"),
        "college_warnings": sum(1 for w in all_warnings if w.target_type == "college"),
    }


def get_target_warnings(
    db: Session,
    target_type: str,
    target_id: int,
    status: Optional[str] = None,
) -> List[Warning]:
    query = db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
    )

    if status:
        query = query.filter(Warning.status == status)

    return query.order_by(Warning.created_at.desc()).all()
