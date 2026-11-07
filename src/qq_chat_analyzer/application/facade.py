"""One stable application entry point for user-facing callers.

This module is the seam a future GUI talks to. It exists so that a window,
a widget, or any other presentation-side caller never has to know that
providers, import services, export files, or analyzer internals exist.

Deliberate boundaries:

* No provider is constructed here. Every collaborator is injected, so tests
  can supply stubs and the desktop wiring stays in one place.
* No analysis happens here. ``AnalysisApplicationService`` owns that, and the
  presentation builder owns formatting. This layer only decides *which*
  collaborator runs and in *what* order.
* No statistics are recomputed. Numbers travel from the analysis reports into
  the Echo report view untouched.
* Intermediate export files are an implementation detail. Generated Echo HTML
  is the exception: its exact local path is retained for the desktop caller.

Everything that escapes this layer is either a plain view model or a
:class:`FacadeError` carrying a stable ``code`` and a user-safe message.
"""

from __future__ import annotations

import logging
import re
import shutil
import stat
import threading
from time import monotonic as _monotonic, perf_counter as _perf_counter
from tempfile import TemporaryDirectory
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from ..resources import resources_dir, user_data_dir
from ..diagnostics import qq_session_stage
from ..analysis_diagnostics import timed_stage
from .shutdown_models import ShutdownResult, ShutdownStatus, ShutdownStepResult
from ..analysis.timestamps import to_epoch_seconds
from ..presentation import EchoReportView
from .analysis_phase import AnalysisPhase, PhaseCallback
from .dto import AnalysisRequestDTO, AnalysisResultDTO
from .errors import ApplicationServiceError
from .connection_models import ConnectionSnapshot
from .qq.qq_connection_manager import QQConnectionManager
from .qq.qq_connection_service import (
    QQConnectionService,
    QQConnectionStatus,
)
from .qq.qq_environment_config import QQEnvironmentConfig
from .echo_report_export import (
    ECHO_REPORT_HTML_NAME,
    _require_no_reparse_points,
    package_echo_report,
)
from .report_package_catalog import (
    ReportPackageCatalog,
    ReportPackageDeletionResult,
    ReportPackageListing,
)
from .report_package_metadata import build_report_metadata
from .update_check_service import UpdateChecker, UpdateCheckResult, UpdateCheckService
from .setup_uninstall_service import SetupUninstallService, UninstallResult
from .installable_update_service import (
    InstallableUpdater, InstallableUpdateResult, InstallableUpdateService, InstallHandoffResult,
)
from .wechat.wechat_connection_service import WeChatConnectionStatus
from .wechat.wechat_connection_progress import WeChatConnectionProgress
from .wechat.wechat_environment_config import WeChatEnvironmentConfig
from .wechat.wechat_export_import_service import WeChatExportImportRequest
from .wechat.wechat_setup_service import WeChatSetupStatus
from .scope_filter import AnalysisScope, AnalysisScopeMode, resolve_scope


_LOGGER = logging.getLogger("qq_chat_analyzer.desktop.facade")
# One foreground report, serial requests, no retries or lifetime lockout.
_ANALYSIS_OUTPUT_NAME = re.compile(r"chat-analyzer-output-[0-9a-f]{8}\Z")


class _RetainedReportDirectory:
    """Own one report directory while preserving its inherited ACL."""

    def __init__(self, parent: Path) -> None:
        _require_no_reparse_points(parent)
        self._parent = parent.resolve()
        while True:
            directory = self._parent / f"chat-analyzer-output-{uuid4().hex[:8]}"
            try:
                directory.mkdir()
            except FileExistsError:
                continue
            self.name = str(directory)
            break

    def cleanup(self) -> None:
        try:
            _remove_owned_analysis_output(Path(self.name), self._parent)
        except FileNotFoundError:
            pass
        except Exception:
            _LOGGER.exception("Analysis scratch could not be cleaned: %s", self.name)


