"""Contract tests for the Echo Report JSON output layer."""

from __future__ import annotations

import json
from datetime import date
import re
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from qq_chat_analyzer.analysis import models as analysis_models  # noqa: E402
from qq_chat_analyzer.presentation import (
    ChartPoint,
    EchoExpressionCulture,
    EchoExpressionCombination,
    EchoExpressionCombinationMember,
    EchoExpressionItem,
    EchoLanguageMember,
    EchoLanguageProfile,
    EchoMemberExpression,
    EchoMemberCard,
    EchoReportView,
    build_echo_report_view,
    echo_report_to_dict,
    export_echo_report_html,
)


def test_featured_stories_export_only_summaries_and_share_the_media_target_union(tmp_path, caplog):
    caplog.set_level("INFO", logger="qq_chat_analyzer.desktop.analysis_diagnostics")
    from dataclasses import replace
    from sticker_feature_samples import indexed
    from test_sticker_features_batch2 import combined_scene
    from qq_chat_analyzer.analysis.sticker_features import analyze_private_stickers
    from qq_chat_analyzer.presentation.stickers import sticker_report_targets

    rows = combined_scene()
    index = indexed(rows, positions=range(len(rows)))
    features = analyze_private_stickers(index)
    reports = analysis_models.AnalysisReports(stickers=(index.report,), private_stickers=features)
    view = build_echo_report_view(reports, conversation_kind="private")
    payload = echo_report_to_dict(view)
    memories = payload["sticker_memories"]
    assert len(memories) == 2  # One six-finding material story, one style comparison.
    findings = {f["kind"]: f for m in memories for f in m["findings"]}
    assert set(findings) == {"reunion", "signature", "shared", "relay", "trend", "adoption", "style"}
    assert memories[0]["findings"][0]["kind"] == "reunion"
    assert [(p["label"], p["start_date"]) for p in findings["reunion"]["points"]] == [
        ("我 · 上次记录", "2025-01-22"), ("我 · 再次使用", "2025-03-12")]
    assert [p["start_date"] for p in findings["adoption"]["points"]] == ["2025-01-01", "2025-03-12"]
    assert [(p["value"], p["total"], p["end_date"]) for p in findings["trend"]["points"]] == [
        (2, 92, "2025-02-13"), (6, 6, "2025-03-15")]
    assert [(p["label"], p["value"]) for p in findings["relay"]["points"]] == [("我", 1), ("TA", 1)] * 4
    assert [(p["value"], p["date_count"]) for p in findings["shared"]["points"]] == [(10, 7), (10, 4)]
    assert [p["total"] for p in findings["style"]["points"]] == [10, 10]
    assert findings["style"]["points"][0]["value"] == pytest.approx(9.67, abs=.01)
    assert findings["adoption"]["note"] == "同一张图，在你们之间留下了回音。"
    assert findings["style"]["note"] == ""
    assert {kind: finding["owner_label"] for kind, finding in findings.items()} == {
        "reunion": "我", "signature": "我", "trend": "我",
        "shared": "", "relay": "", "adoption": "", "style": "",
    }
    habit = features.shared_habits[0]
    undated = replace(features, shared_habits=(replace(habit, members=(
        replace(habit.members[0], has_unknown_time=True), habit.members[1])),))
    undated_payload = echo_report_to_dict(build_echo_report_view(
        replace(reports, private_stickers=undated), conversation_kind="private"))
    assert "部分发送时间不明" not in json.dumps(undated_payload["sticker_memories"], ensure_ascii=False)
    assert all(len(m["top_materials"]) <= 5 for m in payload["stickers"]["members"])
    assert payload["stickers"]["shared_materials"] == []
    targets = sticker_report_targets(view.stickers)
    assert len({t.media_key for t in targets}) == len(targets) == len(view.stickers.assets)
    story_assets = {item["asset_key"] for m in memories for item in m["materials"]}
    assert story_assets <= {a.key for a in view.stickers.assets}
    # A selected image outside both Top 5 lists must still be recovered.
    top_keys = {item.media_key for member in view.stickers.members for item in member.top_materials}
    outside = next(m for m in index.report.materials if m.media_key not in top_keys)
    story = replace(features.stories.candidates[0], media_keys=(outside.media_key,))
    selected = replace(features, stories=replace(features.stories,
        candidates=(story,), featured_ids=(story.story_id,)))
    alternate = build_echo_report_view(replace(reports, private_stickers=selected), conversation_kind="private")
    assert outside.media_key in {t.media_key for t in sticker_report_targets(alternate.stickers)}
    no_stories = replace(features, stories=replace(features.stories, featured_ids=()))
    empty = build_echo_report_view(replace(reports, private_stickers=no_stories), conversation_kind="private")
    assert echo_report_to_dict(empty)["sticker_memories"] == []
    assert empty.stickers.members[0].top_materials
    assert outside.media_key not in {t.media_key for t in sticker_report_targets(empty.stickers)}
    group = build_echo_report_view(reports, conversation_kind="group")
    assert echo_report_to_dict(group)["sticker_memories"] == []
    foreign = replace(features, conversation_id="another-analysis")
    assert echo_report_to_dict(build_echo_report_view(replace(reports, private_stickers=foreign),
        conversation_kind="private"))["sticker_memories"] == []
    source = export_echo_report_html(view, tmp_path / "stories.html").read_text(encoding="utf-8")
    for row in rows:
        for part in row.contents:
            if hasattr(part, "text"):
                assert part.text not in source
    assert all(secret not in source for secret in (
        "fiction-A", "fiction-B", "fiction:", "batch2-seven", "occurrence_id", "context_start"))
    records = [r.getMessage() for r in caplog.records if r.name.endswith(".analysis_diagnostics")]
    counts = json.loads(next(r.removeprefix("[sticker-counts] ") for r in records if r.startswith("[sticker-counts] ")))
    assert counts == dict(features.diagnostic_counts)
    assert all(type(value) is int for value in counts.values())
    assert all(secret not in " ".join(records) for secret in (
        "fiction", "batch2-seven", features.first_date, features.end_date, "occurrence_id"))
    assert any("[sticker-projection-counts] featured=2 matched_scope=1 memories=2" in r for r in records)
    assert any("[sticker-media-counts] before=2 after=2" in r for r in records)


