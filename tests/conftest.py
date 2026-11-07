"""Keep default user-data paths inside each automated test's sandbox."""

from pathlib import Path
import sys

import pytest


@pytest.fixture(autouse=True)
def release_test_widgets():
    """Delete this test's Qt roots while QApplication is still alive."""
    # Do not import or initialize Qt for tests that do not use it.
    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is None:
        yield
        return

    application_type = widgets.QApplication
    if not isinstance(application_type, type):
        yield
        return

    app = application_type.instance()
    existing = set(app.allWidgets()) if isinstance(app, application_type) else set()
    yield

    app = application_type.instance()
    if not isinstance(app, application_type):
        return

    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    # Higher-scope fixtures have already been set up; preserve their widgets,
    # including pre-existing children that a test may have reparented.
    roots = [widget for widget in app.allWidgets()
             if widget not in existing and widget.parentWidget() is None
             and not any(child in existing
                         for child in widget.findChildren(widgets.QWidget))]
    for widget in roots:
        if isValid(widget):
            widget.deleteLater()
    for widget in roots:
        if isValid(widget):
            # Target only our roots: do not drain timers or another fixture's
            # pending DeferredDelete events. Parent deletion owns its children.
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def release_finished_worker_relays():
    """Detach this test's finished worker relays from the module registry.

    ``qq_chat_analyzer.gui.workers`` keeps every submitted worker and its
    callback relay in module-global sets, and ``workers.is_idle()`` reads them to
    report whether the GUI is busy. A relay leaves ``_RELAYS`` only when the
    queued ``finished`` slot runs on the thread that created it, so a test that
    destroys a signal source, or that starts the connection flow from a pool
    thread, leaves a relay behind that no later test can release. Every
    following test then sees a busy GUI even though no work is running.

    Only relays this test added, and only once their worker stopped running, are
    detached. ``_PENDING`` and pre-existing relays stay untouched, so genuinely
    running work still reports busy.
    """
    # Do not import or initialize Qt for tests that do not use it.
    registry = sys.modules.get("qq_chat_analyzer.gui.workers")
    if registry is None:
        yield
        return

    existing = set(registry._RELAYS)
    yield

    for relay in tuple(registry._RELAYS):
        if any(relay is kept for kept in existing):
            continue
        if any(relay is getattr(worker, "_callback_relay", None)
               for worker in registry._PENDING):
            continue
        registry._RELAYS.discard(relay)


@pytest.fixture(autouse=True)
def isolate_qq_runtime_ownership(monkeypatch):
    """A fictional launch must never leave a PID for another test to kill."""
    from qq_chat_analyzer.application.qq import qq_process_registry, qq_runtime_session

    monkeypatch.setattr(
        qq_process_registry, "_DEFAULT_REGISTRY",
        qq_process_registry.QQProcessRegistry(terminator=lambda _pid: True),
    )
    monkeypatch.setattr(qq_runtime_session, "_DEFAULT_SESSION", qq_runtime_session.QQRuntimeSession())


@pytest.fixture(autouse=True)
def isolate_user_data(tmp_path, monkeypatch):
    home = tmp_path / "test-home"
    for variable, directory in {
        "LOCALAPPDATA": tmp_path / "local-app-data",
        "APPDATA": tmp_path / "roaming-app-data",
        "USERPROFILE": home,
        "HOME": home,
        "XDG_DATA_HOME": tmp_path / "xdg-data",
        "XDG_CONFIG_HOME": tmp_path / "xdg-config",
        "XDG_CACHE_HOME": tmp_path / "xdg-cache",
    }.items():
        monkeypatch.setenv(variable, str(directory))
    # Cover home fallback and providers using Path.home(), including tests
    # that remove LOCALAPPDATA. Individual tests can still override this.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


