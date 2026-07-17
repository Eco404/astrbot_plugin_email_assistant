from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CONFIG_SECTIONS: dict[str, str] = {
    "admin_uids": "general_settings",
    "poll_interval_seconds": "general_settings",
    "network_timeout_seconds": "general_settings",
    "max_fetch_per_check": "general_settings",
    "max_query_results": "general_settings",
    "detail_body_max_chars": "general_settings",
    "llm_mail_write_enabled": "llm_write_settings",
    "llm_send_confirmation_ttl_minutes": "llm_write_settings",
    "llm_draft_body_max_chars": "llm_write_settings",
    "notification_mode": "notification_settings",
    "narration_provider_id": "notification_settings",
    "llm_write_official_history": "notification_settings",
    "narration_prompt": "notification_settings",
    "narration_body_max_chars": "notification_settings",
    "narration_max_tokens": "notification_settings",
    "cron_narration_delay_seconds": "notification_settings",
    "mail_processing_provider_id": "webui_settings",
    "mail_summary_prompt": "webui_settings",
    "mail_translation_prompt": "webui_settings",
    "translation_language": "webui_settings",
    "webui_auto_show_cached_ai": "webui_settings",
    "mail_processing_body_max_chars": "webui_settings",
    "mail_processing_max_tokens": "webui_settings",
    "local_index_enabled": "storage_settings",
    "local_index_initial_days": "storage_settings",
    "local_index_initial_max_messages": "storage_settings",
    "local_index_sync_batch_size": "storage_settings",
    "local_index_reconcile_interval_hours": "storage_settings",
    "local_index_all_folders": "storage_settings",
    "secondary_folders_per_poll": "storage_settings",
    "folder_list_refresh_interval_hours": "storage_settings",
    "body_cache_mode": "storage_settings",
    "body_cache_retention_days": "storage_settings",
    "body_cache_max_item_kb": "storage_settings",
    "body_cache_max_total_mb": "storage_settings",
    "body_cache_purge_on_remote_delete": "storage_settings",
}


def config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read grouped settings while keeping pre-v2.2 flat configs compatible."""
    section_name = CONFIG_SECTIONS.get(key)
    getter = getattr(config, "get", None)
    if section_name and callable(getter):
        section = getter(section_name)
        if isinstance(section, Mapping) and key in section:
            return section[key]
    if callable(getter):
        return getter(key, default)
    return default