def test_relay_projection_bounds_turns_without_losing_counts_or_exporting_bodies():
    from sticker_feature_samples import Messages, indexed
    from qq_chat_analyzer.analysis.sticker_features import analyze_private_stickers

    sample = Messages("private-relay")
    for i in range(16):
        sample.add(0, "A" if i % 2 == 0 else "B",
                   *(("stamp", "other", None) if i == 0 else ("stamp",)),
                   text="private-body-sentinel" if i == 0 else "", seconds=i*20)
    rows = sample.ordered()
    index = indexed(rows, positions=range(len(rows)))
    features = analyze_private_stickers(index)
    payload = echo_report_to_dict(build_echo_report_view(analysis_models.AnalysisReports(
        stickers=(index.report,), private_stickers=features), conversation_kind="private"))
    relay = next(f for m in payload["sticker_memories"] for f in m["findings"] if f["kind"] == "relay")
    assert "16" in relay["title"]
    assert len(relay["points"]) == 12
    assert [p["label"] for p in relay["points"]] == ["我", "TA"] * 6
    assert relay["points"][0]["value"] == 3
    assert len(relay["points"][0]["materials"]) == 1  # Representative, not three invented pictures.
    assert relay["note"] == "图示前 12 轮，共 16 轮。"
    assert all(secret not in json.dumps(payload, ensure_ascii=False) for secret in (
        "private-body-sentinel", "fiction:", "private-relay", "fiction-A", "fiction-B"))



def test_independent_stories_after_featured_cap_are_exported_as_compact_memories():
    from sticker_feature_samples import many_sticker_stories, indexed
    from qq_chat_analyzer.analysis.sticker_features import analyze_private_stickers
    from qq_chat_analyzer.presentation.stickers import sticker_report_targets

    rows = many_sticker_stories()
    index = indexed(rows, positions=range(len(rows)))
    features = analyze_private_stickers(index)
    before = features.stories.featured_ids
    view = build_echo_report_view(analysis_models.AnalysisReports(
        stickers=(index.report,), private_stickers=features), conversation_kind="private")
    memories = echo_report_to_dict(view)["sticker_memories"]
    assert len(memories) == 8
    assert [m["compact"] for m in memories] == [False] * 3 + [True] * 5
    returns = [f for m in memories for f in m["findings"] if f["kind"] == "reunion"]
    assert sorted((date.fromisoformat(f["points"][1]["start_date"]) -
                   date.fromisoformat(f["points"][0]["start_date"])).days for f in returns) == [
        196, 396, 596, 796, 996, 1196]
    assert features.stories.featured_ids == before
    assert all(len(m.materials) <= 1 for m in view.stickers.memories if m.compact)
    targets = sticker_report_targets(view.stickers)
    assert len({t.media_key for t in targets}) == len(targets)
    primary = {item.media_key for m in view.stickers.members for item in m.top_materials}
    primary.update(item.media_key for m in view.stickers.memories if not m.compact for item in m.materials)
    assert {item.media_key for item in targets[:len(primary)]} == primary
    assert all(secret not in json.dumps(memories, ensure_ascii=False)
               for secret in ("fiction-", "fiction:", "visual-six-plus", "occurrence_id"))
    assert echo_report_to_dict(build_echo_report_view(
        analysis_models.AnalysisReports(stickers=(index.report,), private_stickers=features),
        conversation_kind="group"))["sticker_memories"] == []


