use std::{
    fs::{self, OpenOptions},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;
use thiserror::Error;
use vonk_agent_protocol::generated::{
    AgentFailureKind, AgentFailureResult, AgentOperation, AgentResultResult, AgentResultState,
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, canonical_json, parse_strict,
};

const STATE_SCHEMA_VERSION: &str = "2";

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
    #[error(
        "durable agent state schema is incompatible; stop the agent, verify no operation is active or pending, then remove only state.sqlite"
    )]
    IncompatibleSchema,
}

#[derive(Debug, Clone, PartialEq)]
pub enum BeginDecision {
    Execute,
    Replay(Box<AgentResult>),
}

pub struct StateStore {
    connection: Connection,
    path: PathBuf,
    node_id: String,
}

struct StoredOperation {
    attempt: u32,
    fence: String,
    operation: String,
    state: String,
    result: Option<Vec<u8>>,
}

/// Bounded, durable record of one Controller refusal of an agent result.
///
/// The receipt itself stays in `operations`; this row only explains why the
/// same bytes must not be re-sent immediately, and when they may be offered
/// again after the cause is corrected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResultRejection {
    pub http_status: u16,
    pub code: String,
    pub decision: String,
    pub request_id: Option<String>,
    pub reason: String,
    pub observed_at: DateTime<Utc>,
    pub retry_due_at: DateTime<Utc>,
    pub rejections: u32,
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
               operation TEXT NOT NULL,
               deadline TEXT NOT NULL,
               state TEXT NOT NULL CHECK (state IN ('running','completed')),
               result_json BLOB,
               result_acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (result_acknowledged IN (0,1)),
               CHECK ((state = 'running' AND result_json IS NULL) OR (state = 'completed' AND result_json IS NOT NULL))
             ) STRICT;
             CREATE TABLE IF NOT EXISTS result_reconciliation (
               operation_id TEXT PRIMARY KEY NOT NULL,
               attempt INTEGER NOT NULL,
               fence TEXT NOT NULL
             ) STRICT;
             CREATE TABLE IF NOT EXISTS result_rejections (
               operation_id TEXT PRIMARY KEY NOT NULL,
               attempt INTEGER NOT NULL CHECK (attempt > 0),
               fence TEXT NOT NULL,
               http_status INTEGER NOT NULL,
               code TEXT NOT NULL,
               decision TEXT NOT NULL,
               request_id TEXT,
               reason TEXT NOT NULL,
               observed_at TEXT NOT NULL,
               retry_due_at TEXT NOT NULL,
               rejections INTEGER NOT NULL DEFAULT 1 CHECK (rejections > 0)
             ) STRICT;",
        )?;
        let operation_column = {
            let mut statement = connection.prepare("PRAGMA table_info(operations)")?;
            statement
                .query_map([], |row| row.get::<_, String>(1))?
                .collect::<Result<Vec<_>, _>>()?
                .into_iter()
                .any(|name| name == "operation")
        };
        if !operation_column {
            return Err(StateError::IncompatibleSchema);
        }
        let state_schema: Option<String> = connection
            .query_row(
                "SELECT value FROM metadata WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .optional()?;
        match state_schema.as_deref() {
            Some(STATE_SCHEMA_VERSION) => {}
            None => {
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('schema_version', ?1)",
                    [STATE_SCHEMA_VERSION],
                )?;
            }
            Some(_) => return Err(StateError::IncompatibleSchema),
        }
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
                "SELECT attempt, fence, operation, state, result_json FROM operations WHERE operation_id=?1",
                [claim.operation_id.to_string()],
                |row| {
                    Ok(StoredOperation {
                        attempt: row.get(0)?,
                        fence: row.get(1)?,
                        operation: row.get(2)?,
                        state: row.get(3)?,
                        result: row.get(4)?,
                    })
                },
            )
            .optional()?;
        let decision = match existing {
            None => {
                transaction.execute(
                    "INSERT INTO operations(operation_id,job_id,node_id,attempt,fence,operation,deadline,state)
                     VALUES (?1,?2,?3,?4,?5,?6,?7,'running')",
                    params![
                        claim.operation_id.to_string(),
                        claim.job_id.to_string(),
                        claim.node_id,
                        claim.attempt,
                        claim.fence.to_string(),
                        claim.operation.to_string(),
                        claim.deadline.to_rfc3339(),
                    ],
                )?;
                BeginDecision::Execute
            }
            Some(stored) if stored.operation != claim.operation.as_str() => {
                return Err(StateError::Identity);
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
                let result: AgentResult = parse_strict(&bytes)?;
                result.validate_for_operation(&claim.operation)?;
                BeginDecision::Replay(Box::new(result))
            }
            Some(_) => {
                transaction.execute(
                    "UPDATE operations SET job_id=?2,node_id=?3,attempt=?4,fence=?5,operation=?6,deadline=?7,
                     state='running',result_json=NULL,result_acknowledged=0 WHERE operation_id=?1",
                    params![
                        claim.operation_id.to_string(),
                        claim.job_id.to_string(),
                        claim.node_id,
                        claim.attempt,
                        claim.fence.to_string(),
                        claim.operation.to_string(),
                        claim.deadline.to_rfc3339(),
                    ],
                )?;
                // A newer authorised attempt replaces the old evidence, so the
                // old attempt's refusal no longer suppresses anything.
                transaction.execute(
                    "DELETE FROM result_rejections WHERE operation_id=?1",
                    [claim.operation_id.to_string()],
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
        if !matches!(
            state,
            "succeeded" | "failed" | "cancelled" | "waiting-for-operator"
        ) {
            return Err(StateError::ResultState);
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (operation, deadline): (String, String) = transaction
            .query_row(
                "SELECT operation, deadline FROM operations
                 WHERE operation_id=?1 AND attempt=?2 AND fence=?3 AND state='running'",
                params![
                    claim.operation_id.to_string(),
                    claim.attempt,
                    claim.fence.to_string()
                ],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?
            .ok_or(StateError::Stale)?;
        if operation != claim.operation.as_str() {
            return Err(StateError::Identity);
        }
        let result = AgentResult {
            attempt: claim.attempt,
            deadline: DateTime::parse_from_rfc3339(&deadline)
                .map_err(|_| StateError::ResultState)?,
            fence: claim.fence,
            job_id: claim.job_id,
            node_id: claim.node_id.clone(),
            operation_id: claim.operation_id,
            result: serde_json::from_value(result).map_err(|_| StateError::ResultState)?,
            schema_version: claim.schema_version,
            state: state.parse().map_err(|_| StateError::ResultState)?,
        };
        result.validate_for_operation(&claim.operation)?;
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

    /// Return the recorded refusal of this exact result while its bounded
    /// cool-down is still open.
    pub fn result_rejection(
        &self,
        result: &AgentResult,
        now: DateTime<Utc>,
    ) -> Result<Option<ResultRejection>, StateError> {
        let row = self
            .connection
            .query_row(
                "SELECT http_status,code,decision,request_id,reason,observed_at,retry_due_at,rejections
                 FROM result_rejections
                 WHERE operation_id=?1 AND attempt=?2 AND fence=?3 AND retry_due_at > ?4",
                params![
                    result.operation_id.to_string(),
                    result.attempt,
                    result.fence.to_string(),
                    now.to_rfc3339(),
                ],
                |row| {
                    Ok((
                        row.get::<_, u16>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, Option<String>>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, String>(6)?,
                        row.get::<_, u32>(7)?,
                    ))
                },
            )
            .optional()?;
        let Some((
            http_status,
            code,
            decision,
            request_id,
            reason,
            observed_at,
            retry_due_at,
            rejections,
        )) = row
        else {
            return Ok(None);
        };
        Ok(Some(ResultRejection {
            http_status,
            code,
            decision,
            request_id,
            reason,
            observed_at: DateTime::parse_from_rfc3339(&observed_at)
                .map_err(|_| StateError::ResultState)?
                .with_timezone(&Utc),
            retry_due_at: DateTime::parse_from_rfc3339(&retry_due_at)
                .map_err(|_| StateError::ResultState)?
                .with_timezone(&Utc),
            rejections,
        }))
    }

    /// Record one Controller ingress refusal of this exact result.
    ///
    /// Only bounded control-plane facts are persisted: the HTTP status, the
    /// validated error code, the decision, the request id and the Controller's
    /// bounded Display line.  The result itself is untouched, so the receipt
    /// and the refusal both survive a restart and can be reconciled after the
    /// cause is corrected.
    pub fn reject_result(
        &mut self,
        result: &AgentResult,
        error: &crate::client::ControllerError,
        now: DateTime<Utc>,
    ) -> Result<ResultRejection, StateError> {
        result.validate()?;
        let previous: u32 = self
            .connection
            .query_row(
                "SELECT rejections FROM result_rejections WHERE operation_id=?1",
                [result.operation_id.to_string()],
                |row| row.get(0),
            )
            .optional()?
            .unwrap_or(0);
        let rejections = previous.saturating_add(1);
        // Bounded exponential cool-down: long enough that the loop never
        // hot-loops the same rejected bytes, short enough that a corrected
        // Controller rule reconciles the retained evidence promptly.
        let delay = backoff_delay(rejections, u64::from(now.timestamp_subsec_nanos()), 15, 900);
        let retry_due_at =
            now + chrono::Duration::from_std(delay).map_err(|_| StateError::ResultState)?;
        // Keep the Controller's own bounded validation digest: it names the
        // failing boundary, field and rule, which is what makes the refusal
        // actionable.  Status, code and request id have their own columns.
        let reason: String = error.rejection_context();
        self.connection.execute(
            "INSERT INTO result_rejections(
               operation_id,attempt,fence,http_status,code,decision,request_id,reason,
               observed_at,retry_due_at,rejections)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)
             ON CONFLICT(operation_id) DO UPDATE SET
               attempt=excluded.attempt,
               fence=excluded.fence,
               http_status=excluded.http_status,
               code=excluded.code,
               decision=excluded.decision,
               request_id=excluded.request_id,
               reason=excluded.reason,
               observed_at=excluded.observed_at,
               retry_due_at=excluded.retry_due_at,
               rejections=excluded.rejections",
            params![
                result.operation_id.to_string(),
                result.attempt,
                result.fence.to_string(),
                error.status,
                error.code,
                error.decision,
                error.request_id,
                reason,
                now.to_rfc3339(),
                retry_due_at.to_rfc3339(),
                rejections,
            ],
        )?;
        Ok(ResultRejection {
            http_status: error.status,
            code: error.code.clone(),
            decision: error.decision.to_owned(),
            request_id: error.request_id.clone(),
            reason,
            observed_at: now,
            retry_due_at,
            rejections,
        })
    }

    /// Drop a refusal once the Controller has accepted or superseded the
    /// result, or once a newer attempt replaces this one.
    fn clear_result_rejection(&mut self, result: &AgentResult) -> Result<(), StateError> {
        self.connection.execute(
            "DELETE FROM result_rejections WHERE operation_id=?1",
            [result.operation_id.to_string()],
        )?;
        Ok(())
    }

    pub fn pending_results(&self) -> Result<Vec<(AgentOperation, AgentResult)>, StateError> {
        let mut statement = self.connection.prepare(
            "SELECT operation,result_json FROM operations
             WHERE state='completed' AND result_acknowledged=0 ORDER BY rowid",
        )?;
        let values = statement
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        values
            .into_iter()
            .map(|(operation, value)| {
                let operation = operation.parse().map_err(|_| StateError::ResultState)?;
                let result: AgentResult = parse_strict(&value)?;
                result.validate_for_operation(&operation)?;
                Ok((operation, result))
            })
            .collect()
    }

    /// Results acknowledged by older agents may include a refused 409. Offer
    /// each retained receipt once to the Controller's diagnostic-only path.
    /// The Controller deduplicates an outcome it already accepted and retains
    /// an expired exact-fence outcome without applying it to live workload state.
    pub fn unreconciled_results(&self) -> Result<Vec<(AgentOperation, AgentResult)>, StateError> {
        let mut statement = self.connection.prepare(
            "SELECT o.operation,o.result_json FROM operations o
             LEFT JOIN result_reconciliation r ON r.operation_id=o.operation_id
             WHERE o.state='completed' AND o.result_acknowledged=1
               AND r.operation_id IS NULL ORDER BY o.rowid LIMIT 16",
        )?;
        let values = statement
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        values
            .into_iter()
            .map(|(operation, value)| {
                let operation = operation.parse().map_err(|_| StateError::ResultState)?;
                let result: AgentResult = parse_strict(&value)?;
                result.validate_for_operation(&operation)?;
                Ok((operation, result))
            })
            .collect()
    }

    pub fn mark_reconciled(&mut self, result: &AgentResult) -> Result<(), StateError> {
        result.validate()?;
        self.connection.execute(
            "INSERT OR IGNORE INTO result_reconciliation(operation_id,attempt,fence)
             SELECT operation_id,attempt,fence FROM operations
             WHERE operation_id=?1 AND attempt=?2 AND fence=?3
               AND state='completed' AND result_acknowledged=1",
            params![
                result.operation_id.to_string(),
                result.attempt,
                result.fence.to_string()
            ],
        )?;
        Ok(())
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
        self.clear_result_rejection(result)?;
        Ok(())
    }

    /// Stop re-sending a result the Controller refused as no longer current.
    ///
    /// The recorded outcome is retained with its attempt and fence so the work
    /// this agent actually performed stays observable, but it is never sent
    /// again: the Controller refused it, so re-sending it cannot make it commit
    /// workload state and would only spin the loop.
    pub fn supersede(&mut self, result: &AgentResult) -> Result<(), StateError> {
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
        self.clear_result_rejection(result)?;
        Ok(())
    }

    pub fn recover_interrupted(&mut self) -> Result<(), StateError> {
        let claims = {
            let mut statement = self.connection.prepare(
                "SELECT job_id,operation_id,attempt,fence,node_id,operation,deadline FROM operations WHERE state='running'",
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
                        row.get::<_, String>(6)?,
                    ))
                })?
                .collect::<Result<Vec<_>, _>>()?
        };
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        for (job_id, operation_id, attempt, fence, node_id, operation, deadline) in claims {
            let operation: AgentOperation =
                operation.parse().map_err(|_| StateError::ResultState)?;
            let result = AgentResult {
                attempt,
                deadline: DateTime::parse_from_rfc3339(&deadline)
                    .map_err(|_| StateError::ResultState)?,
                fence: fence.parse().map_err(|_| StateError::ResultState)?,
                job_id: job_id.parse().map_err(|_| StateError::ResultState)?,
                node_id,
                operation_id: operation_id.parse().map_err(|_| StateError::ResultState)?,
                // Startup cannot establish whether the host action finished.
                // Preserve that uncertainty as typed evidence: the Controller
                // owns any new attempt and its current intent/authority checks.
                result: AgentResultResult::AgentFailureResult(AgentFailureResult {
                    error_code: Some("agent_restart_interrupted".to_owned()),
                    failure_kind: Some(AgentFailureKind::UncertainEffect),
                    operation: Some(operation),
                    reason: Some("agent restarted with an operation in progress".to_owned()),
                    uncertain: Some(true),
                    ..Default::default()
                }),
                schema_version: 1,
                state: AgentResultState::WaitingForOperator,
            };
            result.validate_for_operation(&operation)?;
            transaction.execute(
                "UPDATE operations SET state='completed',result_json=?2,result_acknowledged=0
                 WHERE operation_id=?1 AND state='running'",
                params![operation_id, canonical_json(&result)?],
            )?;
        }
        transaction.commit()?;
        Ok(())
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
