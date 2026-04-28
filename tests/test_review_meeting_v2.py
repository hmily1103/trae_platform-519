import os


def test_rmv2_build_blocker_and_gate_minimal():
    from modules.prd_audit.review_meeting_v2 import build_blocker_item, build_meeting_gate

    d = {
        "risk_level": "P0",
        "type": "异常流程缺失",
        "module": "上传",
        "description": "只描述成功路径，缺少失败/超时/重试口径",
        "anchor": "L0010-L0020",
        "evidence_quotes": [],
    }
    b = build_blocker_item(d)
    assert b["risk_level"] == "P0"
    assert b["owner"] == "PM"
    assert "required_evidence" in b and isinstance(b["required_evidence"], list)
    gate = build_meeting_gate([b])
    assert gate["gate"] in ("FAIL", "BLOCKED")
    assert gate["metrics"]["p0_count"] == 1


def test_rmv2_export_markdown_contains_snapshot_and_gate():
    from modules.prd_audit.review_meeting_v2 import export_meeting_markdown, export_prd_v2_markdown

    meeting = {
        "meta": {"snapshot_id": "snap_xxx", "mode": "snapshot"},
        "gate": {"gate": "BLOCKED", "reason": "x", "metrics": {"p0_count": 1, "blocked_count": 1, "required_evidence_count": 2}},
        "blockers": [
            {
                "risk_level": "P0",
                "title": "失败路径未定义",
                "module": "上传",
                "anchor": "L1-L2",
                "evidence_class": "MISSING_SPEC",
                "evidence_quotes": [],
                "ac": {"given": "g", "when": "w", "then": "t"},
                "observability": ["error_code"],
                "required_evidence": ["补齐失败口径"],
            }
        ],
    }
    md = export_meeting_markdown(meeting)
    assert "输入快照：snap_xxx" in md
    assert "结论：**BLOCKED**" in md
    assert "失败路径未定义" in md
    md2 = export_prd_v2_markdown(
        {
            **meeting,
            "prd_source": "盒子型号：x9、x7、派2",
            "claims": [{"claim": "型号范围：x9、x7、派2", "status": "SUPPORTED", "importance": "P0", "evidence_quotes": ["盒子型号：x9、x7、派2"]}],
        }
    )
    assert "PRD v2.0" in md2
    assert "范围总表" in md2
    assert "盒子型号：x9、x7、派2" in md2


def test_rmv2_save_meeting_snapshot_writes_file(tmp_path):
    from modules.prd_audit.review_meeting_v2 import MeetingV2, save_meeting_snapshot

    base_dir = str(tmp_path)
    os.makedirs(os.path.join(base_dir, "learning_repo"), exist_ok=True)
    m = MeetingV2(
        meeting_id="rmv2_1",
        meta={"snapshot_id": "snap_1", "mode": "snapshot"},
        prd_source="",
        agenda=[],
        decisions=[],
        blockers=[],
        claims=[],
        gate={"gate": "PASS", "reason": "ok", "metrics": {}},
        messages=[],
        prd_patch="",
        prd_v2="",
        created_at="now",
    )
    info = save_meeting_snapshot(base_dir, m)
    assert info["meeting_snapshot_id"].startswith("msnap_")
    assert os.path.exists(info["path"])


def test_rmv2_create_meeting_includes_messages():
    from modules.prd_audit.review_meeting_v2 import create_meeting_from_stage3

    s3 = {
        "defects": [
            {"risk_level": "P0", "type": "异常流程缺失", "module": "上传", "description": "缺少失败/超时/重试口径", "anchor": "L1-L2"},
            {"risk_level": "P1", "type": "访问控制缺失", "module": "扫码", "description": "越权", "anchor": "L3-L4"},
        ]
    }
    prd = "第一行\n第二行\n第三行\n第四行"
    m = create_meeting_from_stage3(s3, snapshot_id="snap_x", mode="snapshot", prd_content=prd)
    obj = m.to_dict()
    assert "agenda" in obj and isinstance(obj["agenda"], list)
    assert "decisions" in obj and isinstance(obj["decisions"], list)
    assert "messages" in obj and isinstance(obj["messages"], list)
    assert len(obj["messages"]) >= 3  # QA/Dev/PM
    assert "prd_patch" in obj
    assert "claims" in obj and isinstance(obj["claims"], list)
    assert "prd_v2" in obj and isinstance(obj["prd_v2"], str)
    assert "PRD v2.0" in obj["prd_v2"]
    assert "prd_source" in obj and isinstance(obj["prd_source"], str)