def test_a_qualified_return_survives_a_full_shortlist_of_other_discovery_kinds():
    from sticker_feature_samples import Messages, indexed
    from qq_chat_analyzer.analysis.sticker_features import analyze_private_stickers

    sample = Messages("ordinary-return-after-cap")
    for side in ("A", "B"):
        for day in (0, 90, 180, 270, 365):
            sample.add(day, side, f"signature-{side}", seconds=100 if side == "A" else 200)
    for key, returned in (("old", 60), ("another-old", 80)):
        for day in (0, 2, 4, returned):
            sample.add(day, "A", key, seconds=300 if key == "old" else 400)
    sample.add(20, "A", "background", "background", seconds=300)
    sample.add(40, "A", "background", seconds=300)
    for day, key in ((190, "phase-one"), (200, "phase-one"),
                     (230, "phase-two"), (240, "phase-two")):
        sample.add(day, "A", key, key, key, seconds=300)
    for i in range(12):
        sample.add(400, "A" if i % 2 == 0 else "B", f"reply-{i}", seconds=i*20)
    rows = sample.ordered()
    index = indexed(rows, positions=range(len(rows)))
    features = analyze_private_stickers(index)
    assert [r.gap_days for r in features.reunions] == [76, 56]
    before = features.stories.featured_ids
    assert len(before) == 3
    featured = [s for s in features.stories.candidates if s.story_id in before]
    assert not any(name == "reunions" for s in featured for name, _ in s.findings)
    payload = echo_report_to_dict(build_echo_report_view(analysis_models.AnalysisReports(
        stickers=(index.report,), private_stickers=features), conversation_kind="private"))
    returns = [(m, f) for m in payload["sticker_memories"] for f in m["findings"]
               if f["kind"] == "reunion"]
    assert len(returns) == 1
    assert returns[0][0]["compact"] is True
    assert "76" in returns[0][1]["title"]  # One representative of the missing kind.
    assert features.stories.featured_ids == before
    assert "ordinary-return-after-cap" not in json.dumps(payload)


def test_relay_previews_retain_each_visible_turn_material_with_a_bounded_target_union():
    from sticker_feature_samples import Messages, indexed
    from qq_chat_analyzer.analysis.sticker_features import analyze_private_stickers
    from qq_chat_analyzer.presentation.stickers import sticker_report_targets

    sample = Messages("distinct-relay")
    for i in range(16):
        sample.add(0, "A" if i % 2 == 0 else "B", f"reply{i:02}", seconds=i * 20)
    rows = sample.ordered()
    index = indexed(rows, positions=range(len(rows)))
    features = analyze_private_stickers(index)
    view = build_echo_report_view(analysis_models.AnalysisReports(
        stickers=(index.report,), private_stickers=features), conversation_kind="private")
    relay = next(f for m in echo_report_to_dict(view)["sticker_memories"]
                 for f in m["findings"] if f["kind"] == "relay")
    assert len(relay["points"]) == 12
    assert [p["materials"] for p in relay["points"]] == [
        [{"asset_key": f"sticker:{i}"}] for i in range(1, 13)]
    assert "16" in relay["title"] and "前 12 轮" in relay["note"]
    assert len(sticker_report_targets(view.stickers)) == 12



def test_private_common_markers_use_the_two_sides_instead_of_other_members():
    from qq_chat_analyzer.analysis.stickers import StickerConversationReport, StickerMaterialUsage, StickerParticipant

    report = StickerConversationReport("qq", "fiction-room", "private", 10, 10, 0, 0,
        members=(StickerParticipant("a", True, 3, 3), StickerParticipant("b", False, 3, 3),
                 StickerParticipant("c", None, 4, 4)),
        materials=(StickerMaterialUsage("other-shared", "other-media", 7, (("a", 3), ("c", 4))),
                   StickerMaterialUsage("peer-only", "peer-media", 3, (("b", 3),))))
    payload = echo_report_to_dict(build_echo_report_view(
        analysis_models.AnalysisReports(stickers=(report,)), conversation_kind="private"))
    leader = payload["stickers"]["members"][0]["top_materials"][0]
    assert leader["reliable_member_count"] == 2
    assert leader["side_counts"] == [3, 0]


def test_group_report_budget_reaches_global_leaders_before_member_long_tail():
    from qq_chat_analyzer.presentation.models import EchoStickers, EchoStickerItem, EchoStickerMember
    from qq_chat_analyzer.presentation.stickers import sticker_report_targets

    def item(key):
        return EchoStickerItem(label=key, asset_key=key, media_key=key, count=1)
    chapter = EchoStickers(available=True, conversation_kind="group",
        total_top_materials=tuple(item(f"global-{i}") for i in range(10)),
        members=tuple(EchoStickerMember(label=f"member-{i}",
                      top_materials=tuple(item(f"member-{i}-{j}") for j in range(10))) for i in range(4)))
    first_batch = sticker_report_targets(chapter)[:32]
    assert {f"global-{i}" for i in range(10)} <= {target.media_key for target in first_batch}
    assert len(sticker_report_targets(chapter)) == 50


