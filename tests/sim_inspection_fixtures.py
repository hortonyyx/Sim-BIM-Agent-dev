"""Explicit assessments for geometry supplied by scripted integration tests."""


def synthetic_inspection():
    return {"rules": [
        {"kind": kind, "all": True, "spatial": {
            "grade": 1, "reason": {"en": en, "zh": zh}, "source_refs": []}}
        for kind, en, zh in (
            ("space", "The scripted test supplies the room extent and height.", "房间范围和层高由测试脚本设定。"),
            ("boundary", "This face follows the geometry declared in the fixture.", "该面沿测试样例设定的边界生成。"),
            ("opening", "The test fixture specifies the hosted opening and its dimensions.", "开口位置和尺寸来自测试样例的设定。"),
        )
    ]}
