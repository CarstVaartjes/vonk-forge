use std::{
    fs::{self, OpenOptions},
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use thiserror::Error;
use vonk_agent_protocol::generated::{AgentOperation, FailureStage, ProgressPhase, WaitReason};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, canonical_json, parse_strict,
};

use crate::outcome::{ExecutionResult, UnknownEvidence};

const STATE_SCHEMA_VERSION: &str = "3";

#[derive(Debug, Error)]
pub enum StateError {
    #[error("durable agent state failed")]
    Database(#[from] rusqlite::Error),
    #[error("durable result is invalid")]
    Protocol(#[from] vonk_agent_protocol::ProtocolError),
    #[error("state file is unsafe")]
    Io(#[from] std::io::Error),
    #[error("claim operation does not match its recorded fence")]
    Identity,
    #[error("claim deadline has elapsed")]
    Expired,
    #[error("claim fence is stale")]
    Stale,
    #[error("claim is already executing")]
    Busy,
    #[error("result state is invalid")]
    ResultState,
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
    delivery_cursor: i64,
    durable_custody: bool,
}

struct StoredOperation {
    operation: String,
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
            let metadata = fs::symlink_metadata(parent)?;
            if !metadata.is_dir() || metadata.file_type().is_symlink() {
                return Err(std::io::Error::other("state directory custody unavailable").into());
            }
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
        connection.busy_timeout(Duration::from_millis(50))?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=FULL;
             PRAGMA foreign_keys=ON;
             PRAGMA trusted_schema=OFF;
             CREATE TABLE IF NOT EXISTS metadata (
               key TEXT PRIMARY KEY NOT NULL,
               value TEXT NOT NULL
             ) STRICT;",
        )?;
        let state_schema: Option<String> = connection
            .query_row(
                "SELECT value FROM metadata WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .optional()?;
        if state_schema.as_deref() != Some(STATE_SCHEMA_VERSION) {
            // Operation receipts of another agent protocol cannot be replayed
            // to this Controller; it re-issues any unfinished work, so start
            // from an empty operation log instead of refusing to run.
            connection.execute_batch(
                "DROP TABLE IF EXISTS operations;
                 DROP TABLE IF EXISTS result_reconciliation;
                 DROP TABLE IF EXISTS result_rejections;",
            )?;
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('schema_version', ?1)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [STATE_SCHEMA_VERSION],
            )?;
        }
        connection.execute_batch(
            "CREATE TABLE IF NOT EXISTS operations (
               fence TEXT PRIMARY KEY NOT NULL,
               operation TEXT NOT NULL,
               deadline TEXT NOT NULL,
               state TEXT NOT NULL CHECK (state IN ('running','completed')),
               result_json BLOB,
               result_acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (result_acknowledged IN (0,1)),
               CHECK ((state = 'running' AND result_json IS NULL) OR (state = 'completed' AND result_json IS NOT NULL))
             ) STRICT;
             CREATE TABLE IF NOT EXISTS result_reconciliation (
               fence TEXT PRIMARY KEY NOT NULL
             ) STRICT;
             CREATE TABLE IF NOT EXISTS result_rejections (
               fence TEXT PRIMARY KEY NOT NULL,
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
        let delivery_cursor = connection
            .query_row(
                "SELECT value FROM metadata WHERE key='receipt_delivery_cursor'",
                [],
                |row| row.get::<_, String>(0),
            )
            .ok()
            .and_then(|value| value.parse::<i64>().ok())
            .filter(|value| *value >= 0)
            .unwrap_or(0);
        Ok(Self {
            connection,
            path: path.to_owned(),
            node_id: node_id.to_owned(),
            delivery_cursor,
            durable_custody: true,
        })
    }

    /// Observation can continue while durable storage is unavailable. This
    /// handle cannot dispatch effects and never replaces the retained journal.
    /// Every pass retries its real owner before admission.
    pub fn observation_only(path: &Path, node_id: &str) -> Result<Self, StateError> {
        Ok(Self {
            connection: Connection::open_in_memory()?,
            path: path.to_owned(),
            node_id: node_id.to_owned(),
            delivery_cursor: 0,
            durable_custody: false,
        })
    }

    pub fn restore_custody(&mut self) {
        // Close the previous connection before any quarantine rename: it must
        // not keep writing the old WAL after the main file is detached. A
        // bounded open reconstructs missing projections after startup too.
        let replacement = match Connection::open_in_memory() {
            Ok(connection) => connection,
            Err(error) => {
                eprintln!("vonk-agent: custody observer unavailable: {error}");
                return;
            }
        };
        drop(std::mem::replace(&mut self.connection, replacement));
        self.durable_custody = false;
        match Self::open_recovered(&self.path, &self.node_id) {
            Ok(restored) => *self = restored,
            Err(error) => eprintln!("vonk-agent: durable custody observation deferred: {error}"),
        }
    }

    /// Scan progress shares the node-bound, FULL-synchronous journal with
    /// effects. Saving follows delivery, so a refused report replays its page.
    pub fn observation_checkpoint(
        &self,
    ) -> Result<Option<crate::oci::RecipeRunObservationCheckpoint>, StateError> {
        let value: Option<String> = self
            .connection
            .query_row(
                "SELECT value FROM metadata WHERE key='recipe_observation_scan_v1'",
                [],
                |row| row.get(0),
            )
            .optional()?;
        if value.as_ref().is_some_and(|value| value.len() > 16 * 1024) {
            return Err(StateError::Identity);
        }
        let checkpoint: Option<crate::oci::RecipeRunObservationCheckpoint> = value
            .map(|value| parse_strict(value.as_bytes()).map_err(StateError::from))
            .transpose()?;
        if let Some(checkpoint) = &checkpoint {
            // The canonical formatted string retains the old nanosecond bytes;
            // native cutoff arithmetic still requires a real aware timestamp.
            DateTime::parse_from_rfc3339(&checkpoint.started_at)
                .map_err(|_| StateError::Identity)?;
        }
        Ok(checkpoint)
    }

    pub fn save_observation_checkpoint(
        &mut self,
        checkpoint: Option<&crate::oci::RecipeRunObservationCheckpoint>,
    ) -> Result<(), StateError> {
        match checkpoint {
            Some(checkpoint) => {
                DateTime::parse_from_rfc3339(&checkpoint.started_at)
                    .map_err(|_| StateError::Identity)?;
                let body = canonical_json(checkpoint)?;
                if body.len() > 16 * 1024 {
                    return Err(StateError::Identity);
                }
                let text = std::str::from_utf8(&body).map_err(|_| StateError::Identity)?;
                self.connection.execute(
                    "INSERT INTO metadata(key,value) VALUES ('recipe_observation_scan_v1',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [text],
                )?;
            }
            None => {
                self.connection.execute(
                    "DELETE FROM metadata WHERE key='recipe_observation_scan_v1'",
                    [],
                )?;
            }
        }
        Ok(())
    }

    /// Restore a disposable local journal after proven stored-state damage.
    /// The Controller remains the authority for claims and observes exact
    /// effects before reissuing work. Keep the damaged journal for diagnostics.
    pub fn open_recovered(path: &Path, node_id: &str) -> Result<Self, StateError> {
        // A crash while moving WAL companions must never reopen the original
        // main database without its committed pages. Finish the durable repair
        // intent before opening either the old or the replacement journal.
        finish_pending_state_repair(path)?;
        prune_state_diagnostics(path);
        let opened = Self::open(path, node_id).and_then(|mut state| {
            state.recover_interrupted()?;
            Ok(state)
        });
        let error = match opened {
            Ok(state) => return Ok(state),
            Err(error) => error,
        };
        let repairable = match &error {
            StateError::Identity | StateError::ResultState | StateError::Protocol(_) => true,
            StateError::Database(rusqlite::Error::SqliteFailure(code, _)) => matches!(
                code.code,
                rusqlite::ErrorCode::DatabaseCorrupt
                    | rusqlite::ErrorCode::NotADatabase
                    | rusqlite::ErrorCode::Unknown
            ),
            _ => false,
        };
        if !repairable {
            return Err(error);
        }
        let cause = match &error {
            StateError::Identity => "stored-node-identity-mismatch",
            StateError::ResultState => "stored-operation-unreadable",
            StateError::Protocol(_) => "stored-receipt-invalid",
            _ => "sqlite-file-corrupt",
        };
        // open() refuses links and non-files. Do not turn an unsafe path or
        // an I/O failure into permission to replace somebody else's file.
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
            return Err(std::io::Error::other("state database path is unsafe").into());
        }
        let repair_id = uuid::Uuid::new_v4();
        let marker = repair_marker(path);
        let temporary = path.with_file_name(format!("state.sqlite.repair-{repair_id}.tmp"));
        let mut intent = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        writeln!(intent, "{repair_id}")?;
        intent.sync_all()?;
        fs::rename(&temporary, &marker)?;
        sync_state_directory(path)?;
        finish_pending_state_repair(path)?;
        prune_state_diagnostics(path);
        eprintln!(
            "vonk-agent: state-journal-recreated: cause={cause}; repair={repair_id}; diagnostic-budget-bytes={}",
            crate::inventory::STATE_DATABASE_DISK_RESERVE_BYTES
        );
        Self::open(path, node_id)
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
        if !self.durable_custody {
            return Err(StateError::ResultState);
        }
        if claim.deadline.with_timezone(&Utc) <= now {
            return Err(StateError::Expired);
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing = transaction
            .query_row(
                "SELECT operation, result_json FROM operations WHERE fence=?1",
                [claim.fence.to_string()],
                |row| {
                    Ok(StoredOperation {
                        operation: row.get(0)?,
                        result: row.get(1)?,
                    })
                },
            )
            .optional()?;
        let decision = match existing {
            None => {
                transaction.execute(
                    "INSERT INTO operations(fence,operation,deadline,state)
                     VALUES (?1,?2,?3,'running')",
                    params![
                        claim.fence.to_string(),
                        claim.operation.to_string(),
                        claim.deadline.to_rfc3339(),
                    ],
                )?;
                BeginDecision::Execute
            }
            Some(stored) => {
                // Stored bookkeeping cannot refuse a valid claim or prove that
                // its effect never ran. Reconcile the same fence as unknown
                // when its operation binding or receipt is damaged.
                let result = stored
                    .result
                    .as_deref()
                    .and_then(|bytes| parse_strict::<AgentResult>(bytes).ok())
                    .filter(|result| {
                        stored.operation == claim.operation.as_str()
                            && result.fence == claim.fence
                            && result.validate_for_operation(&claim.operation).is_ok()
                    })
                    .unwrap_or_else(|| uncertain_receipt(claim.fence, &claim.operation));
                BeginDecision::Replay(Box::new(result))
            }
        };
        transaction.commit()?;
        Ok(decision)
    }

    /// Normalize an executor's typed result and make it durable under the claim's
    /// fence. The protocol message is built from the generated contract types
    /// only: there is no loose-JSON body to persist.
    pub fn finish(
        &mut self,
        claim: &AgentClaim,
        executed: ExecutionResult,
    ) -> Result<AgentResult, StateError> {
        let finished = executed.finish(claim);
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let operation: String = transaction
            .query_row(
                "SELECT operation FROM operations
                 WHERE fence=?1 AND state='running'",
                [claim.fence.to_string()],
                |row| row.get(0),
            )
            .optional()?
            .ok_or(StateError::Stale)?;
        if operation != claim.operation.as_str() {
            return Err(StateError::Identity);
        }
        let result = AgentResult {
            fence: claim.fence,
            result: finished.result,
            state: finished.state,
        };
        // The wire schema is the contract: a message that its own generated
        // deserializer refuses (a bound, a pattern, an empty reason) never
        // becomes a durable result.
        let result: AgentResult =
            vonk_agent_protocol::revalidate(&result).map_err(|_| StateError::ResultState)?;
        result.validate_for_operation(&claim.operation)?;
        let body = canonical_json(&result)?;
        let changed = transaction.execute(
            "UPDATE operations SET state='completed',result_json=?2,result_acknowledged=0
             WHERE fence=?1 AND state='running'",
            params![claim.fence.to_string(), body],
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
        if request.fence != directive.fence {
            return Err(StateError::Stale);
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        // The client verified correlation and the renewal owner selected this
        // directive. Local deadline bytes are disposable bookkeeping, never a
        // second authority gate. Overwrite under the exact running fence even
        // when the previous projection is unreadable or spuriously newer.
        let changed = transaction.execute(
            "UPDATE operations SET deadline=?2 WHERE fence=?1 AND state='running'",
            params![request.fence.to_string(), directive.deadline.to_rfc3339()],
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
                 WHERE fence=?1",
                [result.fence.to_string()],
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
            .optional();
        let row = match row {
            Ok(row) => row,
            Err(
                rusqlite::Error::IntegralValueOutOfRange(..)
                | rusqlite::Error::FromSqlConversionFailure(..)
                | rusqlite::Error::InvalidColumnType(..),
            ) => {
                let _ = self.connection.execute(
                    "DELETE FROM result_rejections WHERE fence=?1",
                    [result.fence.to_string()],
                );
                return Ok(None);
            }
            Err(error) => {
                eprintln!("vonk-agent: suppression observation deferred: {error}");
                return Ok(None);
            }
        };
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
        let parsed = (|| -> Result<ResultRejection, StateError> {
            Ok(ResultRejection {
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
            })
        })();
        match parsed {
            Ok(rejection) if now >= rejection.observed_at + chrono::Duration::seconds(900) => {
                // This delivery attempt is over. Retain effect custody, but
                // never resend these incompatible bytes or hold a new fence.
                Ok(Some(rejection))
            }
            Ok(rejection) if rejection.retry_due_at > now && rejection.observed_at <= now => {
                Ok(Some(rejection))
            }
            Ok(rejection) if rejection.retry_due_at <= now => Ok(None),
            _ => {
                let _ = self.connection.execute(
                    "DELETE FROM result_rejections WHERE fence=?1",
                    [result.fence.to_string()],
                );
                Ok(None)
            }
        }
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
        let previous: Option<(u32, String)> = self
            .connection
            .query_row(
                "SELECT rejections,observed_at FROM result_rejections WHERE fence=?1",
                [result.fence.to_string()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let observed_at = previous
            .as_ref()
            .and_then(|(_, value)| DateTime::parse_from_rfc3339(value).ok())
            .map_or(now, |value| value.with_timezone(&Utc));
        let rejections = previous.map_or(0, |(count, _)| count).saturating_add(1);
        // Bounded exponential cool-down: long enough that the loop never
        // hot-loops the same rejected bytes, short enough that a corrected
        // Controller rule reconciles the retained evidence promptly.
        let delay = backoff_delay(rejections, u64::from(now.timestamp_subsec_nanos()), 15, 900);
        let retry_due_at = (now
            + chrono::Duration::from_std(delay).map_err(|_| StateError::ResultState)?)
        .min(observed_at + chrono::Duration::seconds(900));
        // Keep the Controller's own bounded validation digest: it names the
        // failing boundary, field and rule, which is what makes the refusal
        // actionable.  Status, code and request id have their own columns.
        let reason: String = error.rejection_context();
        self.connection.execute(
            "INSERT INTO result_rejections(
               fence,http_status,code,decision,request_id,reason,
               observed_at,retry_due_at,rejections)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)
             ON CONFLICT(fence) DO UPDATE SET
               http_status=excluded.http_status,
               code=excluded.code,
               decision=excluded.decision,
               request_id=excluded.request_id,
               reason=excluded.reason,
               observed_at=result_rejections.observed_at,
               retry_due_at=excluded.retry_due_at,
               rejections=excluded.rejections",
            params![
                result.fence.to_string(),
                error.status,
                error.code,
                error.decision,
                error.request_id,
                reason,
                observed_at.to_rfc3339(),
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
            observed_at,
            retry_due_at,
            rejections,
        })
    }

    /// Drop a refusal once the Controller has accepted or superseded the
    /// result.
    fn clear_result_rejection(&mut self, result: &AgentResult) -> Result<(), StateError> {
        self.connection.execute(
            "DELETE FROM result_rejections WHERE fence=?1",
            [result.fence.to_string()],
        )?;
        Ok(())
    }

    /// One byte-bounded page per pass. The cursor advances before upload so
    /// an unavailable prefix cannot monopolize delivery. Damaged bytes remain
    /// in custody; only a typed unknown is offered for the exact valid fence.
    pub fn next_delivery_result(
        &mut self,
    ) -> Result<Option<(AgentOperation, AgentResult, bool)>, StateError> {
        if !self.durable_custody {
            return Ok(None);
        }
        const RECEIPT_READ_BYTES: i64 =
            vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES as i64;
        let row = self.connection.query_row(
            "SELECT o.rowid,o.fence,o.operation,CASE WHEN length(o.result_json)<=?2 THEN o.result_json END,o.result_acknowledged
             FROM operations o LEFT JOIN result_reconciliation r ON r.fence=o.fence
             WHERE o.state=?3 AND (o.result_acknowledged=0 OR r.fence IS NULL)
             AND o.rowid>?1 ORDER BY o.rowid LIMIT 1",
            params![self.delivery_cursor, RECEIPT_READ_BYTES, ProgressPhase::Completed.as_str()],
            |row| {
                let cursor = row.get::<_, i64>(0)?;
                let value = (|| -> rusqlite::Result<_> {
                    let bytes = row.get::<_, Option<Vec<u8>>>(3).unwrap_or_else(|error| {
                        eprintln!("vonk-agent: receipt bytes unavailable: {error}");
                        None
                    });
                    let acknowledged = row.get::<_, bool>(4).unwrap_or_else(|error| {
                        eprintln!("vonk-agent: acknowledgement projection unavailable: {error}");
                        false
                    });
                    Ok((row.get::<_, String>(1)?, row.get::<_, String>(2)?, bytes, acknowledged))
                })();
                Ok((cursor, value))
            },
        ).optional()?;
        let Some((cursor, value)) = row else {
            self.delivery_cursor = 0;
            let _ = self.connection.execute(
                "DELETE FROM metadata WHERE key='receipt_delivery_cursor'",
                [],
            );
            return Ok(None);
        };
        self.delivery_cursor = cursor;
        let _ = self.connection.execute(
            "INSERT INTO metadata(key,value) VALUES ('receipt_delivery_cursor',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [cursor.to_string()],
        );
        let (fence, operation, bytes, acknowledged) = match value {
            Ok(value) => value,
            Err(error) => {
                eprintln!("vonk-agent: damaged receipt row observation deferred: {error}");
                return Ok(None);
            }
        };
        let Ok(operation) = operation.parse::<AgentOperation>() else {
            return Ok(None);
        };
        let Ok(fence) = fence.parse() else {
            return Ok(None);
        };
        let result = bytes
            .as_deref()
            .and_then(|bytes| parse_strict::<AgentResult>(bytes).ok())
            .filter(|result| {
                result.fence == fence && result.validate_for_operation(&operation).is_ok()
            })
            .unwrap_or_else(|| uncertain_receipt(fence, &operation));
        Ok(Some((operation, result, acknowledged)))
    }

    pub fn pending_results(&self) -> Result<Vec<(AgentOperation, AgentResult)>, StateError> {
        let mut statement = self.connection.prepare(
            "SELECT operation,result_json FROM operations
             WHERE state='completed' AND result_acknowledged=0 ORDER BY rowid",
        )?;
        let mut rows = statement.query([])?;
        let mut results = Vec::new();
        let deadline = std::time::Instant::now() + crate::client::HEARTBEAT_REQUEST_TIMEOUT;
        while std::time::Instant::now() < deadline {
            let Some(row) = rows.next()? else {
                break;
            };
            let parsed = (|| -> Result<_, StateError> {
                let operation: String = row.get(0)?;
                let bytes: Vec<u8> = row.get(1)?;
                let operation = operation.parse().map_err(|_| StateError::ResultState)?;
                let result: AgentResult = parse_strict(&bytes)?;
                result.validate_for_operation(&operation)?;
                Ok((operation, result))
            })();
            if let Ok(result) = parsed {
                results.push(result);
            }
        }
        Ok(results)
    }

    /// Results acknowledged by older agents may include a refused 409. Offer
    /// each retained receipt once to the Controller's diagnostic-only path.
    /// The Controller deduplicates an outcome it already accepted and retains
    /// an expired exact-fence outcome without applying it to live workload state.
    pub fn unreconciled_results(&self) -> Result<Vec<(AgentOperation, AgentResult)>, StateError> {
        let mut statement = self.connection.prepare(
            "SELECT o.operation,o.result_json FROM operations o
             LEFT JOIN result_reconciliation r ON r.fence=o.fence
             WHERE o.state='completed' AND o.result_acknowledged=1
               AND r.fence IS NULL ORDER BY o.rowid LIMIT 16",
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
            "INSERT OR IGNORE INTO result_reconciliation(fence)
             SELECT fence FROM operations
             WHERE fence=?1 AND state='completed' AND result_acknowledged=1",
            [result.fence.to_string()],
        )?;
        Ok(())
    }

    pub fn acknowledge(&mut self, result: &AgentResult) -> Result<(), StateError> {
        result.validate()?;
        let changed = self.connection.execute(
            "UPDATE operations SET result_acknowledged=1
             WHERE fence=?1 AND state='completed'",
            [result.fence.to_string()],
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
             WHERE fence=?1 AND state='completed'",
            [result.fence.to_string()],
        )?;
        if changed != 1 {
            return Err(StateError::Stale);
        }
        self.clear_result_rejection(result)?;
        Ok(())
    }

    pub fn recover_interrupted(&mut self) -> Result<(), StateError> {
        let claims = {
            let mut statement = self
                .connection
                .prepare("SELECT fence,operation FROM operations WHERE state='running'")?;
            statement
                .query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                })?
                .filter_map(Result::ok)
                .collect::<Vec<_>>()
        };
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        for (fence, operation) in claims {
            let Ok(operation) = operation.parse::<AgentOperation>() else {
                continue;
            };
            // Startup cannot establish whether the host action finished.
            // Preserve that uncertainty as a typed unknown outcome: the
            // Controller owns any new attempt and its current intent/authority
            // checks.
            let Ok(parsed_fence) = fence.parse::<uuid::Uuid>() else {
                continue;
            };
            let finished = ExecutionResult::unknown(
                WaitReason::AgentRestartInterrupted,
                "agent restarted with an operation in progress",
                UnknownEvidence::at(FailureStage::AgentRestart)
                    .because("the agent restarted before the operation's result was recorded"),
            )
            .finish_for(&operation);
            let result = AgentResult {
                fence: parsed_fence,
                result: finished.result,
                state: finished.state,
            };
            result.validate_for_operation(&operation)?;
            transaction.execute(
                "UPDATE operations SET state='completed',result_json=?2,result_acknowledged=0
                 WHERE fence=?1 AND state='running'",
                params![fence, canonical_json(&result)?],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }
}

fn uncertain_receipt(fence: uuid::Uuid, operation: &AgentOperation) -> AgentResult {
    let finished = ExecutionResult::unknown(
        WaitReason::AgentRestartInterrupted,
        "stored effect receipt is unavailable",
        UnknownEvidence::at(FailureStage::AgentRestart),
    )
    .finish_for(operation);
    AgentResult {
        fence,
        result: finished.result,
        state: finished.state,
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

fn repair_marker(path: &Path) -> PathBuf {
    path.with_file_name("state.sqlite.repair-pending")
}

fn sync_state_directory(path: &Path) -> Result<(), StateError> {
    if let Some(parent) = path.parent() {
        fs::File::open(parent)?.sync_all()?;
    }
    Ok(())
}

fn finish_pending_state_repair(path: &Path) -> Result<(), StateError> {
    let marker = repair_marker(path);
    match fs::symlink_metadata(&marker) {
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    }
    // Intent is disposable, even if malformed or its old destination collides.
    // Never reopen the remaining database: an earlier repair may have moved
    // its WAL already. Retain all remaining entries under a fresh identity,
    // moving the main database first and retiring the marker only after fsync.
    // Rename does not follow symlinks or traverse an unexpected directory.
    let id = uuid::Uuid::new_v4();
    let quarantine = path.with_file_name(format!("state.sqlite.corrupt-{id}"));
    for suffix in ["", "-wal", "-shm"] {
        let source = PathBuf::from(format!("{}{suffix}", path.display()));
        let destination = PathBuf::from(format!("{}{suffix}", quarantine.display()));
        match fs::symlink_metadata(&source) {
            Ok(_) => fs::rename(source, destination)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    sync_state_directory(path)?;
    fs::rename(
        marker,
        path.with_file_name(format!("state.sqlite.repair-{id}.tmp")),
    )?;
    sync_state_directory(path)?;
    Ok(())
}

/// Diagnostic journals share the existing state-database disk budget. Charge
/// at least one allocation unit per entry so zero-length crash leftovers cannot
/// grow without consuming the bound. Only our UUID-named inactive files qualify.
fn prune_state_diagnostics(path: &Path) {
    fn prune(path: &Path) -> std::io::Result<()> {
        let Some(parent) = path.parent() else {
            return Ok(());
        };
        let mut files = Vec::new();
        let mut bytes = 0_u64;
        for entry in fs::read_dir(parent)? {
            let entry = entry?;
            let name = entry.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            let identity = if let Some(raw) = name.strip_prefix("state.sqlite.corrupt-") {
                raw.strip_suffix("-wal")
                    .or_else(|| raw.strip_suffix("-shm"))
                    .unwrap_or(raw)
            } else if let Some(raw) = name.strip_prefix("state.sqlite.repair-") {
                let Some(raw) = raw.strip_suffix(".tmp") else {
                    continue;
                };
                raw
            } else {
                continue;
            };
            if uuid::Uuid::parse_str(identity).is_err() {
                continue;
            }
            let metadata = fs::symlink_metadata(entry.path())?;
            if !metadata.file_type().is_file() {
                continue;
            }
            let cost = metadata.len().max(4096);
            bytes = bytes.saturating_add(cost);
            files.push((metadata.modified()?, entry.path(), cost));
        }
        files.sort();
        for (_, file, cost) in files {
            if bytes <= crate::inventory::STATE_DATABASE_DISK_RESERVE_BYTES {
                break;
            }
            fs::remove_file(file)?;
            bytes = bytes.saturating_sub(cost);
        }
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    }
    if let Err(error) = prune(path) {
        eprintln!("vonk-agent: state-diagnostic-retention-deferred: {error}");
    }
}