@pytest.mark.parametrize("kind,senders,limit", [
    ("private", 2, 5), ("group", 1, 10), ("group", 9, 10),
    ("group", 10, 5), ("group", 99, 5), ("group", 100, 3),
])
def test_member_top_limit_uses_scoped_sticker_senders_and_bounds_media_targets(kind, senders, limit):
    from qq_chat_analyzer.analysis.stickers import (
        StickerConversationReport, StickerMaterialUsage, StickerParticipant, StickerHabit,
    )
    from qq_chat_analyzer.presentation.stickers import sticker_report_targets

    # Inactive conversation members must not move the 9/10 or 99/100 boundary.
    members = tuple(StickerParticipant(f"m{i:03}", i == 0, 200, 184) for i in range(senders))
    if kind == "group":
        members += tuple(StickerParticipant(f"idle-{i}", None, 1, 0) for i in range(110))
    materials = tuple(StickerMaterialUsage(f"own-{i:03}-{rank:02}", f"own-{i:03}-{rank:02}",
                                           20 - rank, ((f"m{i:03}", 20 - rank),))
                      for i in range(senders) for rank in range(12))
    materials += tuple(StickerMaterialUsage(f"shared-{rank}", f"shared-{rank}", senders,
                                            tuple((f"m{i:03}", 1) for i in range(senders)))
                       for rank in range(10))
    report = StickerConversationReport(
        "qq", "fiction-room", kind, senders * 200, senders * 184, 0, 0,
        members=members, materials=materials,
        habits=(StickerHabit("m000", "own-000-11", 9, 9, 4, 35, 4, .2,
                             "2025-01-01", "2025-02-05", ()),),
    )
    view = build_echo_report_view(analysis_models.AnalysisReports(stickers=(report,)), conversation_kind=kind)
    chapter = view.stickers
    active = [member for member in chapter.members if member.send_count > 0]
    assert len(active) == senders
    assert all([item.count for item in member.top_materials] == list(range(20, 20 - limit, -1))
               for member in active)
    assert all(member.unique_material_count == 22 and member.send_count == 184 for member in active)
    assert chapter.unique_material_count == senders * 12 + 10
    assert len(chapter.total_top_materials) == (10 if kind == "group" else 0)
    assert len(chapter.shared_materials) == (10 if kind == "group" and senders > 1 else 0)
    expected = {f"own-{i:03}-{rank:02}" for i in range(senders) for rank in range(limit)}
    if kind == "group" and senders > 1:
        expected.update(f"shared-{rank}" for rank in range(10))
    if kind == "group":
        expected.add("own-000-11")  # Preserve the existing group signature contract.
    targets = sticker_report_targets(chapter)
    assert {item.media_key for item in targets} == expected
    assert len(targets) == len(expected) == len(chapter.assets)
    assert {asset.key for asset in chapter.assets} == {item.asset_key for item in targets}
    assert len({item.asset_key for item in targets}) == len(targets)
    serialized = json.dumps(echo_report_to_dict(view))
    assert all(key not in serialized for key in expected)


@pytest.mark.parametrize("encoded_format,verified_format,mime", [
    ("GIF", "GIF", "image/gif"),
    ("PNG", "APNG", "image/png"),
    ("WEBP", "WebP", "image/webp"),
])
def test_verified_animations_share_original_and_preview_files_between_html_and_json(
    tmp_path, encoded_format, verified_format, mime,
):
    import hashlib
    from qq_chat_analyzer.analysis.stickers import StickerConversationReport, StickerMaterialUsage, StickerParticipant
    from qq_chat_analyzer.application.sticker_media import verify_fetched_media
    from test_sticker_data import image_bytes

    raw = image_bytes(encoded_format, animated=True)
    media = tuple(verify_fetched_media(raw, media_key=key, expected_md5=digest) for key, digest in (
        ("original", hashlib.md5(raw).hexdigest()), ("preview", "0" * 32)))
    assert [item.status for item in media] == ["available", "preview"]
    assert all(item.format == verified_format and item.frame_count == 2
               and item.animation_preserved for item in media)
    report = StickerConversationReport(
        "qq", "fiction-room", "group", 2, 2, 0, 0,
        members=(StickerParticipant("a", True, 2, 2),),
        materials=tuple(StickerMaterialUsage(key, key, 1, (("a", 1),))
                        for key in ("original", "preview")),
    )
    view = build_echo_report_view(analysis_models.AnalysisReports(stickers=(report,)),
                                  conversation_kind="group", sticker_media=media)
    assets = echo_report_to_dict(view)["stickers"]["assets"]
    assert [item["status"] for item in assets.values()] == ["available", "preview"]
    html = export_echo_report_html(view, tmp_path / "animations.html").read_text(encoding="utf-8")
    from qq_chat_analyzer.presentation.echo_serializer import export_echo_report_json
    exported = export_echo_report_json(view, tmp_path / "animations.json")
    assert json.loads(exported.read_text(encoding="utf-8"))["stickers"]["assets"] == assets
    for asset in assets.values():
        assert asset["data_uri"].startswith("media/")
        assert (tmp_path / asset["data_uri"]).read_bytes() == raw
        assert asset["data_uri"] in html
    # Two independently verified identities/statuses can share identical bytes.
    assert len({item["data_uri"] for item in assets.values()}) == 1
    assert len(list((tmp_path / "media").iterdir())) == 1


