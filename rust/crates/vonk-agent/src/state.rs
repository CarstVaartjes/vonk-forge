use std::{
    fs::{self, OpenOptions},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};
use thiserror::Error;
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, canonical_json, parse_strict,
};

#[derive(Debug, Error)]
pub enum StateError {
    #[error("durable agent state failed")]
    Database(#[from] rusqlite::Error),
    #[error("durable result is invalid")]
    Protocol(#[from] vonk_agent_protocol::ProtocolError),
    #[error("state file is unsafe")]
    Io(#[from] std::io::Error),
    #[error("claim is bound to another node")]
    Identity,
    #[error("claim deadline has elapsed")]
    Expired,
    #[error("claim attempt or fence is stale")]
    Stale,
    #[error("claim is already executing")]
    Busy,
    #[error("result state is invalid")]
    ResultState,
    #[error("legacy Python receipt store is unsafe or incompatible")]
    LegacyImport,
}

#[derive(Debug, Clone, PartialEq)]
pub enum BeginDecision {
    Execute,
    Replay(AgentResult),
}

pub struct StateStore {
    connection: Connection,
    path: PathBuf,
    node_id: String,
}

struct StoredOperation {
    attempt: u32,
    fence: String,
    state: String,
    result: Option<Vec<u8>>,
}

impl StateStore {
    pub fn open(path: &Path, node_id: &str) -> Result<Self, StateError> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        if !path.exists() {
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(path)?;
        }
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
            return Err(std::io::Error::other("state database path is unsafe").into());
        }
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
        let connection = Connection::open(path)?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=FULL;
             PRAGMA foreign_keys=ON;
             PRAGMA trusted_schema=OFF;
             CREATE TABLE IF NOT EXISTS metadata (
               key TEXT PRIMARY KEY NOT NULL,
               value TEXT NOT NULL
             ) STRICT;
             CREATE TABLE IF NOT EXISTS operations (
               operation_id TEXT PRIMARY KEY NOT NULL,
               job_id TEXT NOT NULL,
               node_id TEXT NOT NULL,
               attempt INTEGER NOT NULL CHECK (attempt > 0),
               fence TEXT NOT NULL,
               deadline TEXT NOT NULL,
               state TEXT NOT NULL CHECK (state IN ('running','completed')),
               result_json BLOB,
               result_acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (result_acknowledged IN (0,1)),
               CHECK ((state = 'running' AND result_json IS NULL) OR (state = 'completed' AND result_json IS NOT NULL))
             ) STRICT;",
        )?;
        let stored: Option<String> = connection
            .query_row(
                "SELECT value FROM metadata WHERE key='node_id'",
                [],
                |row| row.get(0),
            )
            .optional()?;
        match stored {
            Some(stored) if stored != node_id => return Err(StateError::Identity),
            None => {
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('node_id', ?1)",
                    [node_id],
                )?;
            }
            Some(_) => {}
        }
        Ok(Self {
            connection,
            path: path.to_owned(),
            node_id: node_id.to_owned(),
        })
    }

    pub fn reopen(&self) -> Result<Self, StateError> {
        Self::open(&self.path, &self.node_id)
    }

    pub fn begin(
        &mut self,
        claim: &AgentClaim,
        now: DateTime<Utc>,
    ) -> Result<BeginDecision, StateError> {
        claim.validate()?;
        if claim.node_id != self.node_id {
            return Err(StateError::Identity);
        }
        if claim.deadline.with_timezone(&Utc) <= now {
            return Err(StateError::Expired);
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing = transaction
            .query_row(
                "SELECT attempt, fence, state, result_json FROM operations WHERE operation_id=?1",
                [claim.operation_id.to_string()],
                |row| {
                    Ok(StoredOperation {
                        attempt: row.get(0)?,
                        fence: row.get(1)?,
                        state: row.get(2)?,
                        result: row.get(3)?,
                    })
                },
            )
            .optional()?;
        let decision = match existing {
            None => {
                transaction.execute(
                    "INSERT INTO operations(operation_id,job_id,node_id,attempt,fence,deadline,state)
                     VALUES (?1,?2,?3,?4,?5,?6,'running')",
                    params![
                        claim.operation_id.to_string(),
                        claim.job_id.to_string(),
                        claim.node_id,
                        claim.attempt,
                        claim.fence.to_string(),
                        claim.deadline.to_rfc3339(),
                    ],
                )?;
                BeginDecision::Execute
            }
            Some(stored) if claim.attempt < stored.attempt => return Err(StateError::Stale),
            Some(stored) if claim.attempt == stored.attempt => {
                if stored.fence != claim.fence.to_string() {
                    return Err(StateError::Stale);
                }
                if stored.state == "running" {
                    return Err(StateError::Busy);
                }
                let bytes = stored.result.ok_or(StateError::ResultState)?;
                BeginDecision::Replay(parse_strict(&bytes)?)
            }
            Some(_) => {
                transaction.execute(
                    "UPDATE operations SET job_id=?2,node_id=?3,attempt=?4,fence=?5,deadline=?6,
                     state='running',result_json=NULL,result_acknowledged=0 WHERE operation_id=?1",
                    params![
                        claim.operation_id.to_string(),
                        claim.job_id.to_string(),
                        claim.node_id,
                        claim.attempt,
                        claim.fence.to_string(),
                        claim.deadline.to_rfc3339(),
                    ],
                )?;
                BeginDecision::Execute
            }
        };
        transaction.commit()?;
        Ok(decision)
    }

    pub fn finish(
        &mut self,
        claim: &AgentClaim,
        state: &str,
        result: Value,
    ) -> Result<AgentResult, StateError> {
        if !matches!(state, "succeeded" | "failed" | "waiting-for-operator") {
            return Err(StateError::ResultState);
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let deadline: String = transaction
            .query_row(
                "SELECT deadline FROM operations
                 WHERE operation_id=?1 AND attempt=?2 AND fence=?3 AND state='running'",
                params![
                    claim.operation_id.to_string(),
                    claim.attempt,
                    claim.fence.to_string()
                ],
                |row| row.get(0),
            )
            .optional()?
            .ok_or(StateError::Stale)?;
        let result = AgentResult {
            attempt: claim.attempt,
            deadline: DateTime::parse_from_rfc3339(&deadline)
                .map_err(|_| StateError::ResultState)?,
            fence: claim.fence,
            job_id: claim.job_id,
            node_id: claim.node_id.clone(),
            operation_id: claim.operation_id,
            result,
            schema_version: claim.schema_version,
            state: state.to_owned(),
        };
        result.validate()?;
        let body = canonical_json(&result)?;
        let changed = transaction.execute(
            "UPDATE operations SET state='completed',result_json=?4,result_acknowledged=0
             WHERE operation_id=?1 AND attempt=?2 AND fence=?3 AND state='running'",
            params![
                claim.operation_id.to_string(),
                claim.attempt,
                claim.fence.to_string(),
                body
            ],
        )?;
        if changed != 1 {
            return Err(StateError::Stale);
        }
        transaction.commit()?;
        Ok(result)
    }

    pub fn apply_heartbeat(
        &mut self,
        request: &AgentProgress,
        directive: &AgentDirective,
    ) -> Result<(), StateError> {
        request.validate()?;
        directive.validate()?;
        if request.schema_version != directive.schema_version
            || request.job_id != directive.job_id
            || request.operation_id != directive.operation_id
            || request.attempt != directive.attempt
            || request.fence != directive.fence
            || request.node_id != directive.node_id
            || request.node_id != self.node_id
            || directive.deadline < request.deadline
        {
            return Err(StateError::Stale);
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current: String = transaction
            .query_row(
                "SELECT deadline FROM operations
                 WHERE operation_id=?1 AND job_id=?2 AND node_id=?3
                   AND attempt=?4 AND fence=?5 AND state='running'",
                params![
                    request.operation_id.to_string(),
                    request.job_id.to_string(),
                    request.node_id,
                    request.attempt,
                    request.fence.to_string(),
                ],
                |row| row.get(0),
            )
            .optional()?
            .ok_or(StateError::Stale)?;
        let current =
            DateTime::parse_from_rfc3339(&current).map_err(|_| StateError::ResultState)?;
        if current != request.deadline {
            return Err(StateError::Stale);
        }
        let changed = transaction.execute(
            "UPDATE operations SET deadline=?6
             WHERE operation_id=?1 AND job_id=?2 AND node_id=?3
               AND attempt=?4 AND fence=?5 AND state='running'",
            params![
                request.operation_id.to_string(),
                request.job_id.to_string(),
                request.node_id,
                request.attempt,
                request.fence.to_string(),
                directive.deadline.to_rfc3339(),
            ],
        )?;
        if changed != 1 {
            return Err(StateError::Stale);
        }
        transaction.commit()?;
        Ok(())
    }

    pub fn pending_results(&self) -> Result<Vec<AgentResult>, StateError> {
        let mut statement = self.connection.prepare(
            "SELECT result_json FROM operations
             WHERE state='completed' AND result_acknowledged=0 ORDER BY rowid",
        )?;
        let values = statement
            .query_map([], |row| row.get::<_, Vec<u8>>(0))?
            .collect::<Result<Vec<_>, _>>()?;
        values
            .into_iter()
            .map(|value| parse_strict(&value).map_err(StateError::from))
            .collect()
    }

    pub fn acknowledge(&mut self, result: &AgentResult) -> Result<(), StateError> {
        result.validate()?;
        let changed = self.connection.execute(
            "UPDATE operations SET result_acknowledged=1
             WHERE operation_id=?1 AND attempt=?2 AND fence=?3 AND state='completed'",
            params![
                result.operation_id.to_string(),
                result.attempt,
                result.fence.to_string()
            ],
        )?;
        if changed != 1 {
            return Err(StateError::Stale);
        }
        Ok(())
    }

    pub fn recover_interrupted(&mut self) -> Result<(), StateError> {
        let claims = {
            let mut statement = self.connection.prepare(
                "SELECT job_id,operation_id,attempt,fence,node_id,deadline FROM operations WHERE state='running'",
            )?;
            statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, u32>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                    ))
                })?
                .collect::<Result<Vec<_>, _>>()?
        };
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        for (job_id, operation_id, attempt, fence, node_id, deadline) in claims {
            let result = AgentResult {
                attempt,
                deadline: DateTime::parse_from_rfc3339(&deadline)
                    .map_err(|_| StateError::ResultState)?,
                fence: fence.parse().map_err(|_| StateError::ResultState)?,
                job_id: job_id.parse().map_err(|_| StateError::ResultState)?,
                node_id,
                operation_id: operation_id.parse().map_err(|_| StateError::ResultState)?,
                result: json!({"reason": "agent restarted with an operation in progress"}),
                schema_version: 1,
                state: "waiting-for-operator".to_owned(),
            };
            result.validate()?;
            transaction.execute(
                "UPDATE operations SET state='completed',result_json=?2,result_acknowledged=0
                 WHERE operation_id=?1 AND state='running'",
                params![operation_id, canonical_json(&result)?],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    /// Import only terminal, canonical receipts from a stopped Python agent.
    ///
    /// The legacy database is opened read-only and must have its exact v1
    /// table shape. Active work is rejected: an operator must resolve it with
    /// the Python agent before cutover. No credential material is read.
    pub fn import_python_receipts(&mut self, source: &Path) -> Result<usize, StateError> {
        let metadata = fs::symlink_metadata(source)?;
        if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
            return Err(StateError::LegacyImport);
        }
        let legacy = Connection::open_with_flags(
            source,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        legacy.execute_batch("PRAGMA query_only=ON; PRAGMA trusted_schema=OFF;")?;
        let columns = {
            let mut statement = legacy.prepare("PRAGMA table_info(attempts)")?;
            statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, i64>(3)?,
                        row.get::<_, i64>(5)?,
                    ))
                })?
                .collect::<Result<Vec<_>, _>>()?
        };
        let expected = [
            ("node_id", "TEXT", 1, 1),
            ("job_id", "TEXT", 1, 2),
            ("operation_id", "TEXT", 1, 3),
            ("attempt", "INTEGER", 1, 4),
            ("fence", "TEXT", 1, 0),
            ("state", "TEXT", 1, 0),
            ("claim_json", "BLOB", 1, 0),
            ("progress_sequence", "INTEGER", 1, 0),
            ("progress_json", "BLOB", 0, 0),
            ("result_json", "BLOB", 0, 0),
            ("created_at", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
            ("finished_at", "TEXT", 0, 0),
            ("acknowledged_at", "TEXT", 0, 0),
        ];
        if columns.len() != expected.len()
            || columns.iter().zip(expected).any(|(actual, expected)| {
                actual.0 != expected.0
                    || actual.1.to_ascii_uppercase() != expected.1
                    || actual.2 != expected.2
                    || actual.3 != expected.3
            })
        {
            return Err(StateError::LegacyImport);
        }
        let active: i64 = legacy.query_row(
            "SELECT count(*) FROM attempts WHERE state='active'",
            [],
            |row| row.get(0),
        )?;
        if active != 0 {
            return Err(StateError::LegacyImport);
        }
        let receipts = {
            let mut statement = legacy.prepare(
                "SELECT node_id,job_id,operation_id,attempt,fence,state,result_json,acknowledged_at
                 FROM attempts ORDER BY rowid",
            )?;
            statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, u32>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, Vec<u8>>(6)?,
                        row.get::<_, Option<String>>(7)?,
                    ))
                })?
                .collect::<Result<Vec<_>, _>>()?
        };
        let mut validated = Vec::with_capacity(receipts.len());
        for (node_id, job_id, operation_id, attempt, fence, state, body, acknowledged_at) in
            receipts
        {
            let result: AgentResult = parse_strict(&body)?;
            result.validate()?;
            if node_id != self.node_id
                || result.node_id != node_id
                || result.job_id.to_string() != job_id
                || result.operation_id.to_string() != operation_id
                || result.attempt != attempt
                || result.fence.to_string() != fence
                || result.state != state
                || canonical_json(&result)? != body
            {
                return Err(StateError::LegacyImport);
            }
            validated.push((result, body, acknowledged_at.is_some()));
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        for (result, body, acknowledged) in &validated {
            let existing: Option<(u32, String, Vec<u8>, i64)> = transaction
                .query_row(
                    "SELECT attempt,fence,result_json,result_acknowledged FROM operations
                     WHERE operation_id=?1",
                    [result.operation_id.to_string()],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )
                .optional()?;
            if let Some((attempt, fence, stored, stored_acknowledged)) = existing {
                if attempt != result.attempt
                    || fence != result.fence.to_string()
                    || stored != *body
                    || stored_acknowledged != i64::from(*acknowledged)
                {
                    return Err(StateError::LegacyImport);
                }
                continue;
            }
            transaction.execute(
                "INSERT INTO operations
                 (operation_id,job_id,node_id,attempt,fence,deadline,state,result_json,result_acknowledged)
                 VALUES (?1,?2,?3,?4,?5,?6,'completed',?7,?8)",
                params![
                    result.operation_id.to_string(),
                    result.job_id.to_string(),
                    result.node_id,
                    result.attempt,
                    result.fence.to_string(),
                    result.deadline.to_rfc3339(),
                    body,
                    i64::from(*acknowledged),
                ],
            )?;
        }
        transaction.commit()?;
        Ok(validated.len())
    }
}

pub fn backoff_delay(attempt: u32, entropy: u64, minimum: u64, maximum: u64) -> Duration {
    assert!(minimum > 0 && minimum <= maximum);
    let multiplier = 1_u64.checked_shl(attempt.min(62)).unwrap_or(u64::MAX);
    let base = minimum.saturating_mul(multiplier).min(maximum);
    let lower = (base.saturating_mul(3) / 4).max(minimum);
    let upper = (base.saturating_mul(5) / 4).min(maximum).max(lower);
    Duration::from_secs(lower + entropy % (upper - lower + 1))
}