# Primary ownership follows the contract under test, not fixture/source imports.
# GUI source wiring belongs to ui_report; source-specific facade setup to wechat.
# Shared import/analysis orchestration belongs to core; frozen QQ packaging and
# cross-source shutdown/update ownership belong to system. Report storage is ui_report.
# Keep this inventory explicit: new modules must choose a domain during review.
DOMAIN_MODULES = {
    "core": {
        "test_sticker_features",
        "test_group_sticker_features",
        "test_sticker_features_batch2",
        "test_sticker_data",
        "test_analysis_reports",
        "test_analysis_reports_service",
        "test_analyzer",
        "test_application_service",
        "test_cleaner",
        "test_conversation_sessions",
        "test_distinctive_word_analyzer",
        "test_expression_analyzer",
        "test_expression_lexical_boundary",
        "test_filter_pipeline",
        "test_identity_semantics",
        "test_import_service",
        "test_import_service_boundaries",
        "test_message_quality_filter",
        "test_private_language_analyzer",
        "test_private_nudges",
        "test_rich_filtering_association",
        "test_scope_filter",
        "test_sender_identity_filtering",
        "test_smart_profile",
        "test_template_detector",
        "test_tokenizer",
    },
    "qq": {
        "test_bundled_qq_runtime",
        "test_napcat_qq_provider",
        "test_qq_active_resources",
        "test_qq_auth_bridge",
        "test_qq_base_resources",
        "test_qq_bridge_credential",
        "test_qq_connection_admission",
        "test_qq_connection_manager",
        "test_qq_database_provider_audit",
        "test_qq_sqlite_reader",
        "test_qq_db_adapter",
        "test_qq_db_adapter_self_identity",
        "test_qq_db_identity",
        "test_qq_db_import_integration",
        "test_qq_db_nontext_contents",
        "test_qq_db_reply_mentions",
        "test_qq_db_rich_contents",
        "test_qq_direct_acquisition_diagnostics",
        "test_qq_direct_analyze_single_generation",
        "test_qq_direct_database_facade",
        "test_qq_direct_db_lifecycle",
        "test_qq_direct_db_snapshot_runtime",
        "test_qq_direct_identity_names",
        "test_qq_direct_main_wal_acquisition",
        "test_qq_direct_capture_protocol",
        "test_qq_direct_main_wal_poc",
        "test_qq_direct_scope_acquisition",
        "test_qq_direct_self_identity",
        "test_qq_direct_session_discovery",
        "test_qq_direct_session_materialization",
        "test_qq_direct_snapshot_lease",
        "test_qq_direct_snapshot_runtime_client",
        "test_qq_direct_snapshot_workspace",
        "test_qq_explicit_reconnect",
        "test_qq_job_wiring",
        "test_qq_large_database_decrypt",
        "test_qq_napcat_runtime_bootstrap",
        "test_qq_napcat_upgrade",
        "test_qq_private_unknown_sender",
        "test_qq_private_nudges",
        "test_qq_process_detection",
        "test_qq_process_ownership_recovery",
        "test_qq_process_registry",
        "test_qq_process_registry_diagnostics",
        "test_qq_runtime_paths",
        "test_qq_runtime_session",
        "test_qq_runtime_wiring",
        "test_qq_runtime_workspace",
        "test_qq_setup_service",
        "test_qq_version_gate",
        "test_qq_worker_generation",
    },
    "wechat": {
        "test_facade_wechat_setup",
        "test_wcdb_cli_unlimited",
        "test_wechat_chatlab_jsonl",
        "test_wechat_connection_service",
        "test_wechat_data_detector",
        "test_wechat_database_probe",
        "test_wechat_database_recovery",
        "test_wechat_db_source",
        "test_wechat_environment_config",
        "test_wechat_export_import_service",
        "test_wechat_key_helper_resources",
        "test_wechat_key_helper_tree",
        "test_wechat_key_runtime_validation",
        "test_wechat_key_service",
        "test_wechat_provider_factory",
        "test_wechat_runtime_bootstrap",
        "test_wechat_setup_service",
        "test_wechat_sticker_cdn",
        "test_wechat_wcdb_diagnostic",
        "test_wx_key_helper_powershell",
    },
    "ui_report": {
        "test_language_sources",
        "test_echo_member_controls",
        "test_cli",
        "test_echo_conversation_sessions_frontend",
        "test_echo_group_sessions_frontend",
        "test_echo_report_export",
        "test_echo_report_serializer",
        "test_facade",
        "test_gui",
        "test_gui_history_bulk",
        "test_gui_qq_deqce_wiring",
        "test_gui_qq_qr_waiting",
        "test_gui_report_package_deletion",
        "test_gui_setup_uninstall",
        "test_gui_shutdown_protocol",
        "test_gui_update_check",
        "test_gui_update_integration",
        "test_report_package_catalog",
        "test_report_staging_recovery",
        "test_session_analysis_ownership",
    },
    "system": {
        "test_app_version",
        "test_desktop_bootstrap",
        "test_desktop_exit_ownership",
        "test_desktop_exit_subprocess",
        "test_desktop_natural_exit",
        "test_desktop_runtime",
        "test_diagnostics",
        "test_frozen_desktop_package_contract",
        "test_frozen_qq_resource_contract",
        "test_idle_shutdown_evidence",
        "test_installable_update_service",
        "test_package_windows_portable",
        "test_package_windows_setup",
        "test_prepare_cnb_release",
        "test_portable_runtime_copy",
        "test_setup_uninstall",
        "test_setup_uninstall_broker",
        "test_uninstall_data",
        "test_shutdown_diagnostics",
        "test_shutdown_ownership",
        "test_shutdown_results",
        "test_shutdown_worker_results",
        "test_test_user_data_isolation",
        "test_update_channel",
        "test_update_check_service",
        "test_update_install_handoff",
        "test_update_shutdown_admission",
        "test_windows_job_process",
        "test_windows_runtime_contract",
    },
}