@pytest.mark.parametrize("stored", [False, True])
def test_report_publishes_bounded_offline_files_without_dropping_assets(tmp_path, stored):
    from qq_chat_analyzer.presentation.echo_serializer import export_echo_report_json
    import hashlib
    from dataclasses import replace
    from qq_chat_analyzer.analysis.stickers import StickerConversationReport, StickerMaterialUsage, StickerParticipant
    from qq_chat_analyzer.application.sticker_media import verify_fetched_media
    from qq_chat_analyzer.application.echo_report_export import package_echo_report
    from qq_chat_analyzer.media_content import StoredMedia
    from test_sticker_data import image_bytes

    raw = image_bytes("GIF", animated=True)
    verified = verify_fetched_media(raw, media_key="a", expected_md5=hashlib.md5(raw).hexdigest())
    media = replace(verified, original_bytes=None, stored=StoredMedia(raw)) if stored else verified
    report = StickerConversationReport("qq", "fiction-room", "group", 1, 1, 0, 0,
        members=(StickerParticipant("a", True, 1, 1),),
        materials=(StickerMaterialUsage("a", "a", 1, (("a", 1),)),))
    view = build_echo_report_view(analysis_models.AnalysisReports(stickers=(report,)),
                                  conversation_kind="group", sticker_media=(media,))
    asset, = view.stickers.assets
    assert asset.status == "available" and asset.data_uri.startswith("media/")
    output = tmp_path / "output"
    export_echo_report_json(view, output / "echo-report.json")
    export_echo_report_html(view, output / "echo-report.html")
    published = package_echo_report(output, tmp_path / "reports", metadata={"generated_at":"2026-10-20T00:00:00+08:00"})
    assert (published / asset.data_uri).read_bytes() == raw
    assert asset.data_uri in (published / "echo-report.html").read_text(encoding="utf-8")
    assert len(list((published / "media").iterdir())) == 1
    if stored:
        assert media.original_bytes is None and media.stored.read() == raw
        media.stored.close()
    else:
        assert media.original_bytes == raw


def test_report_wordmark_uses_pixel_identical_lossless_resource(tmp_path):
    from PIL import Image
    from qq_chat_analyzer.resources import resource_path

    original = resource_path("assets/branding/echo/echo_wordmark_with_slogan.png")
    optimized = resource_path("assets/branding/echo/echo_wordmark_report.webp")
    assert optimized.is_file()
    assert optimized.stat().st_size < original.stat().st_size
    with Image.open(original) as before, Image.open(optimized) as after:
        assert before.size == after.size
        assert before.convert("RGBA").tobytes() == after.convert("RGBA").tobytes()
        assert before.info.get("icc_profile") == after.info.get("icc_profile")
    html = export_echo_report_html(EchoReportView(title="Fictional Echo"), tmp_path / "report.html").read_text(encoding="utf-8")
    assert 'class="brand-logo" src="data:image/webp;base64,' in html


def test_sticker_asset_budget_keeps_counts_and_explains_unavailable_images(monkeypatch):
    from dataclasses import replace
    from qq_chat_analyzer.analysis.stickers import StickerConversationReport, StickerMaterialUsage, StickerParticipant, StickerMedia
    from qq_chat_analyzer.application.sticker_media import verify_fetched_media
    from qq_chat_analyzer.presentation import stickers as module
    from test_sticker_data import image_bytes

    raw = image_bytes("PNG")
    import hashlib
    original = verify_fetched_media(raw, media_key="0", expected_md5=hashlib.md5(raw).hexdigest())
    preview = verify_fetched_media(raw, media_key="1", expected_md5="0" * 32)
    report = StickerConversationReport("qq", "fiction-room", "group", 9, 9, 0, 0,
        members=(StickerParticipant("a", True, 3, 3), StickerParticipant("b", False, 6, 6)),
        materials=tuple(StickerMaterialUsage(str(i), str(i), 2 if i < 3 else 1,
                         (("a", 1), ("b", 1)) if i < 3 else (("b", 1),)) for i in range(6)))
    monkeypatch.setattr(module, "REPORT_STICKER_BYTE_BUDGET", len(raw) * 2)
    view = build_echo_report_view(analysis_models.AnalysisReports(stickers=(report,)), conversation_kind="group",
        sticker_media=(original, preview, replace(original, media_key="2"),
                       StickerMedia("4", "missing", "no_digest_name_match"),
                       StickerMedia("5", "failed", "message_mapping_unconfirmed")))
    chapter = echo_report_to_dict(view)["stickers"]
    assert chapter["total_send_count"] == 9 and chapter["unique_material_count"] == 6
    assert [a["status"] for a in chapter["assets"].values()] == [
        "available", "preview", "limited", "not_requested", "missing", "failed"]
    assert list(chapter["assets"].values())[2]["reason"] == "report_asset_budget"
    assert sum(bool(a["data_uri"]) for a in chapter["assets"].values()) == 2
    assert all(not a["data_uri"] for a in list(chapter["assets"].values())[2:])