def _remove_owned_analysis_output(directory: Path, root: Path) -> None:
    _require_no_reparse_points(root)
    if directory.parent != root or not _ANALYSIS_OUTPUT_NAME.fullmatch(directory.name):
        raise ValueError("Analysis scratch is outside its ownership boundary.")
    _require_no_reparse_points(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Analysis scratch is not a directory.")
    _require_safe_scratch_tree(directory)
    _require_no_reparse_points(directory)
    current = directory.lstat()
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise ValueError("Analysis scratch changed during cleanup.")
    shutil.rmtree(directory)


def _require_safe_scratch_tree(directory: Path) -> None:
    for child in directory.iterdir():
        _require_no_reparse_points(child)
        if stat.S_ISDIR(child.lstat().st_mode):
            _require_safe_scratch_tree(child)


def _cleanup_stale_analysis_outputs(root: Path) -> None:
    """Recover only direct scratch children; cleanup must not prevent startup."""
    try:
        _require_no_reparse_points(root)
        root = root.resolve()
        if not root.exists():
            return
        candidates = [
            child for child in root.iterdir() if _ANALYSIS_OUTPUT_NAME.fullmatch(child.name)
        ]
        candidates.sort(key=lambda child: child.name)
    except Exception:
        _LOGGER.exception("Stale analysis scratch could not be enumerated: %s", root)
        return
    for directory in candidates:
        try:
            _remove_owned_analysis_output(directory, root)
        except Exception:
            _LOGGER.exception("Stale analysis scratch could not be cleaned: %s", directory)


def _report_progress(
    progress: Callable[[str], None] | None,
    message: str,
) -> None:
    """Publish a facade-owned analysis stage when a caller is listening."""
    if progress is not None:
        progress(message)


def _publish_phase(
    on_phase: PhaseCallback | None,
    phase: AnalysisPhase,
) -> None:
    """Publish one structured phase when a caller is listening.

    This is additive next to :func:`_report_progress`: the Chinese status text
    stays the human-facing channel, the phase enum is the machine-facing one.
    Any exception the callback raises propagates unchanged, so the caller that
    injected it owns that failure and the resources around it still unwind.
    """
    if on_phase is not None:
        on_phase(phase)


DEFAULT_TOP = 50
DEFAULT_PROFILE = "default"

_PROFILE_STOPWORD_FILES = {
    "default": "stopwords.txt",
    "topic": "stopwords_topic.txt",
    "culture": "stopwords_culture.txt",
}

#: Each ordered shutdown step gets its own bounded window.  A step that
#: overruns it is abandoned so the steps after it still run - in particular
#: QQ runtime termination - which keeps the whole shutdown protocol finite.
DEFAULT_SHUTDOWN_STEP_SECONDS = 10.0

_SHUTDOWN_STEP_THREAD_PREFIX = "echo-shutdown-step-"
_SHUTDOWN_STEP_DIRECT_DB = "direct_db_cleanup"
_SHUTDOWN_STEP_RETAINED_OUTPUT = "retained_output_cleanup"
_SHUTDOWN_STEP_QQ_RUNTIME = "qq_runtime_termination"

#: Extra window the Direct DB step gets on top of the bounded budget the
#: service declares for itself (drain window plus recover readiness budget).
#: Without it a normal bounded cleanup is abandoned, the runtime is stopped
#: underneath it, and the process exit kills it mid-recover.
_DIRECT_DB_STEP_MARGIN_SECONDS = 5.0

class ChatSource(str, Enum):
    """Every chat origin a caller may choose from."""

    QQ = "qq"
    WECHAT = "wechat"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One selectable conversation, normalised across every source."""

    source: ChatSource
    session_id: str
    display_name: str
    session_type: str = "other"
    message_count: int | None = None
    message_available: bool = True
    unavailable_reason: str | None = None
    last_message_time: int | None = None
    search_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """One data source a caller may pick, plus whether it is usable."""

    source: ChatSource
    display_name: str
    available: bool
    description: str = ""


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    """Everything a caller may tune for one analysis run.

    ``scope_mode`` selects the analysis window. ``start_time`` and
    ``end_time`` are used for a custom range. Explicit legacy bounds are also
    treated as custom so existing callers retain their behavior.
    """

    start_time: Any = None
    end_time: Any = None
    scope_mode: AnalysisScopeMode = AnalysisScopeMode.ALL
    top: int = DEFAULT_TOP
    profile: str = DEFAULT_PROFILE
    output_directory: Path | None = None
    font_path: str | None = None
    # None inherits the selected provider's explicit configuration; () opts out.
    sticker_media_roots: tuple[Path, ...] | None = field(default=None, repr=False)

    def with_output_directory(self, directory: Path) -> "AnalysisConfig":
        """Return a copy that writes artifacts into ``directory``."""
        return replace(self, output_directory=directory)


class FacadeError(Exception):
    """The single error type a caller outside this layer has to handle."""

    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        source: ChatSource | None = None,
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.source = source


class UnknownChatSource(FacadeError):
    """Raised when a caller asks for a source this layer cannot serve."""

    def __init__(self, source: Any) -> None:
        super().__init__(
            code="unknown_source",
            public_message=(
                "\u4e0d\u652f\u6301\u7684\u6570\u636e\u6765\u6e90"
                f"\uff1a{source}\u3002"
            ),
        )


class SourceUnavailable(FacadeError):
    """Raised when a source was requested but no service was wired for it."""

    def __init__(self, source: ChatSource) -> None:
        super().__init__(
            code="source_unavailable",
            public_message=_SOURCE_UNAVAILABLE_MESSAGES[source],
            source=source,
        )


_SOURCE_UNAVAILABLE_MESSAGES = {
    ChatSource.QQ: (
        "QQ \u6570\u636e\u6e90\u6682\u4e0d\u53ef\u7528\u3002"
    ),
    ChatSource.WECHAT: (
        "\u5fae\u4fe1\u6570\u636e\u6e90\u5c1a\u672a\u914d\u7f6e\u3002"
    ),
}

@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """What one finished analysis hands back to a caller.

    ``result`` is the privacy-safe analysis DTO. ``echo_report_view`` supports
    report sharing, and ``session`` identifies the analyzed conversation.
    The desktop opens the generated HTML at ``report_path``.
    """

    result: AnalysisResultDTO
    source: ChatSource
    session: SessionInfo | None = None
    artifact_directory: Path | None = field(default=None, repr=False)
    report_path: Path | None = field(default=None, repr=False)
    report_directory: Path | None = field(default=None, repr=False)
    echo_report_view: EchoReportView | None = field(default=None, repr=False)
    report_generated_at: datetime | None = None
    analysis_scope: AnalysisScope = field(default_factory=AnalysisScope.all, repr=False)


@dataclass(frozen=True, slots=True)
class _SessionExport:
    """Internal export path for one session."""

    payload_path: Path
    session: SessionInfo | None = None
    conversation_id: str | None = None
    sticker_media_roots: tuple[Path, ...] = field(default=(), repr=False)


# One fixed stage -> (facade code, public message) pair for a QQ media fetch.
# Only these stages exist, so no path, account, message ID or kernel text is
# ever shown; an unrecognised failure keeps the generic message.
_QQ_MEDIA_STAGE_FAILURES = {
    "probe_timeout": (
        "qq_media_query_timeout", "QQ 未在限定时间内返回消息，未获取图片。",
    ),
    "probe_api_unavailable": (
        "qq_media_api_unavailable", "当前 QQ 运行时不支持图片消息查询，未获取图片。",
    ),
    "transport_timeout": (
        "qq_media_transport_timeout", "图片请求等待超时，发送统计已保留。",
    ),
    "media_transport_timeout": (
        "qq_media_transport_timeout", "图片内容传输超时，发送统计已保留。",
    ),
    "media_transport_limit": (
        "qq_media_transport_limit", "图片内容超过传输上限，发送统计已保留。",
    ),
    "media_transport_invalid": (
        "qq_media_transport_invalid", "未收到完整的图片内容，发送统计已保留。",
    ),
    "media_message_query_failed": (
        "qq_media_message_query_failed",
        "QQ 内核消息查询失败，未获取媒体。请确认 QQ 已登录后重试。",
    ),
    "media_mapping_rejected": (
        "qq_media_mapping_rejected",
        "QQ 内核未能唯一确认这条消息的图片元素，未获取媒体。",
    ),
    "media_api_unavailable": (
        "qq_media_api_unavailable",
        "当前 QQ 运行时不支持图片下载接口，未获取媒体。",
    ),
    "media_busy": (
        "qq_media_busy",
        "上一次图片获取尚未结束，请稍后重试。",
    ),
    "media_budget_exhausted": (
        "qq_media_budget_exhausted",
        "短时间内图片请求较多，请稍后重试。",
    ),
    "media_download_timeout": (
        "qq_media_download_timeout",
        "QQ 未在限定时间内完成图片下载，未展示。",
    ),
    "media_download_failed": (
        "qq_media_download_failed",
        "QQ 下载接口返回失败，未展示。",
    ),
    "media_download_no_path": (
        "qq_media_download_no_path",
        "QQ 下载接口未返回图片文件位置，未展示。",
    ),
    "media_download_no_cache_hint": (
        "qq_media_download_no_cache_hint",
        "QQ 未提供原图缓存位置，下载接口也未返回图片文件，未展示。",
    ),
    "media_path_type_invalid": (
        "qq_media_path_type_invalid",
        "QQ 下载接口返回的图片位置类型异常，未展示。",
    ),
    "media_path_not_absolute": (
        "qq_media_path_not_absolute",
        "QQ 下载接口未返回绝对文件位置，未展示。",
    ),
    "media_path_invalid": (
        "qq_media_path_invalid",
        "QQ 返回的图片位置不可安全读取，未展示。",
    ),
    "media_file_missing": (
        "qq_media_file_missing",
        "QQ 返回的图片文件已不存在，未展示。",
    ),
    "media_open_failed": (
        "qq_media_open_failed",
        "无法打开 QQ 返回的图片文件（文件被占用或拒绝访问），未展示。",
    ),
    "media_file_type_rejected": (
        "qq_media_file_type_rejected",
        "QQ 返回的位置不是常规文件（目录、链接或特殊文件），未展示。",
    ),
    "media_file_identity_changed": (
        "qq_media_file_identity_changed",
        "QQ 返回的路径在打开前后指向了不同文件，未展示。",
    ),
    "media_file_empty": (
        "qq_media_file_empty",
        "QQ 返回的图片文件为空（0 字节），未展示。",
    ),
    "media_cancelled": ("qq_media_cancelled", "已取消，未保存媒体。"),
    "media_transfer_invalid": ("qq_media_transfer_invalid", "图片分块传输未完整通过验证，未展示。"),
    "media_file_too_large": (
        "qq_media_too_large",
        "取回的图片超过 32 MiB 上限，未展示。",
    ),
    "media_read_incomplete": (
        "qq_media_read_incomplete",
        "图片文件读取不完整（读取长度与文件大小不符），未展示。",
    ),
    "media_file_changed": (
        "qq_media_file_changed",
        "图片文件在读取过程中发生变化（大小或身份改变），未展示。",
    ),
    "media_file_rejected": (
        "qq_media_file_rejected",
        "取回的图片文件未通过其他安全校验，未展示。",
    ),
}


class ChatAnalyzerFacade:
    """Coordinate sources, analysis, and presentation behind one surface."""

    @staticmethod
    def associate_sticker_media(data, *, media_keys, roots=(), source, cancelled=lambda: False,
                                diagnostics=None):
        """Associate an explicit batch without acquiring another chat database."""
        from .sticker_media import associate_sticker_media

        try:
            with timed_stage("sticker_media_associate"):
                return associate_sticker_media(
                    data, media_keys=media_keys, roots=roots, source=source, cancelled=cancelled,
                    diagnostics=diagnostics)
        except Exception:
            # Worker diagnostics must not receive private paths or decoder data.
            raise FacadeError(code="sticker_media_failed", public_message="原图验证失败，请稍后重试。") from None

    def _qq_connected_provider(self, *required: str) -> Any:
        """Return the connected QQ provider without constructing a second one."""
        connection = self._qq_connection_service
        if connection is None:
            bundle = self._built_sources.get(ChatSource.QQ)
            connection = getattr(bundle, "connection", None)
        factory = getattr(connection, "provider", None)
        if not callable(factory):
            raise FacadeError(
                code="qq_mapping_unavailable",
                public_message="QQ 尚未连接，无法验证消息映射。",
            )
        try:
            provider = factory()
        except Exception:
            raise FacadeError(
                code="qq_mapping_unavailable",
                public_message="QQ 尚未连接，无法验证消息映射。",
            ) from None
        if any(not callable(getattr(provider, name, None)) for name in required):
            raise FacadeError(
                code="qq_mapping_unavailable",
                public_message="当前 QQ 运行时不支持消息映射查询。",
            )
        return provider

    def _fetch_qq_pic(
        self, data, *, media_key: str, session_id: str, stored_facts, provider,
        cancelled=lambda: False, occurrence_id: str | None = None,
        deadline: float | None = None, on_stage=lambda stage: None,
    ):
        """Verify the report target against its owned payload and authenticated QQ element."""
        from .sticker_media import verify_fetched_media

        def remaining(maximum):
            if cancelled():
                raise FacadeError(code="qq_media_cancelled", public_message="已取消，未获取媒体。")
            seconds = maximum if deadline is None else min(maximum, deadline - _monotonic())
            if seconds <= 0:
                raise FacadeError(code="qq_media_time_budget", public_message="本次图片获取已结束，发送统计已保留。")
            return seconds

        on_stage("selection")
        send = next((
            item for item in getattr(data, "sends", ())
            if item.media_key == media_key and item.source == "qq"
            and (occurrence_id is None or item.occurrence_id == occurrence_id)
            and item.conversation_type in ("private", "group")
            and item.identity_basis == "qq_pic_45406_md5"
            and isinstance(item.message_id, str) and item.message_id
            and isinstance(item.content_md5, str) and re.fullmatch(r"[0-9a-f]{32}", item.content_md5)
        ), None)
        if send is None:
            raise FacadeError(
                code="qq_media_invalid_selection",
                public_message="所选素材缺少可验证的 QQ 图片依据，未获取媒体。",
            )
        group = send.conversation_type == "group"
        if group:
            if occurrence_id is not None and sum(s.occurrence_id == occurrence_id for s in data.sends) != 1:
                raise FacadeError(code="qq_media_group_message_unconfirmed",
                                  public_message="无法在本次分析结果中唯一确认所选发送记录，未下载图片。")
            if not (isinstance(session_id, str) and session_id.startswith("group:") and session_id[6:]):
                raise FacadeError(code="qq_media_group_scope_unconfirmed",
                                  public_message="所选素材与当前群聊范围不一致，未下载图片。")
            if not all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{4,19}", value)
                       for value in (send.conversation_id, send.sender_id)):
                raise FacadeError(code="qq_media_invalid_selection",
                                  public_message="所选素材缺少可信群号或成员身份，未下载图片。")
        if cancelled():
            raise FacadeError(code="qq_media_cancelled", public_message="已取消，未获取媒体。")
        on_stage("stored_message")
        facts = stored_facts
        if len(facts) != 1:
            on_stage("stored_message_missing" if not facts else "stored_message_ambiguous")
            raise FacadeError(
                code="qq_media_message_unconfirmed",
                public_message="无法在本次会话数据中唯一确认该素材对应的消息，未获取媒体。",
            )
        fact = facts[0]
        on_stage("stored_identity")
        if group:
            for confirmed, code, message in (
                (fact.conversation_type == "group" and fact.group_id == send.conversation_id
                 and fact.conversation_id == send.conversation_id, "scope", "群号及会话范围"),
                (fact.sender_uin == send.sender_id and fact.is_self is send.is_self, "sender", "发送者身份"),
                (fact.timestamp == send.timestamp, "time", "消息时间"),
            ):
                if not confirmed:
                    raise FacadeError(code=f"qq_media_group_{code}_unconfirmed",
                                      public_message=f"群聊入库事实无法确认{message}，未下载图片。")
        on_stage("stored_element")
        custom = [element for element in fact.elements if element.is_custom_sticker]
        if len(custom) != 1 or custom[0].content_md5 != send.content_md5:
            raise FacadeError(
                code="qq_media_element_unconfirmed",
                public_message="该消息的图片元素与所选素材摘要不一致，未获取媒体。",
            )
        element = custom[0]
        if group and not element.file_uuid:
            raise FacadeError(code="qq_media_group_uuid_unconfirmed",
                              public_message="群聊消息缺少唯一文件 UUID，未下载图片。")
        if (
            fact.conversation_type != send.conversation_type
            or not (fact.group_id if group else fact.peer_uid)
            or not element.segment_id
            or not isinstance(fact.timestamp, int)
            or isinstance(fact.timestamp, bool)
        ):
            raise FacadeError(
                code="qq_media_element_unconfirmed",
                public_message="该消息缺少可唯一确认的图片元素标识，未获取媒体。",
            )
        locator = {
            "msg_id": fact.message_id,
            "peer_uid": fact.group_id if group else fact.peer_uid,
            "expected_time": fact.timestamp,
            "expected_md5": element.content_md5,
            "expected_file_uuid": element.file_uuid,
            "expected_self": fact.is_self,
        }
        if group:
            locator.update(chat_type=2, expected_sender_uin=fact.sender_uin, expected_sender_uid=fact.sender_uid)
        timeout = remaining(8)
        on_stage("kernel_query")
        try:
            mapping = provider.resolve_pic_message(**locator, expected_segment_id=element.segment_id,
                                                   **({"timeout": timeout} if deadline is not None else {}))
        except Exception as error:
            if getattr(error, "rpc_error", None) == "media_budget_exhausted":
                raise FacadeError(code="qq_media_budget_exhausted",
                                  public_message="QQ 素材获取达到当前时间窗口上限，已保留统计。") from None
            code, message = _QQ_MEDIA_STAGE_FAILURES.get(
                getattr(error, "rpc_error", None),
                ("qq_media_query_failed", "QQ 内核消息查询失败，未获取媒体。"))
            raise FacadeError(code=code, public_message=message) from None
        on_stage("kernel_message_missing" if not mapping.found else
                 "kernel_message_ambiguous" if not mapping.unique else
                 "kernel_element_unconfirmed" if mapping.matched_pic_count != 1 else "kernel_identity")
        if group:
            for confirmed, code, message in (
                (mapping.found and mapping.unique and mapping.msg_id_matches, "message", "唯一消息及消息 ID"),
                (mapping.peer_matches, "peer", "群身份及聊天类型"),
                (mapping.sender_matches is True, "sender", "发送者身份"),
                (mapping.time_matches, "time", "消息时间"),
                (mapping.matched_pic_count == 1 and mapping.segment_id_matches is True, "element", "唯一 PIC 元素及元素 ID"),
                (mapping.file_uuid_matches is True, "uuid", "文件 UUID"),
                (mapping.md5_matches, "digest", "图片摘要"),
            ):
                if not confirmed:
                    raise FacadeError(code=f"qq_media_group_{code}_unconfirmed",
                                      public_message=f"QQ 内核未能确认群聊{message}，未下载图片。")
        if not (
            mapping.passed
            and mapping.unique
            and mapping.matched_pic_count == 1
            and mapping.md5_matches
            and mapping.segment_id_matches is True
            and mapping.file_uuid_matches is not False
        ):
            raise FacadeError(
                code="qq_media_mapping_rejected",
                public_message="QQ 内核未能唯一确认这条消息的图片元素，未获取媒体。",
            )
        if cancelled():
            raise FacadeError(code="qq_media_cancelled", public_message="已取消，未获取媒体。")
        # Download RPC includes the repeated 8-second identity query before
        # QQ's 120-second native operation, plus transport/file-open margin.
        timeout = remaining(135)
        on_stage("kernel_download")
        try:
            with timed_stage("sticker_media_download"):
                raw = provider.download_pic_media(**locator, element_id=element.segment_id,
                                                  cancelled=cancelled, timeout=timeout)
        except Exception as error:
            code, message = _QQ_MEDIA_STAGE_FAILURES.get(
                getattr(error, "stage", None),
                ("qq_media_fetch_failed",
                 "QQ 未返回可用的图片内容，未展示。请确认 QQ 在线后重试。"),
            )
            raise FacadeError(code=code, public_message=message) from None
        if cancelled():
            raise FacadeError(code="qq_media_cancelled", public_message="已取消，未保存媒体。")
        remaining(2)
        on_stage("decode")
        with timed_stage("sticker_media_verify"):
            media = verify_fetched_media(
                raw, media_key=media_key, expected_md5=element.content_md5, cancelled=cancelled)
        if media.status == "cancelled":
            raise FacadeError(code="qq_media_cancelled", public_message="已取消，未保存媒体。")
        if (media.status, media.reason) == ("limited", "decode_budget"):
            raise FacadeError(
                code="qq_media_decode_budget",
                public_message="QQ 返回的图片超过解码安全预算（画布、累计像素、逐帧工作量或验证耗时），未展示。",
            )
        if media.status not in {"available", "preview"}:
            raise FacadeError(
                code="qq_media_invalid_content",
                public_message="QQ 返回的内容未通过图片验证，未展示。",
            )
        remaining(2)
        current = {item.media_key: item for item in getattr(data, "media", ())}
        current[media_key] = media
        return replace(data, media=tuple(current.values()))

    def _prepare_qq_report_media(self, result, payload_path, session, roots, progress, cancelled, *, batch=None):
        """Recover only this retained view's targets from its still-owned payload."""
        from collections import Counter, defaultdict
        from ..analysis.stickers import StickerMedia
        from ..presentation.stickers import sticker_report_targets
        from ..media_content import StoredMedia
        from ..qq_db_adapter import extract_qq_pic_media_facts, load_qq_db_json
        from .sticker_media import qq_media_failure

        view = result.echo_report_view
        targets = sticker_report_targets(view.stickers if view else None)
        data = result.stickers
        if not targets:
            return data
        batch = batch or uuid4().hex[:12]
        started_at = _perf_counter()
        attempts = query_calls = download_calls = completed = 0
        def check_cancel():
            closing = self._sources_closing.is_set()
            if closing or cancelled():
                _LOGGER.info("[sticker-media-stop] batch=%s attempts=%d queries=%d downloads=%d completed=%d elapsed_ms=%d",
                             batch, attempts, query_calls, download_calls, completed,
                             max(0, round((_perf_counter() - started_at) * 1000)))
                raise FacadeError(code="application_closing" if closing else "analysis_cancelled",
                                  public_message="Echo 正在关闭。" if closing else "分析已取消。")

        def stopped():
            return cancelled() or self._sources_closing.is_set()

        sends = {}
        seen_messages = set()
        target_keys = {target.media_key for target in targets}
        for send in data.sends:
            if (send.source != "qq" or send.identity_basis != "qq_pic_45406_md5"
                    or send.media_key not in target_keys):
                continue
            candidates = sends.setdefault(send.media_key, [])
            if candidates and (send.conversation_type, send.conversation_id, send.content_md5) != (
                    candidates[0].conversation_type, candidates[0].conversation_id, candidates[0].content_md5):
                continue
            message_key = (send.media_key, send.message_id)
            if message_key not in seen_messages:
                candidates.append(send)
                seen_messages.add(message_key)
        # Preserve the original representative; after a missing resource, prefer
        # recent sends of the same material in this exact retained conversation.
        for candidates in sends.values():
            candidates[1:] = reversed(candidates[1:])
        candidate_indexes = defaultdict(int)
        owned_media = []
        def retain(item):
            try:
                stored = StoredMedia(item.original_bytes)
            except OSError:
                raise FacadeError(code="qq_media_storage_failed",
                                  public_message="表情素材临时存储写入失败，请检查磁盘空间。") from None
            owned_media.append(stored)
            return replace(item, stored=stored, original_bytes=None)

        keys = tuple(target.media_key for target in targets if target.media_key in sends)
        target_order = {target.media_key: index for index, target in enumerate(targets, 1)}
        try:
            with timed_stage("sticker_report_media"):
                _report_progress(progress, "正在获取表情包素材...")
                check_cancel()
                # No discovery: use only caller/source-owned, previously trusted scopes.
                if roots:
                    _LOGGER.info("[sticker-media-local] batch=%s state=start main_thread=%s", batch,
                                 threading.current_thread() is threading.main_thread())
                    for offset in range(0, len(keys), 32):
                        check_cancel()
                        try:
                            data = self.associate_sticker_media(
                                data, media_keys=keys[offset:offset + 32], roots=roots, source="qq",
                                cancelled=stopped)
                        except FacadeError:
                            # A rejected local scope does not authorize a broader scan.
                            # The separately verified QQ element may still be fetched.
                            check_cancel()
                    _LOGGER.info("[sticker-media-local] batch=%s state=finished", batch)
                check_cancel()
                media = {item.media_key: item for item in data.media}
                for key, item in tuple(media.items()):
                    if item.status in {"available", "preview"} and item.original_bytes:
                        media[key] = retain(item)
                completed = sum(item.status in {"available", "preview"} and bool(item.original_bytes or item.stored)
                                for item in media.values())
                pending = tuple(key for key in keys if not (
                    key in media and media[key].status in {"available", "preview"} and (media[key].original_bytes or media[key].stored)))
                facts_by_message = defaultdict(list)
                pending_keys = set(pending)
                provider, setup_failure = None, None
                for target in targets:
                    disposition = ("pending" if target.media_key in pending_keys else
                                   "local" if target.media_key in keys else "unsupported")
                    _LOGGER.info("[sticker-media-plan] batch=%s target=%d disposition=%s", batch,
                                 target_order[target.media_key], disposition)
                if pending:
                    try:
                        # Reuse exactly the payload just analyzed, never reacquire a snapshot.
                        with timed_stage("sticker_report_identity"):
                            payload = load_qq_db_json(payload_path)
                            query = payload.get("query", {}) if payload else {}
                            kind, internal_key = session.session_id.split(":", 1)
                            if query.get("session_type") != kind or query.get("internal_key") != internal_key:
                                setup_failure = "report_scope_unconfirmed"
                            message_ids = {send.message_id for key in pending for send in sends[key]}
                            for fact in extract_qq_pic_media_facts(payload, message_ids):
                                facts_by_message[fact.message_id].append(fact)
                            del payload
                    except Exception:
                        setup_failure = "report_payload_unavailable"
                    if setup_failure is None:
                        try:
                            provider = self._qq_connected_provider("resolve_pic_message", "download_pic_media")
                        except FacadeError:
                            setup_failure = "report_media_source_unavailable"
                index = 0
                while index < len(pending):
                    key = pending[index]
                    ordinal = target_order[key]
                    check_cancel()
                    item_started = _perf_counter()
                    current_stage = "not_requested"
                    stage_started = item_started
                    def trace(stage):
                        nonlocal current_stage, stage_started, query_calls, download_calls
                        query_calls += stage == "kernel_query"
                        download_calls += stage == "kernel_download"
                        now = _perf_counter()
                        _LOGGER.info("[sticker-media-stage] batch=%s target=%d stage=%s elapsed_ms=%d next=%s main_thread=%s",
                                     batch, ordinal, current_stage, max(0, round((now - stage_started) * 1000)),
                                     stage, threading.current_thread() is threading.main_thread())
                        current_stage, stage_started = stage, now
                    if setup_failure:
                        media[key] = StickerMedia(key, "failed", setup_failure)
                        code = setup_failure
                    else:
                        attempts += 1
                        _report_progress(progress, f"正在获取表情包素材（{index + 1}/{len(pending)}）...")
                        try:
                            wait_ready = getattr(provider, "wait_media_ready", None)
                            if callable(wait_ready):
                                trace("waiting_bridge")
                                wait_ready(cancelled=stopped)
                                check_cancel()
                            send = sends[key][candidate_indexes[key]]
                            recovered = self._fetch_qq_pic(
                                replace(data, media=tuple(media.values())), media_key=key,
                                session_id=session.session_id, occurrence_id=send.occurrence_id,
                                stored_facts=tuple(facts_by_message[send.message_id]),
                                provider=provider, cancelled=stopped, on_stage=trace)
                            item = next(item for item in recovered.media if item.media_key == key)
                            if item.original_bytes:
                                item = retain(item)
                            media[key] = item
                            del recovered
                            completed += 1
                            code = item.status
                        except Exception as cause:
                            if isinstance(cause, FacadeError):
                                error = cause
                            else:
                                stage = getattr(cause, "stage", None)
                                code, message = _QQ_MEDIA_STAGE_FAILURES.get(stage,
                                    ("qq_media_fetch_failed", "QQ 图片获取失败，发送统计已保留。"))
                                error = FacadeError(code=code, public_message=message)
                            code = error.code
                            check_cancel()
                            if error.code in {"qq_media_budget_exhausted", "qq_media_busy"} and callable(wait_ready):
                                _LOGGER.info("[sticker-media-wait] batch=%s target=%d code=%s", batch, ordinal, code)
                                # The next pass waits for the same bridge; no target
                                # is abandoned and no native download overlaps.
                                continue
                            next_candidate = candidate_indexes[key] + 1
                            if error.code in {"qq_media_download_no_path", "qq_media_download_no_cache_hint",
                                              "qq_media_file_missing"} and next_candidate < len(sends[key]):
                                candidate_indexes[key] = next_candidate
                                _LOGGER.info("[sticker-media-candidate] batch=%s target=%d code=%s next=%d candidates=%d",
                                             batch, ordinal, code, next_candidate + 1, len(sends[key]))
                                continue
                            _LOGGER.info("[sticker-media-result] batch=%s target=%d stage=%s code=%s elapsed_ms=%d",
                                         batch, ordinal, current_stage, code, max(0, round((_perf_counter() - item_started) * 1000)))
                            media[key] = qq_media_failure(key, error.code)
                            index += 1
                            continue
                    _LOGGER.info("[sticker-media-result] batch=%s target=%d stage=%s code=%s elapsed_ms=%d",
                                 batch, ordinal, current_stage, code, max(0, round((_perf_counter() - item_started) * 1000)))
                    index += 1
                check_cancel()
                data = replace(data, media=tuple(media.values()))
            # Fixed categories/counts only; never log locators, paths, bytes or exception text.
            counts = Counter(media[t.media_key].status if t.media_key in media else "not_requested" for t in targets)
            _LOGGER.info("[sticker-report-media] targets=%d attempts=%d available=%d preview=%d limited=%d missing=%d failed=%d unsupported=%d not_requested=%d",
                         len(targets), attempts, counts["available"], counts["preview"], counts["limited"],
                         counts["missing"], sum(counts[s] for s in ("failed", "invalid", "unsafe", "unreadable", "ambiguous")),
                         counts["unsupported"], counts["not_requested"])
            reasons = Counter(media[t.media_key].reason for t in targets if t.media_key in media)
            _LOGGER.info("[sticker-media-batch] batch=%s targets=%d eligible=%d attempts=%d completed=%d elapsed_ms=%d",
                         batch, len(targets), len(keys), attempts, counts["available"] + counts["preview"],
                         max(0, round((_perf_counter() - started_at) * 1000)))
            _LOGGER.info("[sticker-media-calls] batch=%s queries=%d downloads=%d", batch, query_calls, download_calls)
            _LOGGER.info("[sticker-report-media-failures] mapping=%d source=%d fetch=%d resource_budget=%d request_budget=%d time_budget=%d asset_budget=%d",
                         reasons["message_mapping_unconfirmed"], sum(reasons[r] for r in
                             ("report_media_source_unavailable", "report_payload_unavailable", "report_scope_unconfirmed")),
                         counts["failed"], sum(reasons[r] for r in
                             ("qq_media_too_large", "qq_media_decode_budget", "qq_media_transport_limit")),
                         reasons["report_media_request_budget"], reasons["report_media_time_budget"],
                         reasons["report_asset_budget"])
            return data
        except BaseException:
            for stored in owned_media:
                stored.close()
            raise

    def _prepare_wechat_report_media(self, result, progress, cancelled, *, batch=None):
        """Fetch only this report's selected WeChat materials from the CDN.

        Materials already verified from an authorized local root are never
        requested again. Only the union of the retained rankings is acquired,
        and every result stays a fixed status/reason; URLs are never logged.
        A recovery failure keeps the completed local analysis; an explicit
        cancellation is never converted into a fallback export.
        """
        from collections import Counter
        from ..presentation.stickers import REPORT_STICKER_BYTE_BUDGET, sticker_report_targets
        from .wechat.wechat_sticker_cdn import recover_wechat_sticker_media

        view = result.echo_report_view
        targets = sticker_report_targets(view.stickers if view else None)
        data = result.stickers
        if not targets:
            return data
        batch = batch or uuid4().hex[:12]
        started_at = _perf_counter()
        keys = tuple(target.media_key for target in targets if target.media_key)

        def stopped():
            return cancelled() or self._sources_closing.is_set()

        with timed_stage("sticker_report_media"):
            _report_progress(progress, "正在获取表情包素材...")
            try:
                data = recover_wechat_sticker_media(
                    data,
                    media_keys=keys,
                    max_report_bytes=REPORT_STICKER_BYTE_BUDGET,
                    cancelled=stopped,
                    progress=lambda text: _report_progress(progress, text),
                )
            except Exception:
                # Cancellation must not turn into a fallback report; any other
                # failure keeps the already completed local analysis.
                if self._sources_closing.is_set():
                    raise FacadeError(
                        code="application_closing", public_message="Echo 正在关闭。"
                    ) from None
                if cancelled():
                    raise FacadeError(
                        code="analysis_cancelled", public_message="分析已取消。"
                    ) from None
                _LOGGER.info(
                    "[sticker-media-cdn] batch=%s state=failed code=wechat_media_recovery_failed",
                    batch)
                return result.stickers
        media = {item.media_key: item for item in data.media}
        counts = Counter(media[t.media_key].status if t.media_key in media else "not_requested"
                         for t in targets)
        _LOGGER.info(
            "[sticker-media-cdn] batch=%s state=finished targets=%d available=%d "
            "limited=%d missing=%d failed=%d unsupported=%d elapsed_ms=%d",
            batch, len(targets), counts["available"], counts["limited"], counts["missing"],
            sum(counts[s] for s in ("failed", "invalid", "cancelled")), counts["unsupported"],
            max(0, round((_perf_counter() - started_at) * 1000)))
        return data

    def __init__(
        self,
        *,
        qq_service: Any = None,
        qq_connection_service: Any = None,
        qq_setup_service: Any = None,
        qq_connection_manager: Any = None,
        qq_auth_bridge: Any = None,
        qq_process_registry: Any = None,
        qq_runtime_session: Any = None,
        wechat_service: Any = None,
        wechat_connection_service: Any = None,
        wechat_setup_service: Any = None,
        source_builders: (
            dict[ChatSource, Callable[[], Any]] | None
        ) = None,
        analysis_service: Any = None,
        report_package_catalog: Any = None,
        update_check_service: UpdateChecker | None = None,
        installable_update_service: InstallableUpdater | None = None,
        setup_uninstall_service: SetupUninstallService | None = None,
        stopwords_directory: Path | None = None,
        shutdown_step_seconds: float = DEFAULT_SHUTDOWN_STEP_SECONDS,
    ) -> None:
        self._services: dict[ChatSource, Any] = {
            ChatSource.QQ: qq_service,
            ChatSource.WECHAT: wechat_service,
        }
        self._qq_connection_service = qq_connection_service
        self._qq_setup_service = qq_setup_service
        self._qq_connection_manager = qq_connection_manager
        self._qq_auth_bridge = qq_auth_bridge
        self._qq_process_registry = qq_process_registry
        self._qq_runtime_session = qq_runtime_session
        self._wechat_connection_service_value = wechat_connection_service
        self._wechat_setup_service_value = wechat_setup_service
        self._source_builders = dict(source_builders or {})
        self._built_sources: dict[ChatSource, Any] = {}
        self._analysis_service = analysis_service
        self._report_package_catalog = report_package_catalog or ReportPackageCatalog()
        self._update_check_service = (
            update_check_service if update_check_service is not None else UpdateCheckService()
        )
        self._installable_update_service = (
            installable_update_service if installable_update_service is not None
            else InstallableUpdateService()
        )
        self._stopwords_directory = stopwords_directory or resources_dir()
        self._setup_uninstall_service = setup_uninstall_service or SetupUninstallService()
        self._retained_output_directory: _RetainedReportDirectory | None = None
        self._scratch_recovery_done = False
        self._shutdown_step_seconds = shutdown_step_seconds
        self._shutdown_lock = threading.Lock()
        self._shutdown_result: ShutdownResult | None = None
        self._sources_closing = threading.Event()
        self._source_publication_lock = threading.Lock()

    def can_uninstall_setup(self) -> bool:
        return self._setup_uninstall_service.is_available()

    def prepare_setup_uninstall(self, cleanup_mode: str = 'keep') -> UninstallResult:
        if self._sources_closing.is_set():
            return UninstallResult('denied', public_message='Echo 已在退出，请重新启动后再试。')
        return self._setup_uninstall_service.prepare(cleanup_mode)

    def handoff_setup_uninstall(self, token: str, result: ShutdownResult) -> UninstallResult:
        if (not isinstance(result, ShutdownResult) or not result.can_apply_update
                or self._shutdown_result is None
                or tuple(step for step in result.steps if step.name != 'gui_workers')
                   != self._shutdown_result.steps):
            return UninstallResult('denied', public_message='安全退出证据不完整，未启动卸载。')
        return self._setup_uninstall_service.handoff(token, result)

    def check_for_updates(self, *, manual: bool = False) -> UpdateCheckResult:
        """Check on a worker; construction/startup never initiates a request."""
        try:
            return self._update_check_service.check(manual=manual)
        except Exception as exc:
            raise FacadeError(
                code="update_check_failed",
                public_message="暂时无法检查更新，请稍后手动重试。",
            ) from exc

    def check_installable_update(self) -> InstallableUpdateResult:
        """Worker-only Velopack feed check, separate from release-page checks."""
        try:
            return self._installable_update_service.check()
        except Exception as exc:
            raise FacadeError(code="installable_update_check_failed",
                              public_message="暂时无法检查可安装更新，请稍后重试。") from exc

    def download_update(
        self, *, progress: Callable[[int], None] | None = None,
    ) -> InstallableUpdateResult:
        """Worker-only download. Callbacks must marshal into the GUI thread."""
        try:
            return self._installable_update_service.download(progress=progress)
        except Exception as exc:
            raise FacadeError(code="update_download_failed",
                              public_message="更新下载未完成，请稍后重试。") from exc

    def get_update_status(self) -> InstallableUpdateResult:
        """Read an immutable in-memory snapshot; never grants apply permission."""
        try:
            return self._installable_update_service.get_status()
        except Exception as exc:
            raise FacadeError(code="update_status_failed",
                              public_message="暂时无法读取更新状态。") from exc

    def prepare_update_install(self) -> InstallHandoffResult:
        """Worker-only preparation; 0.2.0 supports never-connected idle sessions only.

        This read-only preflight is advisory, not a replacement for final owner
        evidence. It neither shuts down resources nor starts an installer.
        """
        from .source_bundle_builder import SourceBundleBuilder
        from .analysis_service import AnalysisApplicationService
        used = bool(self._built_sources) or any(value is not None for value in (
            *self._services.values(), self._qq_connection_service, self._qq_setup_service,
            self._qq_connection_manager, self._qq_auth_bridge,
            self._wechat_connection_service_value, self._wechat_setup_service_value))
        if (used or self._sources_closing.is_set()
                or any(type(builder) is not SourceBundleBuilder or not builder.is_unused
                       for builder in self._source_builders.values())
                or (self._analysis_service is not None and
                    (type(self._analysis_service) is not AnalysisApplicationService or not self._analysis_service.is_idle))):
            raise FacadeError(code='update_idle_required', public_message=
                '当前仅支持未连接且没有分析任务的空闲安装。请先完成任务、断开连接并正常退出，再重新打开 Echo 安装更新。')
        try:
            return self._installable_update_service.prepare_install()
        except Exception as exc:
            raise FacadeError(code='update_prepare_failed', public_message='更新包复验失败，请重新检查并下载。') from exc

    def handoff_update_install(self, token: str, result: ShutdownResult) -> InstallHandoffResult:
        """Existing desktop exit owner calls this AFTER its final protocol result.

        Final GUI evidence must contain the exact cached resource-owner result;
        caller-supplied SUCCESS values cannot stand in for a real shutdown.
        """
        if (not isinstance(result, ShutdownResult) or not result.can_apply_update
                or self._shutdown_result is None
                or tuple(step for step in result.steps if step.name != 'gui_workers')
                   != self._shutdown_result.steps):
            return InstallHandoffResult('denied', public_message='安全退出证据不完整，更新未安装。')
        return self._installable_update_service.handoff(token, result)

    @property
    def _wechat_connection_service(self) -> Any:
        if self._wechat_connection_service_value is None:
            bundle = self._source_bundle(ChatSource.WECHAT)
            self._wechat_connection_service_value = getattr(
                bundle,
                "connection",
                None,
            )
        return self._wechat_connection_service_value

    @property
    def _wechat_setup_service(self) -> Any:
        if self._wechat_setup_service_value is None:
            bundle = self._source_bundle(ChatSource.WECHAT)
            self._wechat_setup_service_value = getattr(bundle, "setup", None)
        return self._wechat_setup_service_value

    # ------------------------------------------------------------- discovery

    def list_sessions(self, source: ChatSource) -> list[SessionInfo]:
        """Return the conversations one source offers, normalised."""
        chat_source = _coerce_source(source)
        service = self._require_service(chat_source)
        with _translated_errors(chat_source):
            if chat_source is ChatSource.QQ:
                raw_sessions = service.list_sessions()
            else:
                raw_sessions = service.list_sessions()

        with qq_session_stage(_LOGGER, "facade_convert") if chat_source is ChatSource.QQ else nullcontext():
            return [
                _to_session_info(chat_source, raw_session)
                for raw_session in raw_sessions or ()
            ]


    def get_connection_status(
        self,
        source: ChatSource,
    ) -> QQConnectionStatus | WeChatConnectionStatus:
        """Return the user-facing connection state for one source."""
        chat_source = _coerce_source(source)
        service = self._require_connection_service(chat_source)
        with _translated_errors(chat_source):
            return service.check_status()

    def set_qq_install_path(self, path: str | Path) -> Any:
        """Persist a user-selected QQ.exe path as the first discovery source.

        The GUI uses this only after automatic discovery failed; the next
        connect attempt will prefer the saved path.
        """
        candidate = Path(path)
        if not candidate.is_file():
            raise FacadeError(
                code="qq_install_path_invalid",
                public_message="请选择有效的 QQ.exe 文件。",
            )
        service = self._require_qq_setup_service()
        with _translated_errors(ChatSource.QQ):
            config = service.get_environment_config()
            if config is None:
                config = QQEnvironmentConfig()
            return service.save_environment(
                replace(config, qq_install_path=candidate, qq_install_path_explicit=True)
            )

    def get_qq_connection_snapshot(self) -> ConnectionSnapshot:
        """Report the QQ connection lifecycle without starting anything.

        This is what the GUI shows when a user selects QQ. The manager never
        raises, so an unreachable service becomes a snapshot rather than an
        error the page has to interpret.
        """
        return self._require_qq_connection_manager().get_snapshot()

    def start_qq_auth_flow(
        self,
        progress: Callable[[str], None] | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ConnectionSnapshot:
        """Start QQ authorization and return the resulting lifecycle state.

        The GUI calls this instead of a raw connect: the auth bridge starts
        the runtime's own login flow and the caller keeps polling
        :meth:`get_qq_connection_snapshot` until it reports ``CONNECTED``.
        """
        bridge = self._require_qq_auth_bridge()
        if cancel_event is None or not cancel_event.is_set():
            with _translated_errors(ChatSource.QQ):
                service = self._qq_service_if_present()
                prepare = getattr(service, "prepare_reconnect", None)
                connection = self._optional_qq_connection_service()
                reconnect = getattr(connection, "reconnect", None)
                # Only the two in-memory resets share admission's lock. The
                # runtime login flow below deliberately runs outside it.
                with ExitStack() as resets:
                    guard = getattr(service, "reconnect_guard", None)
                    if callable(guard):
                        resets.enter_context(guard())
                    if callable(prepare):
                        prepare()
                    if callable(reconnect):
                        reconnect()
        if cancel_event is None:
            return bridge.start_auth_flow(progress=progress)
        return bridge.start_auth_flow(progress=progress, cancel_event=cancel_event)

    def cancel_qq_auth_flow(self, cancel_event: threading.Event) -> None:
        """Clean the cancelled QQ attempt off the GUI thread, without touching a newer one."""
        self._require_qq_auth_bridge().cancel_auth_flow(cancel_event)

    def get_qq_qrcode_path(self) -> Path | None:
        """Return the auth bridge's fresh QR path without deriving GUI paths."""
        return self._require_qq_auth_bridge().get_qrcode_path()

    def disconnect_qq(self) -> ConnectionSnapshot:
        """Log out the current QQ account and stop LCA-owned runtime sessions.

        The runtime files and stored QQ configuration are preserved; only the
        active login session is stopped so a different account can scan a
        fresh QR code.
        """
        return self._require_qq_auth_bridge().disconnect()

    def disconnect_wechat(self) -> WeChatConnectionStatus | None:
        """Log out the current WeChat account without deleting local data.

        The database key is released from the stored environment and the
        provider cache is dropped; the data root and database files stay
        untouched.
        """
        service = self._require_setup_service()
        with _translated_errors(ChatSource.WECHAT):
            return service.disconnect()

    def get_wechat_setup_status(self) -> WeChatSetupStatus:
        """Report whether the WeChat environment config is usable."""
        from .source_bundle_builder import SourceBundleBuilder
        builder = self._source_builders.get(ChatSource.WECHAT)
        if (self._wechat_setup_service_value is None
                and ChatSource.WECHAT not in self._built_sources
                and type(builder) is SourceBundleBuilder and builder.has_setup_reader):
            with _translated_errors(ChatSource.WECHAT):
                return builder.read_setup_status()
        service = self._require_setup_service()
        with _translated_errors(ChatSource.WECHAT):
            return service.check_setup()

    def detect_wechat_data_root(self) -> Path | None:
        """Best-effort detect the local WeChat data directory.

        The GUI uses this to prefill the setup dialog and to start the
        one-click connect flow. The detected directory is handed back as a
        config value; the GUI never probes the filesystem itself.
        """
        service = self._require_setup_service()
        with _translated_errors(ChatSource.WECHAT):
            return service.detect_wechat_data_root()

    def detect_wechat_data_roots(self) -> list[Path]:
        """Return every valid WeChat data directory detected locally.

        When several accounts or storage locations exist, the caller shows
        the list and lets the user choose; the application layer never picks
        one automatically.
        """
        service = self._require_setup_service()
        with _translated_errors(ChatSource.WECHAT):
            return service.detect_wechat_data_roots()

    def setup_wechat_environment(
        self,
        config: WeChatEnvironmentConfig,
    ) -> WeChatConnectionStatus | None:
        """Save a WeChat environment config and re-check the connection.

        The GUI hands the collected settings here instead of writing the
        config file itself, so persistence and provider refresh stay in
        the application layer.
        """
        service = self._require_setup_service()
        with _translated_errors(ChatSource.WECHAT):
            return service.save_environment(config)

    def verify_wechat_database(self) -> None:
        """Verify the current key can read the selected WeChat database.

        The GUI calls this immediately before loading sessions. A key that
        cannot open a structurally valid ``session.db`` becomes
        ``wechat_database_unreadable`` instead of a generic read failure, so
        the caller can return the user to the data-location flow. No chat
        content is read.
        """
        service = self._require_setup_service()
        with _translated_errors(ChatSource.WECHAT):
            return service.verify_connection()

    def acquire_wechat_db_key(
        self,
        progress: Callable[[WeChatConnectionProgress], None] | None = None,
        *, cancel_event: threading.Event | None = None,
    ) -> str | None:
        """Acquire and persist the WeChat database key for the connect flow.

        Saving the data root deliberately no longer does this, because the
        key can only be captured while WeChat is at a login moment. The
        connect flow calls this second step explicitly. ``progress`` is
        relayed unchanged; only READY_FOR_LOGIN permits a login prompt.
        """
        service = self._require_setup_service()
        _LOGGER.info(
            "[wechat facade] acquire_wechat_db_key progress=%s",
            progress is not None,
        )
        with _translated_errors(ChatSource.WECHAT):
            options = {"cancel_event": cancel_event} if cancel_event is not None else {}
            return service.acquire_db_key(progress=progress, **options)

    def cancel_wechat_connection(self, cancel_event: threading.Event) -> None:
        """Signal the exact attempt, including queued work, without GUI waits.

        The event reaches the key owner through acquire, so cancellation never
        builds a source or accidentally signals a newer connection attempt.
        """
        cancel_event.set()

    def get_session_message_range(
        self,
        source: ChatSource,
        session_id: str,
    ) -> tuple[int, int] | None:
        """Return earliest and latest message timestamps for one session."""
        chat_source = _coerce_source(source)
        service = self._require_service(chat_source)
        try:
            if chat_source is ChatSource.QQ:
                range_method = getattr(service, "get_session_message_range", None)
                if range_method is None:
                    return None
                raw_session = next(
                    (
                        candidate for candidate in (service.list_sessions() or ())
                        if getattr(candidate, "session_id", None) == session_id
                        or getattr(candidate, "group_code", None) == session_id
                    ),
                    None,
                )
                if _first_string(raw_session, "session_type") == "private":
                    return range_method(
                        session_id,
                        chat_type=1,
                        peer_uin=_first_string(raw_session, "peer_uin") or None,
                        session_name=_first_string(
                            raw_session,
                            "display_name",
                        ) or None,
                    )
                return range_method(session_id)
            return service.get_session_message_range(session_id)
        except Exception:
            return None

    def list_report_packages(self) -> ReportPackageListing:
        """List published package summaries and unreadable package issues."""
        try:
            return self._report_package_catalog.list_reports()
        except Exception as exc:
            _LOGGER.exception("Report package catalog could not be read.")
            raise FacadeError(
                code="report_list_failed",
                public_message="无法读取 Echo 本地报告，请稍后重试。",
            ) from exc

    def get_report_package_html_path(self, package_name: str) -> Path:
        """Return a safely located historical report for the GUI opener."""
        try:
            return self._report_package_catalog.resolve_html_path(package_name)
        except Exception as exc:
            _LOGGER.exception("Report package HTML could not be located.")
            raise FacadeError(
                code="report_open_failed",
                public_message="这份 Echo 报告已损坏或缺少报告文件。",
            ) from exc

    def delete_report_packages(self, package_names: Iterable[str]) -> ReportPackageDeletionResult:
        """Delete exactly the selected report packages, reporting each outcome.

        The caller owns the selection: nothing outside it is rescanned or
        removed, so reports published while the batch runs stay on disk.
        Per-package failures come back inside the result instead of aborting the
        batch; only an unusable selection or storage boundary raises.
        """
        try:
            return self._report_package_catalog.delete_packages(package_names)
        except Exception as exc:
            _LOGGER.exception("Selected report packages could not be deleted.")
            raise FacadeError(
                code="report_delete_failed",
                public_message="所选 Echo 报告未能删除，请稍后重试。",
            ) from exc

    # -------------------------------------------------------------- analysis

    def analyze_session(
        self,
        source: ChatSource,
        session_id: str,
        config: AnalysisConfig | None = None,
        progress: Callable[[str], None] | None = None,
        *,
        speaker_names: Mapping[str, str] | None = None,
        viewer_speaker_key: str | None = None,
        on_phase: PhaseCallback | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> AnalysisOutcome:
        """Export one conversation, analyze it, and return a view.

        QQ Direct DB acquisition owns its payload until analysis completes.
        WeChat exports use the facade scratch directory.

        ``progress`` keeps carrying the human-facing Chinese status text;
        ``on_phase`` additionally receives :class:`AnalysisPhase.READING`
        before the source is acquired and
        :class:`AnalysisPhase.ANALYZING_REPORT` once acquisition succeeded.
        """
        chat_source = _coerce_source(source)
        resolved_config = config or AnalysisConfig()
        resolved_scope = self._resolve_scope(resolved_config, chat_source)
        _report_progress(progress, "正在准备分析...")
        _publish_phase(on_phase, AnalysisPhase.READING)
        _report_progress(progress, "正在读取聊天记录...")
        service = self._require_service(chat_source)
        if chat_source is ChatSource.QQ:
            # Direct DB: one acquisition owns both the payload and the session
            # descriptor. Listing sessions here would acquire a second
            # generation, so the descriptor is resolved from the acquisition.
            return self._analyze_direct_db_qq_session(
                service,
                session_id,
                resolved_config,
                resolved_scope,
                speaker_names=speaker_names,
                viewer_speaker_key=viewer_speaker_key,
                progress=progress,
                on_phase=on_phase,
                cancelled=cancelled,
            )
        raw_session = next(
            (
                candidate for candidate in (service.list_sessions() or ())
                if getattr(candidate, "session_id", None) == session_id
                or getattr(candidate, "group_code", None) == session_id
            ),
            None,
        )
        session = (
            _to_session_info(chat_source, raw_session)
            if raw_session is not None
            else SessionInfo(
                source=chat_source,
                session_id=session_id,
                display_name=session_id,
            )
        )
        conversation_names = {session_id: session.display_name}
        conversation_kind = (
            session.session_type
            if session.session_type in ("private", "group")
            else "unknown"
        )

        with TemporaryDirectory(prefix="chat-analyzer-export-") as scratch:
            scratch_directory = Path(scratch)
            with self._session_export_context(
                chat_source,
                service,
                session_id,
                scratch_directory,
                scope=resolved_scope,
                on_phase=on_phase,
            ) as session_export:
                return self._analyze_path(
                    session_export.payload_path,
                    resolved_config,
                    source_media_roots=session_export.sticker_media_roots,
                    source=chat_source,
                    session=session,
                    scope=resolved_scope,
                    speaker_names=speaker_names,
                    conversation_names=conversation_names,
                    conversation_kind=conversation_kind,
                    viewer_speaker_key=viewer_speaker_key,
                    progress=progress,
                    cancelled=cancelled,
                )

    def _analyze_direct_db_qq_session(
        self,
        service: Any,
        session_id: str,
        config: AnalysisConfig,
        scope: AnalysisScope,
        *,
        speaker_names: Mapping[str, str] | None = None,
        viewer_speaker_key: str | None = None,
        progress: Callable[[str], None] | None = None,
        on_phase: PhaseCallback | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> AnalysisOutcome:
        """Run one Direct DB QQ analysis from a single generation acquisition.

        The session descriptor (display name, type) comes from the same
        acquisition that materialized the payload, so this path never calls
        the public ``list_sessions`` before analyzing.
        """
        started_at = _perf_counter()
        with TemporaryDirectory(prefix="chat-analyzer-export-") as scratch:
            scratch_directory = Path(scratch)
            with self._session_export_context(
                ChatSource.QQ,
                service,
                session_id,
                scratch_directory,
                scope=scope,
                on_phase=on_phase,
            ) as session_export:
                acquired_at = _perf_counter()
                _LOGGER.info(
                    "[analysis-timing] stage=facade_acquisition elapsed_ms=%d",
                    max(0, round((acquired_at - started_at) * 1000)),
                )
                session = session_export.session or SessionInfo(
                    source=ChatSource.QQ,
                    session_id=session_id,
                    display_name=session_id,
                )
                conversation_names = {
                    session_export.conversation_id or session_id: session.display_name
                }
                conversation_kind = (
                    session.session_type
                    if session.session_type in ("private", "group")
                    else "unknown"
                )
                analysis_started_at = _perf_counter()
                completed = False
                try:
                    result = self._analyze_path(
                        session_export.payload_path,
                        config,
                        source=ChatSource.QQ,
                        session=session,
                        scope=scope,
                        speaker_names=speaker_names,
                        conversation_names=conversation_names,
                        conversation_kind=conversation_kind,
                        viewer_speaker_key=viewer_speaker_key,
                        progress=progress,
                        direct_db_diagnostics=True,
                        cancelled=cancelled,
                        source_media_roots=session_export.sticker_media_roots,
                    )
                    completed = True
                    return result
                finally:
                    _LOGGER.info(
                        "[analysis-timing] stage=facade_analysis elapsed_ms=%d status=%s",
                        max(0, round((_perf_counter() - analysis_started_at) * 1000)),
                        "ok" if completed else "failed",
                    )


    # ------------------------------------------------------------- internals

    def _analyze_path(
        self,
        input_path: Path,
        config: AnalysisConfig,
        *,
        source: ChatSource,
        session: SessionInfo | None,
        scope: AnalysisScope,
        speaker_names: Mapping[str, str] | None = None,
        conversation_names: Mapping[str, str] | None = None,
        conversation_kind: str = "unknown",
        viewer_speaker_key: str | None = None,
        progress: Callable[[str], None] | None = None,
        direct_db_diagnostics: bool = False,
        source_media_roots: tuple[Path, ...] = (),
        cancelled: Callable[[], bool] = lambda: False,
    ) -> AnalysisOutcome:
        """Run analysis then presentation for one local path."""
        analysis_service = self._require_analysis_service()

        if config.output_directory is None and not self._scratch_recovery_done:
            _cleanup_stale_analysis_outputs(user_data_dir() / "transient")
            self._scratch_recovery_done = True
        output_directory, temporary_output = _create_output_directory(config)
        prepared_qq_media = None
        try:
            if cancelled():
                raise FacadeError(code="analysis_cancelled", public_message="分析已取消。")
            automatic_qq = source is ChatSource.QQ and session is not None
            automatic_wechat = source is ChatSource.WECHAT and session is not None
            media_roots = source_media_roots if config.sticker_media_roots is None else config.sticker_media_roots
            request = AnalysisRequestDTO(
                input_path=input_path,
                output_directory=output_directory,
                stopwords_path=self._stopwords_path(config.profile),
                font_path=config.font_path,
                top=config.top,
                scope=scope,
                speaker_names=speaker_names or {},
                conversation_names=conversation_names or {},
                conversation_kind=conversation_kind,
                viewer_speaker_key=viewer_speaker_key,
                sticker_media_roots=() if automatic_qq else media_roots,
                export_echo_report=not (automatic_qq or automatic_wechat),
            )
            with _translated_errors(source):
                _report_progress(progress, "正在处理消息...")
                _report_progress(progress, "正在分析聊天内容...")
                if automatic_qq or automatic_wechat:
                    _report_progress(progress, "正在分析表情包...")
                core_started_at = _perf_counter()
                core_completed = False
                try:
                    result = analysis_service.execute(request)
                    core_completed = True
                finally:
                    if direct_db_diagnostics:
                        _LOGGER.info(
                            "[analysis-timing] stage=facade_core_analysis "
                            "elapsed_ms=%d status=%s",
                            max(0, round((_perf_counter() - core_started_at) * 1000)),
                            "ok" if core_completed else "failed",
                        )

            if cancelled() or self._sources_closing.is_set():
                raise FacadeError(code="analysis_cancelled", public_message="分析已取消。")
            if automatic_qq and result.echo_report_view is not None:
                media_batch = uuid4().hex[:12]
                stickers = self._prepare_qq_report_media(
                    result, input_path, session, media_roots, progress, cancelled, batch=media_batch)
                prepared_qq_media = stickers
                _report_progress(progress, "正在生成报告...")
                if cancelled():
                    raise FacadeError(code="analysis_cancelled", public_message="分析已取消。")
                _LOGGER.info("[sticker-media-export] batch=%s state=start main_thread=%s", media_batch,
                             threading.current_thread() is threading.main_thread())
                try:
                    with _translated_errors(source), timed_stage("sticker_report_export"):
                        result = analysis_service.export_sticker_report(result, stickers, output_directory,
                            cancelled=lambda: cancelled() or self._sources_closing.is_set())
                except Exception:
                    if cancelled() or self._sources_closing.is_set():
                        raise FacadeError(code="analysis_cancelled", public_message="分析已取消。") from None
                    _LOGGER.info("[sticker-media-export] batch=%s state=failed code=report_assembly_failed", media_batch)
                    raise
                chapter = result.echo_report_view.stickers
                for ordinal, asset in enumerate(chapter.assets if chapter is not None else (), 1):
                    assembly = ("external_file" if asset.content is not None else
                                "embedded" if asset.data_uri else
                                "report_asset_budget" if asset.reason == "report_asset_budget" else
                                "report_image_unavailable" if asset.reason == "report_image_unavailable" else "upstream_unavailable")
                    _LOGGER.info("[sticker-media-asset] batch=%s target=%d code=%s", media_batch, ordinal, assembly)
                _LOGGER.info("[sticker-media-export] batch=%s state=finished", media_batch)
            elif automatic_wechat and result.echo_report_view is not None:
                media_batch = uuid4().hex[:12]
                stickers = self._prepare_wechat_report_media(
                    result, progress, cancelled, batch=media_batch)
                if cancelled() or self._sources_closing.is_set():
                    raise FacadeError(code="analysis_cancelled", public_message="分析已取消。")
                _report_progress(progress, "正在生成报告...")
                try:
                    with _translated_errors(source), timed_stage("sticker_report_export"):
                        result = analysis_service.export_sticker_report(
                            result, stickers, output_directory,
                            cancelled=lambda: cancelled() or self._sources_closing.is_set())
                except Exception:
                    if cancelled() or self._sources_closing.is_set():
                        raise FacadeError(code="analysis_cancelled", public_message="分析已取消。") from None
                    _LOGGER.info("[sticker-media-export] batch=%s state=failed code=report_assembly_failed",
                                 media_batch)
                    raise
            else:
                _report_progress(progress, "正在生成报告...")
            if cancelled() or self._sources_closing.is_set():
                raise FacadeError(code="analysis_cancelled", public_message="分析已取消。")
            report_generated_at = datetime.now(timezone.utc)
            report_path = _generated_echo_report_path(result, output_directory)
            echo_report_view = getattr(result, "echo_report_view", None)
            report_directory = None
            if report_path is not None:
                try:
                    metadata = build_report_metadata(
                        result=result,
                        source=source.value,
                        scope=scope,
                        generated_at=report_generated_at,
                    )
                    with timed_stage("report_package"):
                        report_directory = package_echo_report(
                            output_directory,
                            reports_root=self._report_package_catalog.reports_root,
                            metadata=metadata,
                            now=report_generated_at.astimezone(),
                            cancelled=lambda: cancelled() or self._sources_closing.is_set(),
                        )
                    report_path = report_directory / ECHO_REPORT_HTML_NAME
                except Exception:
                    _LOGGER.warning(
                        "Analysis completed but the Echo report could not be "
                        "packaged; keeping the generated report in place.",
                        exc_info=True,
                    )
            if cancelled() or self._sources_closing.is_set():
                raise FacadeError(code="analysis_cancelled", public_message="分析已取消。")
            _LOGGER.info(
                "[facade] analysis outcome ready report_path=%s "
                "report_directory=%s echo_view=%s",
                report_path,
                report_directory,
                echo_report_view is not None,
            )
            if temporary_output is not None and report_path is None:
                temporary_output.cleanup()
                temporary_output = None

            outcome = AnalysisOutcome(
                result=result,
                source=source,
                session=session,
                artifact_directory=(
                    output_directory if report_path is not None else None
                ),
                report_path=report_path,
                report_directory=report_directory,
                echo_report_view=echo_report_view,
                report_generated_at=report_generated_at,
                analysis_scope=scope,
            )
            _report_progress(progress, "分析完成")
            self._replace_retained_output(temporary_output)
        except BaseException:
            if prepared_qq_media is not None:
                for media in prepared_qq_media.media:
                    if media.stored is not None:
                        media.stored.close()
            if temporary_output is not None:
                temporary_output.cleanup()
            raise
        return outcome

    def _replace_retained_output(
        self,
        temporary_output: _RetainedReportDirectory | None,
    ) -> None:
        """Keep only the latest successful scratch report alive."""
        previous = self._retained_output_directory
        self._retained_output_directory = temporary_output
        if previous is not None and previous is not temporary_output:
            previous.cleanup()

    def shutdown(self) -> ShutdownResult:
        """Return the same conservative resource evidence on repeated shutdown."""
        with self._shutdown_lock:
            if self._shutdown_result is None:
                self._shutdown_result = self._shutdown_with_result()
            return self._shutdown_result

    def _shutdown_with_result(self) -> ShutdownResult:
        """Release transient artifacts and stop LCA-owned QQ runtime processes.

        Order is deliberate: attempt Direct DB plaintext cleanup/recovery
        before stopping NapCat / the QQ runtime. Failure or timeout remains
        explicit in the result; returning alone does not prove plaintext was
        removed. Every step is bounded and exception-safe, so neither a failure
        nor a hung cleanup in one step can skip the steps after it. A live
        owned Direct DB task keeps its runtime until quiescence is proved.
        Repeated calls are harmless: the
        Direct DB service refuses a second shutdown, and terminated PIDs are
        forgotten by the process registry.
        """
        started_at = _monotonic()
        with self._source_publication_lock:
            self._sources_closing.set()
        source_result = self._run_shutdown_step(
            'source_lifecycle', self._shutdown_source_lifecycle,
            window=self._source_lifecycle_step_window(),
        )
        _LOGGER.info("QQ shutdown requested")
        results = [self._run_shutdown_step(
            _SHUTDOWN_STEP_DIRECT_DB,
            self._shutdown_qq_direct_db_service,
            window=self._direct_db_step_window(),
        ), self._run_shutdown_step(
            _SHUTDOWN_STEP_RETAINED_OUTPUT,
            self._release_retained_output,
        ), self._run_shutdown_step(
            _SHUTDOWN_STEP_QQ_RUNTIME,
            self._shutdown_runtime_and_credential,
        )]
        results.append(source_result)
        _LOGGER.info(
            "QQ shutdown finished elapsed=%.2fs",
            _monotonic() - started_at,
        )
        return ShutdownResult(tuple(results))

    def _shutdown_source_lifecycle(self) -> ShutdownStatus:
        from .source_bundle_builder import SourceBundleBuilder
        from .analysis_service import AnalysisApplicationService

        builder_statuses = [builder.shutdown() if type(builder) is SourceBundleBuilder
                            else ShutdownStatus.UNKNOWN
                            for builder in self._source_builders.values()]
        analysis_status = (ShutdownStatus.SUCCESS if self._analysis_service is None
                           else self._analysis_service.shutdown()
                           if type(self._analysis_service) is AnalysisApplicationService
                           else ShutdownStatus.UNKNOWN)
        # Services currently expose no complete lifecycle/quiescence contract.
        # Even a successful recover RPC cannot prove every helper/read is gone.
        # Unpublished builders can already own resources or be in-flight.
        # Unknown analysis implementations likewise have no ownership proof.
        used_sources = bool(self._built_sources) or any(
            value is not None for value in (
                *self._services.values(), self._qq_connection_service,
                self._qq_setup_service, self._qq_connection_manager,
                self._qq_auth_bridge, self._wechat_connection_service_value,
                self._wechat_setup_service_value,
            )
        )
        setup = self._wechat_setup_service_value
        if setup is None:
            setup = getattr(self._built_sources.get(ChatSource.WECHAT), 'setup', None)
        if setup is not None:
            shutdown = getattr(setup, 'shutdown', None)
            if callable(shutdown):
                status = shutdown()
                if status in (ShutdownStatus.FAILED, ShutdownStatus.TIMED_OUT):
                    return status
        return (ShutdownStatus.UNKNOWN if used_sources
                or any(status is not ShutdownStatus.SUCCESS for status in (*builder_statuses, analysis_status))
                else ShutdownStatus.SUCCESS)

    def _source_lifecycle_step_window(self) -> float:
        # Key-owner drain is 3s; include a 1s scheduling margin, while keeping
        # the existing desktop exit window large enough for QQ's later steps.
        return min(self._shutdown_step_seconds, 4.0)

    def _shutdown_runtime_and_credential(self) -> ShutdownStatus:
        from .qq.qq_process_registry import QQProcessRegistry
        from .qq.qq_direct_database_import_service import QQDirectDatabaseImportService

        service = self._qq_service_if_present()
        if type(service) is QQDirectDatabaseImportService and not service.runtime_stop_safe:
            _LOGGER.warning("QQ runtime stop deferred reason=direct_db_not_quiescent")
            return ShutdownStatus.TIMED_OUT

        registry = self._require_qq_process_registry()
        if type(registry) is QQProcessRegistry:
            try:
                runtime_status = registry.shutdown()
            finally:
                credential_status = self._retire_qq_bridge_credential()
            return ShutdownResult((ShutdownStepResult('runtime', runtime_status),
                                   ShutdownStepResult('credential', credential_status))).status
        recorded = getattr(registry, 'recorded', None)
        try:
            registry.terminate_all()
        finally:
            credential_status = self._retire_qq_bridge_credential()
        if not callable(recorded):
            return (ShutdownStatus.FAILED if credential_status is ShutdownStatus.FAILED
                    else ShutdownStatus.UNKNOWN)
        if recorded():
            return ShutdownStatus.FAILED
        if credential_status is not ShutdownStatus.SUCCESS:
            return credential_status
        # A duck-typed empty inventory is not a proven ownership contract.
        return ShutdownStatus.UNKNOWN

    def _retire_qq_bridge_credential(self) -> ShutdownStatus:
        """Drop the credential of the QQ runtime Echo just stopped.

        Deliberately last: the plaintext recovery step above still needs it, and
        once the runtime is gone a value minted for it must not be able to reach
        whatever might answer on the port later.
        """
        session = self._qq_runtime_session
        from .qq.qq_runtime_session import QQRuntimeSession
        if type(session) is QQRuntimeSession:
            return session.shutdown()
        if session is None:
            from .qq.qq_runtime_session import default_qq_runtime_session
            session = default_qq_runtime_session()
        retire = getattr(session, "retire", None)
        credential = getattr(session, "credential", None)
        if not callable(retire):
            return ShutdownStatus.UNKNOWN
        credential_before = credential() if callable(credential) else None
        retire()
        if not callable(credential):
            return ShutdownStatus.UNKNOWN
        if credential() is not None:
            return ShutdownStatus.FAILED
        # Retirement clears the session's process reference, without waiting
        # for its exit. An injected owner has no complete lifecycle contract.
        if self._qq_runtime_session is not None or credential_before is not None:
            return ShutdownStatus.UNKNOWN
        return ShutdownStatus.SUCCESS

    def _direct_db_step_window(self) -> float:
        """Return the bounded window the Direct DB cleanup step gets.

        The service knows its own bounded worst case - the drain window plus the
        recover readiness budget - and this step window is that budget plus a
        margin.  A normal bounded cleanup therefore finishes and reports,
        instead of being abandoned and then killed by the process exit while it
        is still recovering orphan plaintext.
        """
        service = self._qq_service_if_present()
        budget = getattr(service, "shutdown_budget_seconds", None)
        if (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or budget <= 0
        ):
            return self._shutdown_step_seconds
        return max(
            self._shutdown_step_seconds,
            float(budget) + _DIRECT_DB_STEP_MARGIN_SECONDS,
        )

    def _release_retained_output(self) -> ShutdownStatus:
        """Drop the last retained scratch report directory, if any."""
        previous = self._retained_output_directory
        self._replace_retained_output(None)
        if previous is None:
            return ShutdownStatus.SUCCESS
        # cleanup() logs and swallows filesystem failures; verify its effect.
        try:
            Path(previous.name).lstat()
        except FileNotFoundError:
            return ShutdownStatus.SUCCESS
        return ShutdownStatus.FAILED

    def _run_shutdown_step(
        self,
        label: str,
        step: Callable[[], Any],
        *,
        window: float | None = None,
    ) -> ShutdownStepResult:
        """Run one shutdown step inside its own bounded window.

        The step runs on a throwaway daemon thread so a cleanup that never
        returns - a wedged runtime RPC, a stuck process kill - is abandoned
        instead of holding the whole protocol, and with it the desktop process
        exit, hostage.  The window is finite by design, and start, outcome and
        elapsed time are logged so a real run is diagnosable.
        """
        budget = self._shutdown_step_seconds if window is None else window
        finished = threading.Event()
        started_at = _monotonic()
        outcome = ShutdownStatus.UNKNOWN

        def _run() -> None:
            nonlocal outcome
            try:
                returned = step()
                if isinstance(returned, ShutdownStatus):
                    outcome = returned
            except BaseException:
                outcome = ShutdownStatus.FAILED
                _LOGGER.warning(
                    "QQ shutdown step failed step=%s",
                    label,
                    exc_info=True,
                )
            finally:
                finished.set()

        thread = threading.Thread(
            target=_run,
            name=f"{_SHUTDOWN_STEP_THREAD_PREFIX}{label}",
            daemon=True,
        )
        _LOGGER.info(
            "QQ shutdown step started step=%s window=%.2fs",
            label,
            budget,
        )
        thread.start()
        if not finished.wait(budget):
            _LOGGER.warning(
                "QQ shutdown step exceeded %.1fs and was abandoned step=%s",
                budget,
                label,
            )
            return ShutdownStepResult(label, ShutdownStatus.TIMED_OUT)
        _LOGGER.info(
            "QQ shutdown step finished step=%s elapsed=%.2fs",
            label,
            _monotonic() - started_at,
        )
        return ShutdownStepResult(label, outcome)

    def _shutdown_qq_direct_db_service(self) -> ShutdownStatus:
        """Recover plaintext and consume only owned, explicit cleanup evidence."""
        service = self._qq_service_if_present()
        if service is None:
            return ShutdownStatus.SUCCESS
        shutdown = getattr(service, "shutdown", None)
        if callable(shutdown):
            try:
                status = shutdown()
                from .qq.qq_direct_database_import_service import QQDirectDatabaseImportService
                # Only the owned implementation proves acquisition admission,
                # drain and recover. A duck-typed return is not ownership proof.
                if (type(service) is QQDirectDatabaseImportService
                        and isinstance(status, ShutdownStatus)):
                    return status
            except Exception:
                _LOGGER.warning(
                    "QQ Direct DB plaintext cleanup did not complete during "
                    "shutdown."
                )
                raise
        return ShutdownStatus.UNKNOWN

    def _qq_service_if_present(self) -> Any:
        """Return the QQ service without building it for the first time.

        A never-built QQ service has never acquired a generation, so there is
        nothing to clean up; forcing its construction here would be a side
        effect the shutdown path must not have.
        """
        service = self._services.get(ChatSource.QQ)
        if service is not None:
            return service
        bundle = self._built_sources.get(ChatSource.QQ)
        if bundle is not None:
            return getattr(bundle, "service", None)
        return None

    @contextmanager
    def _session_export_context(
        self,
        source: ChatSource,
        service: Any,
        session_id: str,
        scratch_directory: Path,
        *,
        scope: AnalysisScope | None = None,
        on_phase: PhaseCallback | None = None,
    ) -> Iterator[_SessionExport]:
        """Yield an export while preserving source-specific ownership.

        Acquisition receives inclusive epoch-second scope bounds. The final
        scope filter re-checks every imported message. QQ payload ownership
        spans the consumer, and acquisition failures become FacadeError.

        ``ANALYZING_REPORT`` is published here, not by the caller, because only
        this context knows that the source acquisition really succeeded: for QQ
        while the acquisition context is still open, for WeChat once
        ``export_only`` has returned a payload. A failed acquisition therefore
        never publishes it.
        """
        start_seconds, end_seconds = _scope_export_window_seconds(scope)
        if source is ChatSource.QQ:
            with ExitStack() as ownership:
                with _translated_errors(source):
                    acquisition = ownership.enter_context(
                        service.acquired_session(
                            session_id,
                            start_time=start_seconds,
                            end_time=end_seconds,
                        )
                    )
                _publish_phase(on_phase, AnalysisPhase.ANALYZING_REPORT)
                raw_session = getattr(acquisition, "session", None)
                yield _SessionExport(
                    payload_path=Path(acquisition.payload_path),
                    conversation_id=getattr(raw_session, "conversation_id", None),
                    session=(
                        _to_session_info(source, raw_session)
                        if raw_session is not None
                        else None
                    ),
                )
            return

        with _translated_errors(source):
            payload_path = Path(
                service.export_only(
                    WeChatExportImportRequest(
                        session_id=session_id,
                        output_path=(
                            scratch_directory / "wechat_export.json"
                        ),
                        start_time=start_seconds,
                        end_time=end_seconds,
                    )
                )
            )
        # The phase callback belongs to the caller, not to the source, so a
        # failure in it stays a caller failure instead of becoming a source
        # error. No source resource is held at this point.
        _publish_phase(on_phase, AnalysisPhase.ANALYZING_REPORT)
        with _translated_errors(source):
            accessor = getattr(service, "sticker_media_roots", None)
            yield _SessionExport(
                payload_path=payload_path,
                sticker_media_roots=tuple(accessor()) if accessor is not None else (),
            )

    @staticmethod
    def _resolve_scope(
        config: AnalysisConfig,
        source: ChatSource,
    ) -> AnalysisScope:
        with _translated_errors(source):
            return resolve_scope(
                config.scope_mode,
                start_time=config.start_time,
                end_time=config.end_time,
            )

    def _stopwords_path(self, profile: str) -> Path:
        filename = _PROFILE_STOPWORD_FILES.get(
            profile,
            _PROFILE_STOPWORD_FILES[DEFAULT_PROFILE],
        )
        return self._stopwords_directory / filename

    def _require_service(self, source: ChatSource) -> Any:
        service = self._services.get(source)
        if service is None:
            bundle = self._source_bundle(source)
            service = getattr(bundle, "service", None)
        if service is None:
            raise SourceUnavailable(source)
        return service

    def _require_connection_service(self, source: ChatSource) -> Any:
        if source is ChatSource.QQ:
            service = self._qq_connection_service
        elif source is ChatSource.WECHAT:
            service = self._wechat_connection_service
        else:
            raise UnknownChatSource(source)
        if service is None:
            bundle = self._source_bundle(source)
            service = getattr(bundle, "connection", None)
        if service is None:
            raise SourceUnavailable(source)
        return service

    def _require_qq_connection_manager(self) -> Any:
        """Return the QQ connection manager, building it on first use.

        The manager is composed from the services this facade already holds,
        so a caller that injected stubs keeps getting those stubs.
        """
        if self._qq_connection_manager is None:
            self._qq_connection_manager = QQConnectionManager(
                setup_service=self._optional_qq_setup_service(),
                connection_service=self._optional_qq_connection_service(),
            )
        return self._qq_connection_manager

    def _require_qq_auth_bridge(self) -> Any:
        """Return the QQ auth bridge, composing it from injected services."""
        if self._qq_auth_bridge is None:
            from .qq.qq_auth_bridge import QQAuthBridge
            from .qq.qq_auth_bridge import (
                terminate_bundled_runtime_sessions,
            )

            self._qq_auth_bridge = QQAuthBridge(
                setup_service=self._optional_qq_setup_service(),
                connection_service=self._optional_qq_connection_service(),
                manager=self._require_qq_connection_manager(),
                process_registry=self._require_qq_process_registry(),
                credential=self._qq_runtime_session,
                # A new auth session must not keep sharing the QR cache with
                # a runtime left over from an earlier Echo instance.
                runtime_cleaner=terminate_bundled_runtime_sessions,
            )
        return self._qq_auth_bridge

    def _require_qq_process_registry(self) -> Any:
        """Return the shared QQ process registry for this application."""
        if self._qq_process_registry is None:
            from .qq.qq_process_registry import (
                default_qq_process_registry,
            )

            self._qq_process_registry = default_qq_process_registry()
        return self._qq_process_registry

    def _optional_qq_setup_service(self) -> Any:
        try:
            return self._require_qq_setup_service()
        except SourceUnavailable:
            return None

    def _optional_qq_connection_service(self) -> Any:
        try:
            return self._require_connection_service(ChatSource.QQ)
        except (SourceUnavailable, UnknownChatSource):
            return None

    def _require_qq_setup_service(self) -> Any:
        service = self._qq_setup_service
        if service is None:
            bundle = self._source_bundle(ChatSource.QQ)
            service = getattr(bundle, "setup", None)
        if service is None:
            raise SourceUnavailable(ChatSource.QQ)
        return service

    def _require_setup_service(self) -> Any:
        if self._sources_closing.is_set():
            raise FacadeError(code="application_closing", public_message="Echo 正在关闭。")
        service = self._wechat_setup_service
        if self._sources_closing.is_set():
            raise FacadeError(code="application_closing", public_message="Echo 正在关闭。")
        if service is None:
            bundle = self._source_bundle(ChatSource.WECHAT)
            service = getattr(bundle, "setup", None)
        if service is None:
            raise SourceUnavailable(ChatSource.WECHAT)
        return service

    def _source_bundle(self, source: ChatSource) -> Any:
        """Build and cache one source's services on first access."""
        if source not in self._built_sources:
            builder = self._source_builders.get(source)
            if builder is None:
                raise SourceUnavailable(source)
            if source is ChatSource.WECHAT and self._sources_closing.is_set():
                raise FacadeError(code="application_closing", public_message="Echo 正在关闭。")
            bundle = builder()
            if source is ChatSource.WECHAT:
                # Never hold this short publication lock while building/draining.
                # Close takes the same boundary before its ownership snapshot.
                with self._source_publication_lock:
                    closing = self._sources_closing.is_set()
                    if not closing:
                        self._built_sources[source] = bundle
                if closing:
                    shutdown = getattr(getattr(bundle, "setup", None), "shutdown", None)
                    if callable(shutdown):
                        shutdown()
                    raise FacadeError(code="application_closing", public_message="Echo 正在关闭。")
            else:
                self._built_sources[source] = bundle
        return self._built_sources[source]

    def _require_analysis_service(self) -> Any:
        if self._analysis_service is None:
            raise FacadeError(
                code="analysis_service_unavailable",
                public_message="分析服务不可用。",
            )
        return self._analysis_service


# ------------------------------------------------------------------ helpers


def _create_output_directory(
    config: AnalysisConfig,
) -> tuple[Path, _RetainedReportDirectory | None]:
    """Create an artifact directory and transfer scratch ownership upward."""
    if config.output_directory is not None:
        directory = Path(config.output_directory)
        directory.mkdir(parents=True, exist_ok=True)
        return directory, None

    transient_directory = user_data_dir() / "transient"
    _require_no_reparse_points(transient_directory)
    transient_directory.mkdir(parents=True, exist_ok=True)
    scratch = _RetainedReportDirectory(transient_directory)
    return Path(scratch.name), scratch


def _generated_echo_report_path(
    result: AnalysisResultDTO,
    output_directory: Path,
) -> Path | None:
    """Resolve the report descriptor produced by this exact analysis run."""
    for artifact in result.artifacts:
        if artifact.kind != "echo_report_html":
            continue
        candidate = (output_directory / artifact.filename).resolve()
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            return None
    return None


@contextmanager
def _translated_errors(source: ChatSource | None) -> Iterator[None]:
    """Convert every known collaborator failure into a :class:`FacadeError`."""
    try:
        yield
    except FacadeError:
        raise
    except ApplicationServiceError as error:
        raise FacadeError(
            code=getattr(error, "code", "application_error"),
            public_message=_provider_error_message(error),
            source=source,
        ) from error
    except Exception as error:
        raise FacadeError(
            code=_provider_error_code(error),
            public_message=_provider_error_message(error),
            source=source,
        ) from error


def _provider_error_code(error: Exception) -> str:
    """Prefer a provider's own stable code, else derive one from its class."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return code
    return _snake_case(type(error).__name__)


def _provider_error_message(error: Exception) -> str:
    """Trust explicit public messages; never expose raw exception text."""
    message = getattr(error, "public_message", None)
    if isinstance(message, str) and message.strip():
        return message
    return "操作失败，请稍后重试。"


def _snake_case(name: str) -> str:
    characters: list[str] = []
    for index, character in enumerate(name):
        if character.isupper() and index > 0:
            characters.append("_")
        characters.append(character.lower())
    return "".join(characters)


def _scope_export_window_seconds(
    scope: AnalysisScope | None,
) -> tuple[int | None, int | None]:
    """Translate an inclusive calendar scope into QQ/WeChat epoch-second bounds.

    ``(None, None)`` means "no filter", which keeps the full-history export
    path unchanged for the ALL scope. The end bound is the last second
    (23:59:59) of the inclusive end date, matching WeChat second-precision
    m.create_time column.

    """
    if scope is None or scope.start_date is None or scope.end_date is None:
        return None, None
    try:
        start_seconds = _local_midnight_epoch_seconds(scope.start_date)
        end_exclusive_seconds = _local_midnight_epoch_seconds(
            scope.end_date + timedelta(days=1)
        )
    except (OverflowError, OSError, ValueError):
        return None, None
    end_inclusive_seconds = end_exclusive_seconds - 1
    return (start_seconds, end_inclusive_seconds)


def _local_midnight_epoch_seconds(value: date) -> int:

    """Return the local-time midnight of one calendar date in epoch seconds."""
    return int(datetime.combine(value, time.min).timestamp())


def _coerce_source(source: Any) -> ChatSource:
    """Accept a :class:`ChatSource` or its string value."""
    if isinstance(source, ChatSource):
        return source
    try:
        return ChatSource(source)
    except (ValueError, TypeError):
        raise UnknownChatSource(source) from None


def _to_session_info(source: ChatSource, raw_session: Any) -> SessionInfo:
    """Normalise one provider session or group into :class:`SessionInfo`.

    QQ groups expose ``group_code``/``group_name``/``message_count`` while
    WeChat sessions expose ``session_id``/``display_name``/``message_count``.
    Both collapse into the same shape here so a caller never branches on the
    source when rendering a list.
    """
    if source is ChatSource.QQ:
        session_id = _first_string(raw_session, "group_code", "session_id")
        display_name = _first_string(
            raw_session,
            "group_name",
            "display_name",
        )
        session_type = _first_string(raw_session, "session_type") or "group"
    else:
        session_id = _first_string(raw_session, "session_id", "group_code")
        display_name = _first_string(
            raw_session,
            "display_name",
            "group_name",
        )
        session_type = _first_string(raw_session, "session_type") or "other"

    return SessionInfo(
        source=source,
        session_id=session_id,
        display_name=(
            display_name
            or ("\u672a\u77e5\u7fa4\u804a" if source is ChatSource.QQ else session_id)
        ),
        session_type=session_type,
        message_count=_first_int(raw_session, "message_count"),
        last_message_time=_first_epoch(
            raw_session,
            "last_message_time",
            "last_timestamp",
        ),
        message_available=bool(
            getattr(raw_session, "message_available", True)
        ),
        unavailable_reason=getattr(raw_session, "unavailable_reason", None),
        search_names=tuple(getattr(raw_session, "search_names", ())),
    )


def _first_string(raw_session: Any, *names: str) -> str:
    for name in names:
        value = getattr(raw_session, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_int(raw_session: Any, *names: str) -> int | None:
    for name in names:
        value = getattr(raw_session, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _first_epoch(raw_session: Any, *names: str) -> int | None:
    for name in names:
        value = getattr(raw_session, name, None)
        if value is None:
            continue
        epoch = to_epoch_seconds(value)
        if epoch is not None:
            return epoch
    return None