# Exact collected node IDs keep parameter expansion out of Smoke by accident.
# Small synthetic seams and refusal checks; no native runtime/browser/subprocess.
SMOKE_NODES = {
    "tests/test_analyzer.py::test_counts_repeated_tokens",
    "tests/test_analyzer.py::test_counts_word_occurrences_for_each_sender",
    "tests/test_analysis_reports.py::test_activity_analyzer_counts_epoch_in_beijing_local_hour",
    "tests/test_application_service.py::test_execute_returns_completed_privacy_safe_result_without_cli_output",
    "tests/test_application_service.py::test_execute_rejects_missing_input_without_exposing_path",
    "tests/test_qq_db_import_integration.py::test_session_payload_import_preserves_session_identity_for_group_and_private",
    "tests/test_qq_bridge_credential.py::test_provider_without_a_credential_never_sends_a_request",
    "tests/test_wechat_export_import_service.py::test_execute_exports_then_imports_into_chat_messages",
    "tests/test_wechat_db_source.py::test_key_is_never_placed_on_the_command_line",
    "tests/test_echo_report_export.py::test_package_creates_echo_report_directory_with_expected_files",
    "tests/test_echo_report_export.py::test_package_never_copies_raw_chat_data",
    "tests/test_echo_report_serializer.py::test_echo_report_html_escapes_injected_user_text",
    "tests/test_report_package_catalog.py::test_delete_packages_rejects_whole_selection_when_one_name_is_invalid",
    "tests/test_diagnostics.py::test_sensitive_filter_redacts_key_paths_and_helper_stderr",
    "tests/test_update_install_handoff.py::test_missing_step_forged_token_and_repeated_handoff_are_denied",
}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, items):
    """Add native selection markers without changing test behavior or order."""
    # Validate full collection before pytest applies -m, -k or --deselect.
    # Explicit file/node selections and --ignore runs are intentionally partial.
    config = session.config
    full_roots = {config.rootpath, Path(__file__).resolve().parent}
    full_suite = any(
        "::" not in arg and Path(arg).resolve() in full_roots
        for arg in config.args
    )
    if full_suite and not (config.getoption("ignore") or config.getoption("ignore_glob")):
        missing = SMOKE_NODES - {item.nodeid for item in items}
        if missing:
            raise pytest.UsageError(
                "Missing Smoke nodes; update SMOKE_NODES in tests/conftest.py:\n"
                + "\n".join(sorted(missing))
            )

    for item in items:
        domains = [name for name, modules in DOMAIN_MODULES.items()
                   if item.path.stem in modules]
        if len(domains) != 1:
            raise pytest.UsageError(
                f"Choose exactly one domain in tests/conftest.py for {item.nodeid}"
            )
        item.add_marker(getattr(pytest.mark, domains[0]))
        if item.nodeid in SMOKE_NODES:
            item.add_marker(pytest.mark.smoke)