@pytest.mark.parametrize("shared_kinds", [0, 1, 2])
def test_group_shared_board_shows_every_eligible_material_even_with_few_kinds(shared_kinds):
    from qq_chat_analyzer.analysis.stickers import aggregate_stickers, StickerData, collect_sticker_sends
    from sticker_feature_samples import Messages
    from test_group_sticker_features import group_rows

    b = Messages("small-shared-board")
    for sender in "AB":
        b.add(0, sender, *(f"common-{i}" for i in range(shared_kinds)), f"personal-{sender}")
    rows = group_rows(b)
    report, = aggregate_stickers(StickerData(collect_sticker_sends(rows)), rows)
    chapter = echo_report_to_dict(build_echo_report_view(
        analysis_models.AnalysisReports(stickers=(report,)), conversation_kind="group"))["stickers"]
    assert chapter["shared_material_count"] == shared_kinds
    assert len(chapter["shared_materials"]) == shared_kinds
    assert all(item["count"] == 2 for item in chapter["shared_materials"])
    assert chapter["shared_ranking_min_materials"] == 1


def test_sticker_chapter_ranks_counts_and_limits_shared_exclusive_candidates():
    from dataclasses import replace
    from qq_chat_analyzer.analysis.stickers import (
        StickerConversationReport, StickerMaterialUsage, StickerParticipant,
    )

    report = StickerConversationReport(
        source="qq", conversation_id="fiction-room", conversation_type="private",
        message_count=20, total_send_count=18, unknown_identity_count=2, unknown_sender_count=1,
        members=(StickerParticipant("a", True, 10, 9), StickerParticipant("b", False, 10, 8)),
        materials=(
            StickerMaterialUsage("shared", "shared", 5, (("a", 2), ("b", 3))),
            StickerMaterialUsage("mine", "mine", 4, (("a", 4),)),
            StickerMaterialUsage("theirs", "theirs", 4, (("b", 4),)),
            StickerMaterialUsage("uncertain", "uncertain", 2, (("a", 1),), 1),
            StickerMaterialUsage("once", "once", 1, (("a", 1),)),
        ),
    )
    view = build_echo_report_view(analysis_models.AnalysisReports(stickers=(report,)), conversation_kind="private")
    chapter = echo_report_to_dict(view)["stickers"]
    assert chapter["available"] is True
    assert (chapter["total_send_count"], chapter["unique_material_count"]) == (18, 5)
    assert chapter["unattributed_send_count"] == 1
    mine, theirs = chapter["members"]
    assert (mine["unique_material_count"], theirs["unique_material_count"]) == (4, 2)
    assert (mine["send_count"], mine["per_hundred_messages"]) == (9, 90.0)
    assert [item["count"] for item in mine["top_materials"]] == [4, 2, 1, 1]
    assert [item["count"] for item in theirs["top_materials"]] == [4, 3]
    assert chapter["shared_material_count"] == 1
    assert chapter["shared_materials"] == []  # Private reports use the two personal boards.
    assert [m["exclusive_material_count"] for m in chapter["members"]] == [2, 1]
    assert mine["signature"] is None
    disjoint = replace(report, materials=report.materials[1:3])
    disjoint_view = build_echo_report_view(analysis_models.AnalysisReports(stickers=(disjoint,)), conversation_kind="private")
    assert echo_report_to_dict(disjoint_view)["stickers"]["shared_material_count"] == 0
    assert echo_report_to_dict(disjoint_view)["stickers"]["shared_materials"] == []
    serialized = json.dumps(chapter)
    assert "uncertain" not in serialized and "fiction-room" not in serialized
    assert len(chapter["assets"]) == 5
    assert all(a["status"] == "not_requested" and not a["data_uri"] for a in chapter["assets"].values())
    # A single occurrence remains eligible; zero denominator is serialized as null.
    tiny = replace(report, total_send_count=1, members=(StickerParticipant("a", True, 0, 1),),
                   materials=(StickerMaterialUsage("one", "one", 1, (("a", 1),)),))
    tiny_view = build_echo_report_view(analysis_models.AnalysisReports(stickers=(tiny,)), conversation_kind="private")
    payload = echo_report_to_dict(tiny_view)["stickers"]
    assert payload["members"][0]["per_hundred_messages"] is None
    assert payload["members"][0]["top_materials"][0]["count"] == 1
    assert payload["shared_materials"] == []
    assert payload["members"][0]["exclusive_material_count"] is None
    assert echo_report_to_dict(build_echo_report_view(conversation_kind="private", reports=None))["stickers"]["total_send_count"] == 0
    assert echo_report_to_dict(build_echo_report_view(analysis_models.AnalysisReports(stickers=(report,)), conversation_kind="group"))["stickers"]["available"] is False
    assert echo_report_to_dict(build_echo_report_view(analysis_models.AnalysisReports(stickers=(report, replace(report, source="wechat"))), conversation_kind="private"))["stickers"]["available"] is False


