-- Released v1 database schema used for upgrade compatibility checks.
CREATE TABLE IF NOT EXISTS mailboxes (
    account_id TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    last_synced_uid INTEGER NOT NULL DEFAULT 0,
    history_before_uid INTEGER NOT NULL DEFAULT 0,
    history_complete INTEGER NOT NULL DEFAULT 0,
    last_sync_at REAL NOT NULL DEFAULT 0,
    last_reconcile_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, folder)
);

CREATE TABLE IF NOT EXISTS mail_messages (
    account_id TEXT NOT NULL,
    message_key TEXT NOT NULL,
    identity_kind TEXT NOT NULL,
    identity_value TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    gmail_msgid TEXT NOT NULL DEFAULT '',
    first_seen_at REAL NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, message_key)
);

CREATE INDEX IF NOT EXISTS idx_mail_messages_identity
ON mail_messages (account_id, identity_kind, identity_value);

CREATE TABLE IF NOT EXISTS mail_headers (
    account_id TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    message_key TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    from_name TEXT NOT NULL DEFAULT '',
    from_addr TEXT NOT NULL DEFAULT '',
    reply_to TEXT NOT NULL DEFAULT '',
    date_text TEXT NOT NULL DEFAULT '',
    date_ts REAL NOT NULL DEFAULT 0,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    message_id TEXT NOT NULL DEFAULT '',
    references_text TEXT NOT NULL DEFAULT '',
    remote_state TEXT NOT NULL DEFAULT 'active',
    last_seen_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, folder, uidvalidity, uid),
    FOREIGN KEY (account_id, message_key)
        REFERENCES mail_messages (account_id, message_key)
);

CREATE INDEX IF NOT EXISTS idx_mail_headers_query
ON mail_headers (
    account_id, folder, uidvalidity, remote_state, date_ts DESC, uid DESC
);

CREATE TABLE IF NOT EXISTS mail_bodies (
    account_id TEXT NOT NULL,
    message_key TEXT NOT NULL,
    body_text TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL DEFAULT 0,
    last_accessed_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, message_key),
    FOREIGN KEY (account_id, message_key)
        REFERENCES mail_messages (account_id, message_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mail_bodies_eviction
ON mail_bodies (last_accessed_at, fetched_at);

CREATE TABLE IF NOT EXISTS mail_folders (
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    delimiter TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '[]',
    selectable INTEGER NOT NULL DEFAULT 1,
    special_use TEXT NOT NULL DEFAULT '',
    remote_state TEXT NOT NULL DEFAULT 'active',
    last_seen_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, name)
);

CREATE INDEX IF NOT EXISTS idx_mail_folders_list
ON mail_folders (account_id, remote_state, special_use, name);

CREATE TABLE IF NOT EXISTS mail_ai_cache (
    account_id TEXT NOT NULL,
    message_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    task TEXT NOT NULL,
    target_language TEXT NOT NULL DEFAULT '',
    result_text TEXT NOT NULL,
    provider_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    PRIMARY KEY (
        account_id, message_key, content_hash,
        task, target_language
    ),
    FOREIGN KEY (account_id, message_key)
        REFERENCES mail_messages (account_id, message_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mail_ai_cache_access
ON mail_ai_cache (last_accessed_at);

CREATE TABLE IF NOT EXISTS mail_drafts (
    draft_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    to_json TEXT NOT NULL DEFAULT '[]',
    cc_json TEXT NOT NULL DEFAULT '[]',
    bcc_json TEXT NOT NULL DEFAULT '[]',
    subject TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    body_html TEXT NOT NULL DEFAULT '',
    reply_folder TEXT NOT NULL DEFAULT '',
    reply_uid INTEGER,
    reply_message_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user'
        CHECK (source IN ('user', 'bot')),
    owner_umo TEXT NOT NULL DEFAULT '',
    owner_sender_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'editing'
        CHECK (status IN (
            'editing', 'pending_review', 'approved', 'sending',
            'sent', 'failed', 'cancelled'
        )),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    sent_at REAL,
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_mail_drafts_list
ON mail_drafts (account_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS mail_send_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    draft_id TEXT NOT NULL,
    draft_revision INTEGER NOT NULL,
    owner_umo TEXT NOT NULL,
    owner_sender_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES mail_drafts (draft_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mail_send_confirmations_draft
ON mail_send_confirmations (draft_id, consumed_at, expires_at);

CREATE TABLE db_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    migrated_at TEXT NOT NULL,
    migration_duration_seconds REAL NOT NULL DEFAULT 0
);
INSERT INTO db_version (version, description, migrated_at) VALUES
    ('v1', '稳定邮件实体、跨文件夹缓存与草稿存储', '2026-07-01T00:00:00+00:00');