def test_sticker_member_names_only_disambiguate_missing_and_duplicate_nicknames():
    from qq_chat_analyzer.analysis.stickers import StickerConversationReport, StickerParticipant
    from qq_chat_analyzer.presentation.stickers import build_echo_stickers

    ids = tuple(f"fictional-source-{i}" for i in range(7))
    report = StickerConversationReport(
        "qq", "fiction-room", "group", 70, 7, 7, 0,
        members=tuple(StickerParticipant(sender, i == 0, 10, 1) for i, sender in enumerate(ids)),
    )
    chapter = build_echo_stickers((report,), (), conversation_kind="group", member_names={
        ids[0]: "小满", ids[1]: "同名", ids[2]: "同名", ids[3]: "同名（1）",
        ids[4]: "", ids[5]: ids[5], ids[6]: "未命名成员（1）",
    })
    labels = [member.label for member in chapter.members]
    assert labels == ["小满", "同名（2）", "同名（3）", "同名（1）",
                      "未命名成员（2）", "未命名成员（3）", "未命名成员（1）"]
    assert len(set(labels)) == len(labels)
    assert not any(sender in label for sender in ids for label in labels)
    assert all(member.unique_material_count == 0 for member in chapter.members)


def test_activity_analysis_preserves_report_distribution_and_density_contract(tmp_path):
    from qq_chat_analyzer.analysis.analyzers import ActivityAnalyzer
    from qq_chat_analyzer.message import ChatMessage

    messages = [
        ChatMessage(timestamp=value, sender="Fictional-Alice", message_type="text",
                    text="fictional", platform="fictional")
        for value in ("2024-01-01 08:00:00", "2024-01-01 08:30:00", "invalid")
    ]
    view = build_echo_report_view(analysis_models.AnalysisReports(
        activity=ActivityAnalyzer().analyze(messages),
    ), conversation_kind="group")
    payload = echo_report_to_dict(view)
    assert payload["schema_version"] == "echo-report.v0.19"
    assert "group_replies" not in payload  # Deferred to 0.4, including group reports.
    assert payload["overview"] == {
        "has_data": True, "total_message_count": 3, "participant_count": 0,
        "empty_description": "", "active_days": 1,
        "average_messages_per_active_day": 3.0,
    }
    assert set(payload["activity"]) == {"hourly", "weekday", "weekday_daily_average", "calendar_day_count"}
    assert payload["activity"]["calendar_day_count"] == 1
    assert payload["activity"]["weekday_daily_average"] == [{"label": "周一", "value": 2.0}]
    assert len(payload["activity"]["hourly"]) == 24
    assert len(payload["activity"]["weekday"]) == 7
    assert payload["activity"]["hourly"][8] == {"label": "08:00-08:59", "value": 2.0}
    assert payload["activity"]["weekday"][0] == {"label": "周一", "value": 2.0}
    output = export_echo_report_html(view, tmp_path / "echo-report.html")
    source = output.read_text(encoding="utf-8")
    assert all(marker not in source for marker in ("回复关系", 'id="replies"', 'href="#replies"'))
    match = re.search(r"window\.ECHO_DATA = (\{.*?\});", source, re.S)
    assert match is not None
    assert json.loads(match.group(1)) == payload



def test_group_openers_keep_each_identity_unknowns_and_display_names():
    from qq_chat_analyzer.analysis.conversation_sessions import analyze_conversation_sessions
    from qq_chat_analyzer.message import ChatMessage
    from qq_chat_analyzer.presentation.builders import _build_echo_sessions

    senders = ["a", "b", "a", "c", "", "b", "d"]
    report = analyze_conversation_sessions(tuple(ChatMessage(
        timestamp=1700000000 + index * 3600, sender=key, sender_id=key or None,
        message_type="text", text="fictional", platform="wechat",
        conversation_id="fictional", conversation_type="group", is_self=key == "c",
    ) for index, key in enumerate(senders)))
    members = tuple(EchoMemberCard(
        speaker_key=key, display_name=name, is_viewer=key == "c", message_count=2,
        message_share_percent=25, average_length=1, max_length=1, active_period="",
    ) for key, name in (("a", "同名"), ("b", "同名"), ("c", "虚构本人")))
    sessions = _build_echo_sessions(report, members=members)
    payload = echo_report_to_dict(EchoReportView(
        title="Fictional", conversation_kind="group", conversation_sessions=sessions,
    ))["conversation_sessions"]["group_initiators"]
    assert [(row["count"], row["is_viewer"]) for row in payload["members"]] == [
        (2, False), (2, False), (1, True), (1, False)]
    assert payload["unknown_count"] == 1
    assert sum(row["count"] for row in payload["members"]) + payload["unknown_count"] == 7
    assert len({row["display_name"] for row in payload["members"]}) == 4
    assert payload["members"][-1]["display_name"].startswith("未命名成员")
    assert all(set(row) == {"display_name", "count", "is_viewer", "search_names"} for row in payload["members"])
    assert payload["members"][0]["search_names"] == ("同名",)
    assert "fictional" not in repr(report.group_stats)


def test_echo_report_html_escapes_injected_user_text(tmp_path: Path) -> None:
    view = EchoReportView(
        title="余音 Echo",
        has_data=True,
        conversation_kind="group",
        conversation_name='<script>alert("x")</script>&"\'余音',
        time_span="1 天",
        total_message_count=1,
        participant_count=1,
        hourly_activity=(ChartPoint(label="09:00-09:59", value=1.0),),
        weekday_activity=(ChartPoint(label="周一", value=1.0),),
        members=(
            EchoMemberCard(
                speaker_key="fictional-alice",
                display_name="</script><b>x</b>",
                is_viewer=False,
                message_count=1,
                message_share_percent=100.0,
                average_length=1.0,
                max_length=1,
                active_period="09:00-09:59",
            ),
        ),
    )

    output_path = tmp_path / "echo-report.html"
    export_echo_report_html(view, output_path)
    html = output_path.read_text(encoding="utf-8")

    match = re.search(
        r"window\.ECHO_DATA = (\{.*?\});",
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["conversation"]["name"] == (
        '<script>alert("x")</script>&"\'余音'
    )
    assert payload["members"][0]["display_name"] == "</script><b>x</b>"
    assert html.count("</script>") == 2
    assert "<\\/script>" in html


def test_echo_report_html_inlines_data_and_expression_assets_without_local_paths(
    tmp_path: Path,
) -> None:
    culture = EchoExpressionCulture(
        available=True,
        expression_message_count=3,
        expression_only_message_count=1,
        expression_only_rate=0.33,
        unique_expression_count=1,
        top_expressions=(
            EchoExpressionItem(
                expression_key="捂脸",
                display_text="[捂脸]",
                count=3,
                kind="platform_face",
                with_text_message_count=2,
                text_only_message_count=1,
                asset_key="wechat:捂脸",
            ),
        ),
    )
    output_path = tmp_path / "echo-report.html"

    result_path = export_echo_report_html(
        EchoReportView(
            title="Echo Report",
            has_data=True,
            conversation_kind="group",
            conversation_name="虚构讨论组",
            language_profile=EchoLanguageProfile(
                mode="group_distinctive", available=True,
                members=(EchoLanguageMember(
                    speaker_key="fictional-alice", display_name="虚构 Alice",
                    heading="虚构 Alice", primary_words=("回声",),
                ),),
            ),
            expression_culture=culture,
        ),
        output_path,
    )

    assert result_path == output_path
    html = output_path.read_text(encoding="utf-8")
    assert "window.ECHO_DATA" in html
    assert "window.ECHO_ASSETS" in html
    assert "wechat:捂脸" in html
    assert "data:image/png;base64," in html
    assert "虚构讨论组" in html and "虚构 Alice" in html
    assert "fetch(" not in html
    assert "frontend/echo_report" not in html and "assets/branding" not in html
    assert 'href="style.css"' not in html and 'src="app.js"' not in html
    assert 'id="voices-intro"' in html and '"mode":"group_distinctive"' in html
    assert "D:" not in html
    assert "references" not in html


@pytest.mark.parametrize("key, expected", [
    ("⌚", "⌚"), ("1️⃣", "1️⃣"), ("★", "表情"), ("❤\ufe0e", "表情"),
])
def test_expression_token_fallback_uses_the_same_unicode_contract(key, expected):
    view = EchoReportView(title="Fictional Echo", language_profile=EchoLanguageProfile(
        mode="private_common", available=True,
        members=(EchoLanguageMember("fictional-a", "Fictional A", "A", ("expression:" + key,)),),
    ))
    payload = echo_report_to_dict(view)
    assert payload["language_profile"]["members"][0]["primary_words"] == [{"text": expected}]
