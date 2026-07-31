"""FastAPI backend for the React chat interface."""

import json
import logging
import mimetypes
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated, Callable, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Match

from agent import detect_guardrail_intervention, query, stream_query
import access_control
import comparison_detection_worker
import comparison_detector
import comparison_export
import comparison_governance
import comparison_reliability
import comparison_review
import comparison_store
import config
import detection_job_lease
import detection_job_retry
import detection_recovery
import runtime_readiness
from governance import review_queue
from loaders.registry import supported_extensions

logger = logging.getLogger("api")

# The only error contract chat clients ever see. Raw exception text must never
# leave the server: provider errors, OSError strings, and library messages can
# carry absolute paths, request contents, and configuration fragments. The
# error_id is a fresh opaque correlation id logged with the server-side
# traceback — NOT an audit_id, because when a request fails no audit record
# was written and claiming one would be false.
SAFE_ERROR_CODE = "internal_error"
SAFE_ERROR_MESSAGE = "The request could not be completed. Please try again."


def _new_error_id() -> str:
    return f"err_{uuid.uuid4().hex[:12]}"


AUTHENTICATION_REQUIRED = "authentication_required"
INSUFFICIENT_PERMISSION = "insufficient_permission"
# Detection only, never parsing or authorization: locally issued PyJWT compact
# tokens use a JSON header (base64url starts with ``eyJ``) and an HS256
# signature. Protected inputs containing a complete compact token are refused
# before domain code can persist it or interpolate it into an exception.
_COMPACT_ACCESS_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])"
    + "ey"
    + r"J[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{32,}"
    r"(?![A-Za-z0-9_-])"
)
_BEARER = HTTPBearer(
    auto_error=False,
    bearerFormat="JWT",
    description="Local signed comparison-workflow access token.",
)


def _safe_auth_error(
    *,
    status_code: int,
    code: str,
    message: str,
    request: Request,
    required_permission: str | None = None,
    principal: access_control.Principal | None = None,
    route_template: str | None = None,
) -> HTTPException:
    """Build and safely log a stable authentication/authorization refusal.

    The Authorization header and bearer value are deliberately unreachable
    from the log payload. Subject and JTI are included only after successful
    verification produced an immutable Principal.
    """
    error_id = _new_error_id()
    resolved_route = route_template
    if resolved_route is None:
        resolved_route = getattr(request.scope.get("route"), "path", None)
    if not isinstance(resolved_route, str):
        resolved_route = "unmatched_route"
    safe_extra = {
        "event": (
            "authorization_rejected"
            if status_code == 403
            else "authentication_rejected"
        ),
        "error_id": error_id,
        "route": resolved_route,
        "required_permission": required_permission,
        "actor_subject": principal.subject if principal else None,
        "actor_token_id": principal.token_id if principal else None,
    }
    logger.warning(
        "%s error_id=%s route=%s",
        safe_extra["event"],
        error_id,
        resolved_route,
        extra=safe_extra,
    )
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "error_id": error_id},
        headers=headers,
    )


def require_principal(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_BEARER)
    ],
) -> access_control.Principal:
    """Resolve one verified Principal from an Authorization: Bearer header."""
    preverified = getattr(request.state, "principal", None)
    if isinstance(preverified, access_control.Principal):
        return preverified
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _safe_auth_error(
            status_code=401,
            code=AUTHENTICATION_REQUIRED,
            message="A valid bearer access token is required.",
            request=request,
            required_permission=getattr(
                request.state, "required_permission", None
            ),
            route_template=getattr(
                request.state, "comparison_route_template", None
            ),
        )
    try:
        return request.app.state.authenticator.verify(credentials.credentials)
    except access_control.AccessTokenError as exc:
        raise _safe_auth_error(
            status_code=401,
            code=exc.code,
            message=exc.public_message,
            request=request,
            required_permission=getattr(
                request.state, "required_permission", None
            ),
            route_template=getattr(
                request.state, "comparison_route_template", None
            ),
        ) from exc


def require_permission(
    permission: str,
) -> Callable[..., access_control.Principal]:
    """Create an explicit exact-permission dependency for one route."""
    if permission not in access_control.DEFINED_PERMISSIONS:
        raise access_control.AccessControlConfigError(
            f"access_control policy: unknown required permission {permission!r}."
        )

    def dependency(
        request: Request,
        principal: Annotated[
            access_control.Principal, Depends(require_principal)
        ],
    ) -> access_control.Principal:
        if permission not in principal.permissions:
            raise _safe_auth_error(
                status_code=403,
                code=INSUFFICIENT_PERMISSION,
                message="The authenticated principal lacks the required permission.",
                request=request,
                required_permission=permission,
                principal=principal,
            )
        return principal

    dependency.__name__ = f"require_{permission.replace('.', '_')}"
    setattr(dependency, "required_permission", permission)
    return dependency


def _actor_context(
    principal: access_control.Principal, required_permission: str
) -> dict[str, str]:
    """Allowlisted Principal-derived context for persistence/logging seams."""
    return {
        "actor_subject": principal.subject,
        "actor_auth_method": principal.auth_method,
        "actor_token_id": principal.token_id,
        "required_permission": required_permission,
    }


def _authenticated_actor(
    supplied: str | None,
    principal: access_control.Principal,
    *,
    field_name: str,
) -> str:
    """Honor a deprecated actor field only when it exactly matches the token."""
    if supplied is not None and supplied != principal.subject:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "actor_identity_mismatch",
                "message": (
                    f"{field_name} is deprecated and, when supplied, must "
                    "exactly match the authenticated subject."
                ),
            },
        )
    return principal.subject


MAX_HISTORY_TURNS = 4
DOCS_DIR = Path(__file__).resolve().parent / "docs"

# Single source of truth: whatever the ingestion registry supports is also
# what the document sidebar / file-server endpoint exposes. Adding a new
# format never requires touching this file again.
SUPPORTED_DOC_SUFFIXES = set(supported_extensions())

# Explicit MIME types where the stdlib mimetypes module is ambiguous or
# under-specified for finance/compliance use; anything else falls back to
# ``mimetypes.guess_type`` at serve time.
MEDIA_TYPES = {
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
}


class ChatMessage(BaseModel):
    """A single chat message exchanged with the frontend."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """Request body for a chat turn."""

    message: str = Field(min_length=1)
    history: list[ChatMessage] = Field(default_factory=list)


class RetrievedSource(BaseModel):
    """Retrieved chunk metadata exposed for auditability in the UI.

    Mirrors the review API's SafeReviewSource: corpus-relative fields only.
    The agent's source dicts also carry `source` (a local absolute path);
    _to_chat_source drops it before anything reaches the response.
    """

    rank: int
    source_name: str
    source_path: str
    section_title: str | None = None
    page: int | None = None
    excerpt: str


class ChatResponse(BaseModel):
    """Response body for a chat turn."""

    answer: str
    sources: list[RetrievedSource] = Field(default_factory=list)
    audit_id: str | None = None
    governance_report: dict | None = None


class CorpusDocument(BaseModel):
    """A source document available for review in the frontend."""

    name: str
    path: str
    file_type: str
    url: str


class SafeReviewSource(BaseModel):
    """A retrieved source from a review item, restricted to corpus-relative fields.

    The stored queue source also carries `source` (a local absolute path); that
    field is dropped by the mapper and has no counterpart here.
    """

    rank: int | None = None
    sourceName: str | None = None
    sourcePath: str | None = None
    sectionTitle: str | None = None
    page: int | None = None
    excerpt: str | None = None
    documentUrl: str | None = None


class ReviewSummary(BaseModel):
    """Queue listing entry for the reviewer UI. The field set is an allowlist."""

    reviewId: str
    question: str
    riskScore: float
    riskLevel: str
    riskReasons: list[str] = Field(default_factory=list)
    reviewStatus: Literal["pending", "approved", "rejected"]
    createdAt: str
    reviewedAt: str | None = None
    wasWithheld: bool | None = None


class ReviewDetail(ReviewSummary):
    """Full review item: summary fields plus the draft, evidence, and audit join."""

    auditId: str | None = None
    draftAnswer: str
    retrievedSources: list[SafeReviewSource] = Field(default_factory=list)
    decision: str | None = None
    reviewerNote: str | None = None
    governanceReport: dict | None = None


class ReviewActionRequest(BaseModel):
    """Body for the approve/reject review actions."""

    note: str | None = None


class ComparisonCreateRequest(BaseModel):
    """Body for creating a comparison. Identity fields only.

    The client may name the two filings and the section scope — nothing else.
    Company, form type, dates, source names, and hashes are resolved
    server-side from the filing registry, never accepted from the client.
    """

    previousFilingId: str = Field(min_length=1)
    currentFilingId: str = Field(min_length=1)
    sectionScope: list[str] | None = None


class ComparisonRecordDTO(BaseModel):
    """A persisted comparison entity. The field set is an allowlist.

    Pre-detection on purpose: there are no change/evidence/validation/risk/
    review fields because no detector has run — this is a ComparisonRecord,
    not a comparison.v1 ComparisonResult.
    """

    comparisonId: str
    schemaVersion: str
    workflowVersion: str
    previousFilingId: str
    currentFilingId: str
    sectionScope: list[str]
    status: Literal[
        "ready_for_detection",
        "queued_for_detection",
        "detecting",
        "waiting_for_detection_retry",
        "detected",
        "failed",
    ]
    createdAt: str
    updatedAt: str
    failureCode: str | None = None
    failureSummary: str | None = None


class ComparisonCreateResponse(BaseModel):
    """POST /api/comparisons response: the entity plus idempotency outcome."""

    created: bool
    comparison: ComparisonRecordDTO


class GovernanceEvaluationDTO(BaseModel):
    """An immutable comparison governance evaluation. Allowlisted fields.

    ``governedResult`` is the validated comparison.v1 snapshot (risk/review
    updated, everything else preserved from the detector result) — the same
    wire contract the result endpoint uses. No storage paths, no SQL detail.
    """

    evaluationId: str
    comparisonId: str
    policyId: str
    policyVersion: str
    riskScore: float
    riskLevel: str
    decision: str
    reasonCodes: list[str]
    evaluatedAt: str
    comparisonResultHash: str
    governedResultHash: str
    governedResult: dict


class GovernanceEvaluateResponse(BaseModel):
    """POST governance response: the evaluation plus idempotency outcome."""

    created: bool
    evaluation: GovernanceEvaluationDTO


class ComparisonReviewSummaryDTO(BaseModel):
    """Pending comparison-review summary. Deliberately excerpt-free: the
    governed result (with evidence) is referenced by hash and served by the
    governance endpoint, never copied into list rows."""

    reviewId: str
    comparisonId: str
    evaluationId: str
    status: Literal["pending"]
    riskScore: float
    riskLevel: str
    reasonCodes: list[str]
    createdAt: str


class ComparisonReviewEditRequest(BaseModel):
    """One requested summary edit, addressed by change_id. Nothing else about
    a change is editable."""

    changeId: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class ComparisonReviewDecisionRequest(BaseModel):
    """Body for the terminal comparison-review decision.

    ``reviewerId`` is retained only as a deprecated compatibility field. When
    present it must exactly match the authenticated Principal; it is never the
    identity source. ``reasonCode`` must come from the action's allowlist and
    ``reviewerNote`` remains required bounded prose.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approved", "rejected"]
    reviewerId: str | None = Field(
        default=None, min_length=1, max_length=120, deprecated=True
    )
    reasonCode: str = Field(min_length=1)
    reviewerNote: str = Field(min_length=1)
    edits: list[ComparisonReviewEditRequest] | None = None


class ComparisonReviewEventDTO(BaseModel):
    """One append-only review decision event. Allowlisted fields."""

    eventId: str
    reviewId: str
    comparisonId: str
    evaluationId: str
    action: Literal["approved", "rejected"]
    reviewerId: str
    reviewerIdBasis: Literal["legacy_self_asserted", "local_hs256"]
    reasonCode: str
    reviewerNote: str
    originalGovernedResultHash: str
    finalReviewedResultHash: str
    editedChangeIds: list[str]
    createdAt: str


class ComparisonReviewDecisionDTO(ComparisonReviewEventDTO):
    """The terminal decision with the complete final reviewed comparison.v1
    snapshot (the sanctioned wire contract, as with governed results)."""

    reviewedResult: dict


class ComparisonReviewDecisionResponse(BaseModel):
    created: bool
    decision: ComparisonReviewDecisionDTO


class ComparisonReviewDetailDTO(BaseModel):
    """Full review item: linkage, status, governance metadata, the governed
    result, and the terminal decision when one exists."""

    reviewId: str
    comparisonId: str
    evaluationId: str
    status: Literal["pending", "approved", "rejected"]
    riskScore: float
    riskLevel: str
    reasonCodes: list[str]
    comparisonResultHash: str
    governedResultHash: str
    createdAt: str
    decidedAt: str | None = None
    governedResult: dict
    decision: ComparisonReviewDecisionDTO | None = None


class ComparisonExportCreateRequest(BaseModel):
    """Body for creating an export. The evaluation id is the ONLY input.

    Eligibility is resolved server-side from persisted records: the client
    cannot submit a decision, review status, reviewer identity, result JSON,
    result hashes, policy identity, or release basis — unknown fields are
    rejected (422), not ignored.
    """

    model_config = ConfigDict(extra="forbid")

    evaluationId: str = Field(min_length=1)


class ComparisonExportSummaryDTO(BaseModel):
    """One export row for list views. The field set is an allowlist:
    identity, linkage, basis, and hashes only — no comparison evidence, no
    exported payload, no reviewer note."""

    exportId: str
    exportSchemaVersion: str
    comparisonId: str
    evaluationId: str
    reviewId: str | None = None
    releaseBasis: Literal[
        "returned_by_policy",
        "returned_with_warning_by_policy",
        "approved_after_review",
    ]
    sourceResultHash: str
    finalResultHash: str
    exportPayloadHash: str
    createdAt: str


class ComparisonExportCreateResponse(BaseModel):
    """POST exports response: the persisted comparison.export.v1 document
    (verbatim, as with detection results) plus the idempotency outcome."""

    created: bool
    export: dict


class DetectionAttemptDTO(BaseModel):
    """One durable detection execution. The field set is an allowlist.

    Deliberately payload-free: no comparison result, no evidence excerpts, no
    filing content. ``failureSummary`` is the store's bounded, code-derived
    summary — never an exception message, path, or SQL fragment.
    """

    attemptId: str
    comparisonId: str
    attemptNumber: int
    status: Literal["running", "succeeded", "failed", "timed_out"]
    detectorVersion: str
    workflowVersion: str
    previousSourceHash: str
    currentSourceHash: str
    startedAt: str
    finishedAt: str | None = None
    resultHash: str | None = None
    failureCode: str | None = None
    failureSummary: str | None = None


class DetectionEventDTO(BaseModel):
    """One append-only detection transition event. Allowlisted fields.

    Application records, NOT tamper-proof storage. Carries a stable event type
    and at most a result hash or failure code — never result JSON, evidence,
    reviewer notes, paths, SQL, or raw error text.
    """

    eventId: str
    attemptId: str
    comparisonId: str
    eventType: Literal[
        "detection_started",
        "detection_succeeded",
        "detection_failed",
        "detection_timed_out",
    ]
    eventSeq: int
    createdAt: str
    resultHash: str | None = None
    failureCode: str | None = None


class DetectionJobDTO(BaseModel):
    """Read-only detection-job allowlist; no claim or request hashes."""

    model_config = ConfigDict(extra="forbid")

    jobId: str
    comparisonId: str
    attemptId: str | None = None
    triggerType: Literal["initial_detection"]
    status: Literal["queued", "running", "retry_wait", "succeeded", "failed"]
    detectorVersion: str
    workflowVersion: str
    requestedBySubject: str
    requestedByAuthMethod: Literal["local_hs256"]
    queuedAt: str
    claimedAt: str | None = None
    finishedAt: str | None = None
    workerId: str | None = None
    claimGeneration: int
    leaseStartedAt: str | None = None
    heartbeatAt: str | None = None
    leaseExpiresAt: str | None = None
    leaseState: Literal["not_claimed", "active", "expired", "terminal"]
    resultHash: str | None = None
    failureCode: str | None = None
    retryCount: int
    maxRetryAttempts: int
    nextAttemptAt: str | None = None
    lastFailureCode: str | None = None
    lastFailureClassification: str | None = None
    retryState: Literal[
        "not_applicable", "waiting", "due", "exhausted", "terminal"
    ]


class DetectionJobEventDTO(BaseModel):
    """Insert-only job transition allowlist."""

    model_config = ConfigDict(extra="forbid")

    eventId: str
    jobId: str
    comparisonId: str
    attemptId: str | None = None
    eventType: Literal[
        "detection_job_queued",
        "detection_job_claimed",
        "detection_job_heartbeat",
        "detection_job_reclaimed",
        "detection_job_claim_exhausted",
        "detection_job_retry_scheduled",
        "detection_job_retry_claimed",
        "detection_job_retry_exhausted",
        "detection_job_succeeded",
        "detection_job_failed",
    ]
    eventSeq: int
    createdAt: str
    workerId: str | None = None
    claimGeneration: int
    sourceAttemptId: str | None = None
    replacementAttemptId: str | None = None
    leaseExpiresAt: str | None = None
    resultHash: str | None = None
    failureCode: str | None = None
    retryCount: int = 0
    failureClassification: str | None = None
    nextAttemptAt: str | None = None


class DetectionRecoveryDTO(BaseModel):
    """READ-ONLY recovery assessment for one detection attempt.

    Reports whether the attempt has crossed the configured stale threshold and
    whether a replay would currently be accepted. Reading it never mutates
    state: an attempt is retired only by an explicit replay request.
    ``blockingReason`` uses the same stable codes a replay would return.
    """

    attemptId: str
    comparisonId: str
    status: Literal["running", "succeeded", "failed", "timed_out"]
    startedAt: str
    staleAt: str
    ageSeconds: float
    isStale: bool
    replayEligible: bool
    attemptsUsed: int
    maxAttempts: int
    remainingAttempts: int
    policyId: str
    policyVersion: str
    blockingReason: str | None = None


class DetectionReplayRequest(BaseModel):
    """Body for an authenticated operator-requested replay.

    ``operatorId`` is retained only as a deprecated compatibility field. When
    present it must exactly match the authenticated Principal and is never the
    identity source. The client cannot submit staleness, policy identity,
    attempt state, or the replacement attempt id.
    """

    model_config = ConfigDict(extra="forbid")

    operatorId: str | None = Field(
        default=None, min_length=1, max_length=120, deprecated=True
    )
    reasonCode: str = Field(min_length=1)
    operatorNote: str = Field(min_length=1)


class DetectionReplayDTO(BaseModel):
    """One operator replay linking a retired attempt to its replacement.

    Allowlisted fields only — no raw error text, evidence, paths, SQL, or
    environment values. New API-created rows carry authenticated actor
    provenance; historical rows remain visibly legacy self-asserted.
    Application records are still NOT tamper-proof storage.
    """

    replayId: str
    comparisonId: str
    sourceAttemptId: str
    replacementAttemptId: str
    operatorId: str
    operatorIdBasis: Literal["legacy_self_asserted", "local_hs256"]
    reasonCode: str
    operatorNote: str
    policyId: str
    policyVersion: str
    requestedAt: str


class DetectionReplayResponse(BaseModel):
    """POST replay response: the linkage, the replacement's terminal status,
    and the comparison result when it succeeded (the existing comparison.v1
    wire shape — recovery metadata is never inserted into that document)."""

    created: bool
    replay: DetectionReplayDTO
    sourceAttemptId: str
    replacementAttemptId: str
    replacementStatus: Literal["running", "succeeded", "failed", "timed_out"]
    result: dict | None = None


class ReliabilityRateDTO(BaseModel):
    """One rate with its denominator visible.

    A zero denominator asserts nothing, so ``value`` is null with
    ``zeroDenominator`` true — never NaN, never 0.0, never omitted.
    """

    metric: str
    value: float | None = None
    numerator: int
    denominator: int
    zeroDenominator: bool
    zeroDenominatorPolicy: str | None = None


class ReliabilityGaugesDTO(BaseModel):
    """Current state at query time. NOT restricted by the historical window."""

    comparisonsReadyForDetection: int
    comparisonsQueuedForDetection: int
    comparisonsDetecting: int
    comparisonsWaitingForDetectionRetry: int
    comparisonsDetected: int
    comparisonsFailed: int
    runningAttempts: int
    staleRunningAttempts: int
    replayEligibleAttempts: int
    attemptLimitExhaustedComparisons: int
    detectionJobsQueued: int
    detectionJobsRunning: int
    detectionJobsWaitingForRetry: int
    detectionJobsSucceeded: int
    detectionJobsFailed: int
    activeJobLeases: int
    expiredJobLeases: int
    reclaimableJobs: int
    claimExhaustedJobs: int
    detectionJobsRetryDue: int
    detectionJobsRetryNotDue: int
    detectionJobsRetryExhausted: int
    unresolvedOperationalIssues: int


class ReliabilityJobCountersDTO(BaseModel):
    jobsQueued: int
    jobsClaimed: int
    jobsSucceeded: int
    jobsFailed: int
    jobHeartbeats: int
    jobsReclaimed: int
    jobsClaimExhausted: int
    retriesScheduled: int
    retriesClaimed: int
    retriesSucceeded: int
    retriesFailed: int
    retriesExhausted: int


class ReliabilityJobDurationsDTO(BaseModel):
    queueWaitCount: int
    queueWaitSecondsMin: float | None = None
    queueWaitSecondsMax: float | None = None
    queueWaitSecondsMean: float | None = None
    queueWaitSecondsP50: float | None = None
    queueWaitSecondsP95: float | None = None
    executionCount: int
    executionSecondsMin: float | None = None
    executionSecondsMax: float | None = None
    executionSecondsMean: float | None = None
    executionSecondsP50: float | None = None
    executionSecondsP95: float | None = None
    negativeQueueWaitJobs: int
    negativeExecutionJobs: int
    negativeLeaseDurationJobs: int
    percentileMethod: str


class ReliabilityAttemptCountersDTO(BaseModel):
    """Historical attempt counters, windowed on ``started_at``.

    ``terminalAttempts`` excludes running attempts, which is the denominator
    every attempt rate uses; ``attemptsRunningInWindow`` is reported separately
    so ``attemptsStarted`` reconciles.
    """

    attemptsStarted: int
    attemptsSucceeded: int
    attemptsFailed: int
    attemptsTimedOut: int
    attemptsRunningInWindow: int
    terminalAttempts: int


class ReliabilityAttemptRatesDTO(BaseModel):
    successRate: ReliabilityRateDTO
    failureRate: ReliabilityRateDTO
    timeoutRate: ReliabilityRateDTO


class ReliabilityReplayCountersDTO(BaseModel):
    """Historical replay counters, windowed on ``requested_at``. A running
    replacement is not a failure and never enters the terminal denominator."""

    replaysStarted: int
    replayReplacementsSucceeded: int
    replayReplacementsFailed: int
    replayReplacementsRunning: int
    replayReplacementsTimedOut: int
    terminalReplayReplacements: int


class ReliabilityReplayRatesDTO(BaseModel):
    replaySuccessRate: ReliabilityRateDTO


class ReliabilityDurationsDTO(BaseModel):
    """Duration statistics over terminal in-window attempts.

    Duration is ``finished_at - started_at``. Negative durations are excluded
    and counted, never folded into the statistics.
    """

    durationCount: int
    durationSecondsMin: float | None = None
    durationSecondsMax: float | None = None
    durationSecondsMean: float | None = None
    durationSecondsP50: float | None = None
    durationSecondsP95: float | None = None
    negativeDurationAttempts: int
    percentileMethod: str


class ReliabilityFailureBreakdownDTO(BaseModel):
    """Counts keyed by stable failure code and by producing version. The
    version breakdowns count failed AND timed-out attempts."""

    failedAttemptsByCode: dict[str, int]
    timedOutAttemptsByCode: dict[str, int]
    failuresByDetectorVersion: dict[str, int]
    failuresByWorkflowVersion: dict[str, int]
    retryableFailuresByCode: dict[str, int]
    nonRetryableFailuresByCode: dict[str, int]
    retryExhaustionsByOriginalCode: dict[str, int]


class ReliabilitySummaryDTO(BaseModel):
    """Read-only reliability aggregate over persisted workflow records.

    Derived entirely from the local comparison database and the checked-in
    recovery policy. Deliberately payload-free: no comparison result, no
    evidence, no filing content, no reviewer or operator notes, no paths.
    """

    contractVersion: str
    generatedAt: str
    since: str | None = None
    until: str | None = None
    detectorVersions: list[str]
    workflowVersions: list[str]
    recoveryPolicyId: str
    recoveryPolicyVersion: str
    staleAfterSeconds: int
    maxAttemptsPerComparison: int
    leasePolicyId: str
    leasePolicyVersion: str
    leaseDurationSeconds: int
    heartbeatExtensionSeconds: int
    reclaimGraceSeconds: int
    maxClaimGenerations: int
    retryPolicyId: str
    retryPolicyVersion: str
    maxRetryAttempts: int
    gauges: ReliabilityGaugesDTO
    jobs: ReliabilityJobCountersDTO
    jobDurations: ReliabilityJobDurationsDTO
    attempts: ReliabilityAttemptCountersDTO
    attemptRates: ReliabilityAttemptRatesDTO
    replays: ReliabilityReplayCountersDTO
    replayRates: ReliabilityReplayRatesDTO
    durations: ReliabilityDurationsDTO
    failureBreakdown: ReliabilityFailureBreakdownDTO


class ReliabilityIssueDTO(BaseModel):
    """One unresolved operational issue. The field set is a closed allowlist.

    Carries stable codes and identifiers only — no evidence, result JSON,
    filing content, reviewer or operator notes, filesystem paths, SQL, or raw
    exception text. ``recommendedActionCode`` is a machine-readable code, never
    prose instructions.
    """

    issueType: Literal[
        "stale_running_attempt",
        "attempt_limit_exhausted",
        "comparison_failed",
        "replacement_attempt_failed",
        "invalid_negative_duration",
        "invalid_negative_lease_duration",
        "queued_detection_job",
        "expired_detection_job_lease",
        "detection_job_claims_exhausted",
        "detection_job_waiting_for_retry",
        "detection_job_retry_overdue",
        "detection_job_retries_exhausted",
    ]
    comparisonId: str
    jobId: str | None = None
    attemptId: str | None = None
    replayId: str | None = None
    status: str
    failureCode: str | None = None
    startedAt: str | None = None
    queuedAt: str | None = None
    claimedAt: str | None = None
    claimGeneration: int | None = None
    leaseStartedAt: str | None = None
    heartbeatAt: str | None = None
    leaseExpiresAt: str | None = None
    leaseState: Literal["not_claimed", "active", "expired", "terminal"] | None = None
    createdAt: str | None = None
    detectedAt: str
    ageSeconds: float | None = None
    staleAt: str | None = None
    attemptsUsed: int
    maxAttempts: int
    detectorVersion: str | None = None
    workflowVersion: str | None = None
    recommendedActionCode: Literal[
        "inspect_and_replay_if_valid",
        "create_new_workflow_version",
        "inspect_failure",
        "no_replay_available",
        "inspect_clock_integrity",
        "run_one_shot_detection_worker",
        "run_one_shot_worker_to_reclaim",
        "run_one_shot_worker_to_claim_retry",
    ]


class ReliabilityIssuesResponse(BaseModel):
    """Current-state issue listing. There is no time window here on purpose:
    the issue set is what needs attention now, which is also what keeps
    ``unresolvedOperationalIssues`` in the summary equal to ``total``.

    ``total`` counts matches before ``limit`` truncates and ``truncated`` says
    so explicitly — a cap is never silent.
    """

    contractVersion: str
    generatedAt: str
    recoveryPolicyId: str
    recoveryPolicyVersion: str
    leasePolicyId: str
    leasePolicyVersion: str
    retryPolicyId: str
    retryPolicyVersion: str
    total: int
    returned: int
    truncated: bool
    issues: list[ReliabilityIssueDTO]


class ReliabilityFailureDTO(BaseModel):
    """One failed or timed-out attempt summary. Allowlisted fields only.

    ``failureSummary`` is the store's bounded, code-derived string — never an
    exception message, path, or SQL fragment. No comparison result and no
    evidence are ever included.
    """

    attemptId: str
    comparisonId: str
    attemptNumber: int
    status: Literal["failed", "timed_out"]
    failureCode: str | None = None
    failureSummary: str | None = None
    detectorVersion: str
    workflowVersion: str
    startedAt: str
    finishedAt: str | None = None
    durationSeconds: float | None = None
    replayId: str | None = None
    sourceAttemptId: str | None = None


class ReliabilityFailuresResponse(BaseModel):
    """Failure listing, newest first, windowed on ``started_at``."""

    contractVersion: str
    generatedAt: str
    since: str | None = None
    until: str | None = None
    total: int
    returned: int
    truncated: bool
    failures: list[ReliabilityFailureDTO]


class ComparisonDetectResponse(BaseModel):
    """Idempotent already-detected response.

    ``result`` is the validated comparison.v1 wire document exactly as
    persisted (the schema contract IS the API shape for detection results —
    it contains filing ids, section keys, chunk ids, and excerpts, never
    absolute paths or storage detail).

    ``attemptId`` is additive and optional, so the existing wire contract is
    unchanged for current clients: it names the durable execution that produced
    the result. It is deliberately NOT inserted into the comparison.v1 document
    — attempt metadata is workflow bookkeeping, not part of the result schema.
    It is null only for a result stored before attempt tracking existed.
    """

    created: bool
    result: dict
    attemptId: str | None = None


class ComparisonDetectionJobResponse(BaseModel):
    """202 response for a newly queued or equivalent active detection job."""

    model_config = ConfigDict(extra="forbid")

    created: bool
    comparisonId: str
    jobId: str
    jobStatus: Literal["queued", "running", "retry_wait"]
    comparisonStatus: Literal[
        "queued_for_detection", "detecting", "waiting_for_detection_retry"
    ]
    queuedAt: str
    attemptId: str | None = None


COMPARISON_ROUTE_PERMISSION_MATRIX: dict[tuple[str, str], str] = {
    ("POST", "/api/comparisons"): "comparison.create",
    ("GET", "/api/comparisons"): "comparison.read",
    ("GET", "/api/comparisons/{comparison_id}"): "comparison.read",
    (
        "POST",
        "/api/comparisons/{comparison_id}/detect",
    ): "comparison.detect",
    (
        "GET",
        "/api/comparisons/{comparison_id}/detection-attempts",
    ): "detection_attempt.read",
    (
        "GET",
        "/api/comparisons/{comparison_id}/detection-jobs",
    ): "detection_attempt.read",
    (
        "GET",
        "/api/comparison-detection-jobs/{job_id}",
    ): "detection_attempt.read",
    (
        "GET",
        "/api/comparison-detection-jobs/{job_id}/events",
    ): "detection_attempt.read",
    ("GET", "/api/detection-attempts/{attempt_id}"): "detection_attempt.read",
    (
        "GET",
        "/api/detection-attempts/{attempt_id}/events",
    ): "detection_attempt.read",
    (
        "GET",
        "/api/detection-attempts/{attempt_id}/recovery",
    ): "recovery.read",
    (
        "POST",
        "/api/detection-attempts/{attempt_id}/replay",
    ): "recovery.replay",
    (
        "GET",
        "/api/detection-attempts/{attempt_id}/replays",
    ): "recovery.read",
    ("GET", "/api/comparison-reliability/summary"): "reliability.read",
    ("GET", "/api/comparison-reliability/issues"): "reliability.read",
    ("GET", "/api/comparison-reliability/failures"): "reliability.read",
    (
        "POST",
        "/api/comparisons/{comparison_id}/governance",
    ): "governance.evaluate",
    (
        "GET",
        "/api/comparisons/{comparison_id}/governance",
    ): "governance.read",
    ("GET", "/api/comparison-reviews"): "review.read",
    ("GET", "/api/comparison-reviews/{review_id}"): "review.read",
    (
        "POST",
        "/api/comparison-reviews/{review_id}/decision",
    ): "review.decide",
    (
        "GET",
        "/api/comparison-reviews/{review_id}/events",
    ): "review.read",
    ("GET", "/api/comparisons/{comparison_id}/result"): "comparison.read",
    (
        "POST",
        "/api/comparisons/{comparison_id}/exports",
    ): "export.create",
    (
        "GET",
        "/api/comparisons/{comparison_id}/exports",
    ): "export.read",
    ("GET", "/api/comparison-exports/{export_id}"): "export.read",
}


_AUTHENTICATOR = access_control.Authenticator.from_environment()
app = FastAPI(title="Financial Document Intelligence API")
app.state.authenticator = _AUTHENTICATOR

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _comparison_route_requirement(
    request: Request,
) -> tuple[str, str] | None:
    """Resolve a protected request to its route template and permission.

    This runs before routing and body parsing. Only checked-in route templates
    are returned; raw URL paths, query strings, and headers are never retained
    or logged.
    """
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match is not Match.FULL:
            continue
        route_template = getattr(route, "path", None)
        if not isinstance(route_template, str):
            return None
        permission = COMPARISON_ROUTE_PERMISSION_MATRIX.get(
            (request.method.upper(), route_template)
        )
        if permission is None:
            return None
        return route_template, permission
    return None


def _verify_comparison_request(
    request: Request,
    *,
    route_template: str,
    required_permission: str,
) -> access_control.Principal:
    """Authenticate and authorize without exposing the bearer value."""
    authorization_values = request.headers.getlist("authorization")
    if not authorization_values:
        raise _safe_auth_error(
            status_code=401,
            code=AUTHENTICATION_REQUIRED,
            message="A valid bearer access token is required.",
            request=request,
            required_permission=required_permission,
            route_template=route_template,
        )
    if len(authorization_values) != 1:
        raise _safe_auth_error(
            status_code=401,
            code=access_control.INVALID_ACCESS_TOKEN,
            message="The access token is invalid.",
            request=request,
            required_permission=required_permission,
            route_template=route_template,
        )

    scheme, separator, token = authorization_values[0].partition(" ")
    if (
        separator != " "
        or scheme.casefold() != "bearer"
        or not token
        or token != token.strip()
    ):
        raise _safe_auth_error(
            status_code=401,
            code=access_control.INVALID_ACCESS_TOKEN,
            message="The access token is invalid.",
            request=request,
            required_permission=required_permission,
            route_template=route_template,
        )
    try:
        principal = request.app.state.authenticator.verify(token)
    except access_control.AccessTokenError as exc:
        raise _safe_auth_error(
            status_code=401,
            code=exc.code,
            message=exc.public_message,
            request=request,
            required_permission=required_permission,
            route_template=route_template,
        ) from exc

    if required_permission not in principal.permissions:
        raise _safe_auth_error(
            status_code=403,
            code=INSUFFICIENT_PERMISSION,
            message="The authenticated principal lacks the required permission.",
            request=request,
            required_permission=required_permission,
            principal=principal,
            route_template=route_template,
        )
    return principal


def _http_exception_response(exc: HTTPException) -> JSONResponse:
    """Render a locally constructed safe HTTPException from middleware."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


def _contains_compact_access_token(value: object) -> bool:
    """Find token-shaped strings recursively without decoding any claims."""
    if isinstance(value, str):
        return _COMPACT_ACCESS_TOKEN_PATTERN.search(value) is not None
    if isinstance(value, dict):
        return any(
            _contains_compact_access_token(key)
            or _contains_compact_access_token(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_compact_access_token(item) for item in value)
    return False


async def _protected_input_contains_compact_token(request: Request) -> bool:
    """Inspect protected path/query/body values after auth, before routing."""
    if _contains_compact_access_token(request.url.path):
        return True
    if any(
        _contains_compact_access_token(key)
        or _contains_compact_access_token(value)
        for key, value in request.query_params.multi_items()
    ):
        return True

    body = await request.body()
    if not body:
        return False
    if _contains_compact_access_token(body.decode("utf-8", errors="ignore")):
        return True
    if "json" not in request.headers.get("content-type", "").casefold():
        return False
    try:
        decoded = json.loads(body)
    except (TypeError, ValueError, UnicodeError):
        return False
    return _contains_compact_access_token(decoded)


def _protected_validation_response(request: Request) -> JSONResponse:
    """Return and log one closed validation refusal without input values."""
    error_id = _new_error_id()
    principal = getattr(request.state, "principal", None)
    safe_extra = {
        "event": "protected_request_validation_rejected",
        "error_id": error_id,
        "route": getattr(
            request.state, "comparison_route_template", "unmatched_route"
        ),
        "required_permission": getattr(
            request.state, "required_permission", None
        ),
        "actor_subject": (
            principal.subject
            if isinstance(principal, access_control.Principal)
            else None
        ),
        "actor_token_id": (
            principal.token_id
            if isinstance(principal, access_control.Principal)
            else None
        ),
    }
    logger.warning(
        "protected_request_validation_rejected error_id=%s route=%s",
        error_id,
        safe_extra["route"],
        extra=safe_extra,
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "invalid_request",
                "message": "The request body or parameters are invalid.",
                "error_id": error_id,
            }
        },
    )


@app.middleware("http")
async def authenticate_comparison_routes(
    request: Request,
    call_next: Callable,
) -> Response:
    """Enforce comparison RBAC before request bodies or resources are read."""
    requirement = _comparison_route_requirement(request)
    if requirement is not None:
        route_template, required_permission = requirement
        request.state.comparison_route_template = route_template
        request.state.required_permission = required_permission
        try:
            request.state.principal = _verify_comparison_request(
                request,
                route_template=route_template,
                required_permission=required_permission,
            )
        except HTTPException as exc:
            return _http_exception_response(exc)
        request.state.comparison_protected = True
        if await _protected_input_contains_compact_token(request):
            return _protected_validation_response(request)
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def sanitize_protected_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    """Do not echo protected request inputs from FastAPI validation errors."""
    if not getattr(request.state, "comparison_protected", False):
        return await request_validation_exception_handler(request, exc)
    return _protected_validation_response(request)


def _to_agent_history(history: list[ChatMessage]) -> list[tuple[str, str]]:
    """Convert UI message history into LangChain chat roles.

    Drops blocked turns (the refused question and its block message). Bedrock's
    guardrail scans the whole input including history, so replaying a denied
    question would block every later turn in the conversation. The blocked turn
    stays visible in the UI; it just is not fed back to the model.
    """
    recent = history[-(MAX_HISTORY_TURNS * 2):]
    role_map = {
        "user": "human",
        "assistant": "ai",
    }

    cleaned: list[tuple[str, str]] = []
    for msg in recent:
        role = role_map[msg.role]
        if role == "ai" and detect_guardrail_intervention(msg.content, []) == "blocked":
            # Also drop the user question that triggered the block.
            if cleaned and cleaned[-1][0] == "human":
                cleaned.pop()
            continue
        cleaned.append((role, msg.content))
    return cleaned


def _to_chat_source(raw: dict) -> RetrievedSource:
    """Map an agent source dict onto the chat allowlist, field by field.

    The `source` value (a local absolute path) is discarded here, matching the
    review API's SafeReviewSource mapping.
    """
    return RetrievedSource(
        rank=raw.get("rank", 0),
        source_name=raw.get("source_name", ""),
        source_path=raw.get("source_path", ""),
        section_title=raw.get("section_title"),
        page=raw.get("page"),
        excerpt=raw.get("excerpt", ""),
    )


def _resolve_doc_path(document_path: str) -> Path:
    """Resolve a requested document path inside the docs directory."""
    root = DOCS_DIR.resolve()
    resolved = (root / document_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc

    if not resolved.is_file() or resolved.suffix.lower() not in SUPPORTED_DOC_SUFFIXES:
        raise HTTPException(status_code=404, detail="Document not found.")
    return resolved


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness check: this process is running and can serve a request.

    Deliberately simpler than readiness — it inspects no dependency, so a live
    process with unavailable storage still answers ``ok`` here and ``not_ready``
    at /api/ready.
    """
    return {"status": "ok"}


@app.get("/api/ready")
def ready(response: Response) -> dict:
    """Read-only readiness for the API role of the local reference runtime.

    Creates nothing and migrates nothing: storage initialization is a separate
    explicit operator action (see OPERATIONS.md). Fails closed — a dependency
    that cannot be observed is reported failed, never ready — and returns 503
    with a stable code plus a correlation id when it does.

    The body carries stable check names and codes only: no filesystem path, no
    secret, no SQL, no schema text, and no exception text.
    """
    report = runtime_readiness.evaluate(runtime_readiness.ROLE_API)
    body = {
        "status": report["status"],
        "role": report["role"],
        "checks": [
            {
                "name": check["name"],
                "status": check["status"],
                "code": check["code"],
            }
            for check in report["checks"]
        ],
    }
    if report["status"] != runtime_readiness.STATUS_READY:
        error_id = _new_error_id()
        logger.warning(
            "Runtime readiness refused (error_id=%s, failed=%s)",
            error_id,
            [
                check["name"]
                for check in report["checks"]
                if check["status"] != runtime_readiness.CHECK_OK
            ],
        )
        response.status_code = 503
        body["code"] = runtime_readiness.NOT_READY_CODE
        body["message"] = runtime_readiness.NOT_READY_MESSAGE
        body["error_id"] = error_id
    return body


@app.get("/api/documents", response_model=list[CorpusDocument])
def list_documents() -> list[CorpusDocument]:
    """List reviewable source documents from the local corpus."""
    documents = []
    for path in sorted(DOCS_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_DOC_SUFFIXES:
            continue

        relative_path = path.relative_to(DOCS_DIR).as_posix()
        encoded_path = quote(relative_path)
        documents.append(
            CorpusDocument(
                name=path.name,
                path=relative_path,
                file_type=path.suffix.lower().lstrip("."),
                url=f"/api/documents/{encoded_path}",
            )
        )
    return documents


@app.get("/api/documents/{document_path:path}")
def get_document(document_path: str) -> FileResponse:
    """Open a source document from the local corpus."""
    path = _resolve_doc_path(document_path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=path.name,
            content_disposition_type="inline",
        )

    media_type = MEDIA_TYPES.get(suffix)
    if media_type is None:
        guessed, _ = mimetypes.guess_type(path.name)
        media_type = guessed or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


def _document_url_for(source_path: str | None) -> str | None:
    """Return the served URL for a corpus-relative source path, or None.

    Reuses the docs-directory resolver so a URL is only produced for files the
    document endpoint would actually serve. Anything unresolvable (missing file,
    escape attempt, unsupported suffix) maps to None instead of an error.
    """
    if not source_path or not isinstance(source_path, str):
        return None
    try:
        resolved = _resolve_doc_path(source_path)
    except HTTPException:
        return None
    relative = resolved.relative_to(DOCS_DIR.resolve()).as_posix()
    return f"/api/documents/{quote(relative)}"


def _to_safe_source(raw: dict) -> SafeReviewSource:
    """Map one stored queue source onto the response allowlist, field by field.

    The stored `source` value (a local absolute path) is discarded here.
    """
    source_path = raw.get("source_path")
    return SafeReviewSource(
        rank=raw.get("rank"),
        sourceName=raw.get("source_name"),
        sourcePath=source_path,
        sectionTitle=raw.get("section_title"),
        page=raw.get("page"),
        excerpt=raw.get("excerpt"),
        documentUrl=_document_url_for(source_path),
    )


def _to_review_summary(item: dict, status: str) -> ReviewSummary:
    """Map a stored queue item onto the summary allowlist. No dict spread."""
    return ReviewSummary(
        reviewId=item.get("reviewId", ""),
        question=item.get("question", ""),
        riskScore=item.get("riskScore", 0.0),
        riskLevel=item.get("riskLevel", ""),
        riskReasons=list(item.get("riskReasons") or []),
        reviewStatus=status,
        createdAt=item.get("createdAt") or "",
        reviewedAt=item.get("reviewedAt"),
        wasWithheld=item.get("wasWithheld"),
    )


def _to_review_detail(item: dict, status: str) -> ReviewDetail:
    """Map a stored queue item onto the detail allowlist. No dict spread."""
    sources = item.get("retrievedSources") or []
    return ReviewDetail(
        reviewId=item.get("reviewId", ""),
        question=item.get("question", ""),
        riskScore=item.get("riskScore", 0.0),
        riskLevel=item.get("riskLevel", ""),
        riskReasons=list(item.get("riskReasons") or []),
        reviewStatus=status,
        createdAt=item.get("createdAt") or "",
        reviewedAt=item.get("reviewedAt"),
        wasWithheld=item.get("wasWithheld"),
        auditId=item.get("auditId"),
        draftAnswer=item.get("draftAnswer", ""),
        retrievedSources=[
            _to_safe_source(source) for source in sources if isinstance(source, dict)
        ],
        decision=item.get("decision"),
        reviewerNote=item.get("reviewerNote"),
        governanceReport=_governance_report_for(item.get("auditId")),
    )


def _governance_report_for(audit_id: str | None) -> dict | None:
    """Scan the audit log for audit_id and return only its governance_report.

    The rest of the audit record stays server-side on purpose: retrieved_sources
    carries local absolute paths and response_trace carries full tool content.
    A missing record, absent log, or unreadable line yields None.
    """
    if not audit_id:
        return None
    try:
        with Path(config.AUDIT_LOG_PATH).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("audit_id") == audit_id:
                    report = record.get("governance_report")
                    return report if isinstance(report, dict) else None
    except OSError:
        return None
    return None


@app.get("/api/reviews", response_model=list[ReviewSummary])
def list_reviews(
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
) -> list[ReviewSummary]:
    """List human review queue items.

    Ordering: pending items come oldest first by createdAt. Approved and
    rejected items come most recently reviewed first (reviewedAt, falling back
    to createdAt). For status=all the response is the pending block first in
    its own order, then the terminal block in its own order.
    """
    pairs = review_queue.list_items(config.REVIEW_QUEUE_DIR, status)
    pending = [pair for pair in pairs if pair[1] == "pending"]
    terminal = [pair for pair in pairs if pair[1] != "pending"]
    pending.sort(key=lambda pair: pair[0].get("createdAt") or "")
    terminal.sort(
        key=lambda pair: pair[0].get("reviewedAt") or pair[0].get("createdAt") or "",
        reverse=True,
    )
    return [_to_review_summary(item, found) for item, found in pending + terminal]


@app.get("/api/reviews/{review_id}", response_model=ReviewDetail)
def get_review(review_id: str) -> ReviewDetail:
    """Fetch one review item by id, searching pending and terminal files."""
    found = review_queue.get_any(review_id, config.REVIEW_QUEUE_DIR)
    if found is None:
        raise HTTPException(status_code=404, detail="Review item not found.")
    item, status = found
    return _to_review_detail(item, status)


def _resolve_review_action(review_id: str, action, target_status: str, note: str | None) -> ReviewDetail:
    """Shared approve/reject flow: 404 absent, 409 terminal, 200 on success."""
    found = review_queue.get_any(review_id, config.REVIEW_QUEUE_DIR)
    if found is None:
        raise HTTPException(status_code=404, detail="Review item not found.")
    if found[1] != "pending":
        raise HTTPException(
            status_code=409, detail=f"Review item is already {found[1]}."
        )

    try:
        resolved = action(review_id, config.REVIEW_QUEUE_DIR, note=note)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="Failed to write the review queue."
        ) from exc

    if resolved is None:
        # The item left pending between the check and the write. Re-classify
        # into 404/409 rather than surfacing a 500.
        recheck = review_queue.get_any(review_id, config.REVIEW_QUEUE_DIR)
        if recheck is None:
            raise HTTPException(status_code=404, detail="Review item not found.")
        raise HTTPException(
            status_code=409, detail=f"Review item is already {recheck[1]}."
        )
    return _to_review_detail(resolved, target_status)


@app.post("/api/reviews/{review_id}/approve", response_model=ReviewDetail)
def approve_review(
    review_id: str, payload: ReviewActionRequest | None = None
) -> ReviewDetail:
    """Approve a pending review item, stamping reviewedAt server-side."""
    note = payload.note if payload else None
    return _resolve_review_action(review_id, review_queue.approve, "approved", note)


@app.post("/api/reviews/{review_id}/reject", response_model=ReviewDetail)
def reject_review(
    review_id: str, payload: ReviewActionRequest | None = None
) -> ReviewDetail:
    """Reject a pending review item, stamping reviewedAt server-side."""
    note = payload.note if payload else None
    return _resolve_review_action(review_id, review_queue.reject, "rejected", note)


def _to_comparison_dto(record: dict) -> ComparisonRecordDTO:
    """Map a stored comparison record onto the allowlist, field by field.

    Filing ids, scope keys, timestamps, and lifecycle fields only — no
    storage paths, no registry entry contents.
    """
    return ComparisonRecordDTO(
        comparisonId=record.get("comparison_id", ""),
        schemaVersion=record.get("schema_version", ""),
        workflowVersion=record.get("workflow_version", ""),
        previousFilingId=record.get("previous_filing_id", ""),
        currentFilingId=record.get("current_filing_id", ""),
        sectionScope=list(record.get("section_scope") or []),
        status=record.get("status", "ready_for_detection"),
        createdAt=record.get("created_at", ""),
        updatedAt=record.get("updated_at", ""),
        failureCode=record.get("failure_code"),
        failureSummary=record.get("failure_summary"),
    )


def _comparison_storage_error(exc: Exception) -> HTTPException:
    """Log the real storage failure; return a safe 500 with a correlation id.

    SQL text, SQLite error strings, and filesystem paths stay server-side.
    """
    error_id = _new_error_id()
    logger.exception("Comparison storage failure (error_id=%s)", error_id)
    return HTTPException(
        status_code=500,
        detail={
            "code": "comparison_storage_error",
            "message": "Failed to access comparison storage.",
            "error_id": error_id,
        },
    )


@app.post(
    "/api/comparisons",
    response_model=ComparisonCreateResponse,
)
def create_comparison(
    request: ComparisonCreateRequest,
    response: Response,
    principal: Annotated[
        access_control.Principal,
        Depends(require_permission("comparison.create")),
    ],
) -> ComparisonCreateResponse:
    """Create (or idempotently return) a comparison for a validated filing pair.

    201 with created=true for a new record; 200 with created=false when the
    identical logical comparison already exists. Ineligible pairs are 422
    with stable machine-readable reason codes — nothing is persisted.
    """
    try:
        record, created = comparison_store.create_comparison(
            request.previousFilingId.strip(),
            request.currentFilingId.strip(),
            request.sectionScope,
        )
    except comparison_store.ComparisonPairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_comparison_pair",
                "reasons": exc.reasons,
                "message": exc.detail,
            },
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    if created:
        comparison_reliability.log_lifecycle_event(
            comparison_reliability.EVENT_COMPARISON_CREATED,
            comparison_id=record.get("comparison_id"),
            status=record.get("status"),
            actor_context=_actor_context(principal, "comparison.create"),
        )
    return ComparisonCreateResponse(
        created=created, comparison=_to_comparison_dto(record)
    )


@app.get(
    "/api/comparisons",
    response_model=list[ComparisonRecordDTO],
    dependencies=[Depends(require_permission("comparison.read"))],
)
def list_comparisons(
    filing_id: str | None = None,
    status: Literal[
        "ready_for_detection",
        "queued_for_detection",
        "detecting",
        "waiting_for_detection_retry",
        "detected",
        "failed",
    ]
    | None = None,
) -> list[ComparisonRecordDTO]:
    """List comparisons, newest first. Minimal stable filters only:
    filing_id matches either side of the pair; status matches exactly."""
    try:
        records = comparison_store.list_comparisons(
            filing_id=filing_id, status=status
        )
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_comparison_dto(record) for record in records]


@app.get(
    "/api/comparisons/{comparison_id}",
    response_model=ComparisonRecordDTO,
    dependencies=[Depends(require_permission("comparison.read"))],
)
def get_comparison(comparison_id: str) -> ComparisonRecordDTO:
    """Fetch one comparison by id; 404 when it does not exist."""
    try:
        record = comparison_store.get_comparison(comparison_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Comparison not found.")
    return _to_comparison_dto(record)


@app.post(
    "/api/comparisons/{comparison_id}/detect",
    response_model=ComparisonDetectionJobResponse | ComparisonDetectResponse,
    responses={
        202: {
            "model": ComparisonDetectionJobResponse,
            "description": "Detection job queued or equivalent active job returned.",
        },
        200: {
            "model": ComparisonDetectResponse,
            "description": "Existing detected result returned idempotently.",
        },
    },
)
def detect_comparison(
    comparison_id: str,
    response: Response,
    principal: Annotated[
        access_control.Principal,
        Depends(require_permission("comparison.detect")),
    ],
) -> ComparisonDetectionJobResponse | ComparisonDetectResponse:
    """Durably queue deterministic Item 1A detection; never execute it inline.

    202 creates or idempotently returns one active job. No attempt exists until
    a separate one-shot worker claims it. A current already-detected result
    preserves the existing 200 result contract and creates no job.
    """
    actor_context = _actor_context(principal, "comparison.detect")
    try:
        outcome = comparison_detection_worker.enqueue_initial_detection(
            comparison_id,
            requested_by_subject=principal.subject,
            requested_by_auth_method=principal.auth_method,
            requested_by_token_id=principal.token_id,
            requested_by_policy_id=principal.policy_id,
            requested_by_policy_version=principal.policy_version,
            actor_context=actor_context,
        )
    except comparison_detector.UnknownComparison:
        raise HTTPException(status_code=404, detail="Comparison not found.")
    except comparison_store.DetectionStateError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except comparison_store.ComparisonPairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_comparison_pair",
                "reasons": exc.reasons,
                "message": exc.detail,
            },
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    if outcome["kind"] == "result":
        response.status_code = 200
        return ComparisonDetectResponse(
            created=False,
            result=outcome["result"]["result"],
            attemptId=outcome["attempt_id"],
        )

    job = outcome["job"]
    response.status_code = 202
    return ComparisonDetectionJobResponse(
        created=outcome["created"],
        comparisonId=job["comparison_id"],
        jobId=job["job_id"],
        jobStatus=job["status"],
        comparisonStatus=(
            "queued_for_detection"
            if job["status"] == comparison_store.JOB_QUEUED
            else (
                "waiting_for_detection_retry"
                if job["status"] == comparison_store.JOB_RETRY_WAIT
                else "detecting"
            )
        ),
        queuedAt=job["queued_at"],
        attemptId=job["attempt_id"],
    )


def _to_detection_attempt_dto(record: dict) -> DetectionAttemptDTO:
    """Map a stored attempt onto the allowlist, field by field."""
    return DetectionAttemptDTO(
        attemptId=record.get("attempt_id", ""),
        comparisonId=record.get("comparison_id", ""),
        attemptNumber=record.get("attempt_number", 0),
        status=record.get("status", "running"),
        detectorVersion=record.get("detector_version", ""),
        workflowVersion=record.get("workflow_version", ""),
        previousSourceHash=record.get("previous_source_hash", ""),
        currentSourceHash=record.get("current_source_hash", ""),
        startedAt=record.get("started_at", ""),
        finishedAt=record.get("finished_at"),
        resultHash=record.get("result_hash"),
        failureCode=record.get("failure_code"),
        failureSummary=record.get("failure_summary"),
    )


def _to_detection_event_dto(record: dict) -> DetectionEventDTO:
    """Map a stored transition event onto the allowlist, field by field."""
    return DetectionEventDTO(
        eventId=record.get("event_id", ""),
        attemptId=record.get("attempt_id", ""),
        comparisonId=record.get("comparison_id", ""),
        eventType=record.get("event_type", "detection_started"),
        eventSeq=record.get("event_seq", 0),
        createdAt=record.get("created_at", ""),
        resultHash=record.get("result_hash"),
        failureCode=record.get("failure_code"),
    )


def _to_detection_job_dto(record: dict) -> DetectionJobDTO:
    return DetectionJobDTO(
        jobId=record.get("job_id", ""),
        comparisonId=record.get("comparison_id", ""),
        attemptId=record.get("attempt_id"),
        triggerType=record.get("trigger_type", "initial_detection"),
        status=record.get("status", "queued"),
        detectorVersion=record.get("detector_version", ""),
        workflowVersion=record.get("workflow_version", ""),
        requestedBySubject=record.get("requested_by_subject", ""),
        requestedByAuthMethod=record.get(
            "requested_by_auth_method", "local_hs256"
        ),
        queuedAt=record.get("queued_at", ""),
        claimedAt=record.get("claimed_at"),
        finishedAt=record.get("finished_at"),
        workerId=record.get("worker_id"),
        claimGeneration=record.get("claim_generation", 0),
        leaseStartedAt=record.get("lease_started_at"),
        heartbeatAt=record.get("heartbeat_at"),
        leaseExpiresAt=record.get("lease_expires_at"),
        leaseState=detection_job_lease.lease_state(record),
        resultHash=record.get("result_hash"),
        failureCode=record.get("failure_code"),
        retryCount=record.get("retry_count", 0),
        maxRetryAttempts=detection_job_retry.POLICY["max_retry_attempts"],
        nextAttemptAt=record.get("next_attempt_at"),
        lastFailureCode=record.get("last_failure_code"),
        lastFailureClassification=record.get(
            "last_failure_classification"
        ),
        retryState=detection_job_retry.retry_state(record),
    )


def _to_detection_job_event_dto(record: dict) -> DetectionJobEventDTO:
    return DetectionJobEventDTO(
        eventId=record.get("event_id", ""),
        jobId=record.get("job_id", ""),
        comparisonId=record.get("comparison_id", ""),
        attemptId=record.get("attempt_id"),
        eventType=record.get("event_type", "detection_job_queued"),
        eventSeq=record.get("event_seq", 0),
        createdAt=record.get("created_at", ""),
        workerId=record.get("worker_id"),
        claimGeneration=record.get("claim_generation", 0),
        sourceAttemptId=record.get("source_attempt_id"),
        replacementAttemptId=record.get("replacement_attempt_id"),
        leaseExpiresAt=record.get("lease_expires_at"),
        resultHash=record.get("result_hash"),
        failureCode=record.get("failure_code"),
        retryCount=record.get("retry_count", 0),
        failureClassification=record.get("failure_classification"),
        nextAttemptAt=record.get("next_attempt_at"),
    )


@app.get(
    "/api/comparisons/{comparison_id}/detection-jobs",
    response_model=list[DetectionJobDTO],
    dependencies=[Depends(require_permission("detection_attempt.read"))],
)
def list_comparison_detection_jobs(
    comparison_id: str,
) -> list[DetectionJobDTO]:
    try:
        if comparison_store.get_comparison(comparison_id) is None:
            raise HTTPException(status_code=404, detail="Comparison not found.")
        records = comparison_store.list_detection_jobs(comparison_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_detection_job_dto(record) for record in records]


@app.get(
    "/api/comparison-detection-jobs/{job_id}",
    response_model=DetectionJobDTO,
    dependencies=[Depends(require_permission("detection_attempt.read"))],
)
def get_comparison_detection_job(job_id: str) -> DetectionJobDTO:
    try:
        record = comparison_store.get_detection_job(job_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Detection job not found.")
    return _to_detection_job_dto(record)


@app.get(
    "/api/comparison-detection-jobs/{job_id}/events",
    response_model=list[DetectionJobEventDTO],
    dependencies=[Depends(require_permission("detection_attempt.read"))],
)
def list_comparison_detection_job_events(
    job_id: str,
) -> list[DetectionJobEventDTO]:
    try:
        if comparison_store.get_detection_job(job_id) is None:
            raise HTTPException(status_code=404, detail="Detection job not found.")
        records = comparison_store.list_detection_job_events(job_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_detection_job_event_dto(record) for record in records]


@app.get(
    "/api/comparisons/{comparison_id}/detection-attempts",
    response_model=list[DetectionAttemptDTO],
    dependencies=[Depends(require_permission("detection_attempt.read"))],
)
def list_detection_attempts(comparison_id: str) -> list[DetectionAttemptDTO]:
    """Every detection attempt for one comparison, in execution order
    (attempt_number ascending); 404 when the comparison does not exist.

    Summaries only — no comparison result payload and no evidence.
    """
    try:
        if comparison_store.get_comparison(comparison_id) is None:
            raise HTTPException(status_code=404, detail="Comparison not found.")
        records = comparison_store.list_detection_attempts(comparison_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_detection_attempt_dto(record) for record in records]


@app.get(
    "/api/detection-attempts/{attempt_id}",
    response_model=DetectionAttemptDTO,
    dependencies=[Depends(require_permission("detection_attempt.read"))],
)
def get_detection_attempt(attempt_id: str) -> DetectionAttemptDTO:
    """One detection attempt by id; 404 when it does not exist."""
    try:
        record = comparison_store.get_detection_attempt(attempt_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Detection attempt not found.")
    return _to_detection_attempt_dto(record)


@app.get(
    "/api/detection-attempts/{attempt_id}/events",
    response_model=list[DetectionEventDTO],
    dependencies=[Depends(require_permission("detection_attempt.read"))],
)
def list_detection_events(attempt_id: str) -> list[DetectionEventDTO]:
    """Append-only transition events for one attempt, oldest first.

    Deterministically ordered by the stored sequence key, so a started event
    always precedes its terminal event regardless of timestamp resolution.
    404 when the attempt does not exist.
    """
    try:
        if comparison_store.get_detection_attempt(attempt_id) is None:
            raise HTTPException(
                status_code=404, detail="Detection attempt not found."
            )
        records = comparison_store.list_detection_events(attempt_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_detection_event_dto(record) for record in records]


def _to_detection_replay_dto(record: dict) -> DetectionReplayDTO:
    """Map a stored replay onto the allowlist, field by field."""
    return DetectionReplayDTO(
        replayId=record.get("replay_id", ""),
        comparisonId=record.get("comparison_id", ""),
        sourceAttemptId=record.get("source_attempt_id", ""),
        replacementAttemptId=record.get("replacement_attempt_id", ""),
        operatorId=record.get("operator_id", ""),
        operatorIdBasis=record.get(
            "actor_auth_method", "legacy_self_asserted"
        ),
        reasonCode=record.get("reason_code", ""),
        operatorNote=record.get("operator_note", ""),
        policyId=record.get("policy_id", ""),
        policyVersion=record.get("policy_version", ""),
        requestedAt=record.get("requested_at", ""),
    )


@app.get(
    "/api/detection-attempts/{attempt_id}/recovery",
    response_model=DetectionRecoveryDTO,
    dependencies=[Depends(require_permission("recovery.read"))],
)
def get_detection_recovery(attempt_id: str) -> DetectionRecoveryDTO:
    """Read-only recovery assessment; 404 when the attempt does not exist.

    This endpoint MUTATES NOTHING. Observing that an attempt has passed the
    stale threshold does not retire it — only an explicit replay request does.
    """
    try:
        view = detection_recovery.recovery_view(attempt_id)
    except detection_recovery.ReplayAttemptNotFound:
        raise HTTPException(status_code=404, detail="Detection attempt not found.")
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return DetectionRecoveryDTO(
        attemptId=view["attempt_id"],
        comparisonId=view["comparison_id"],
        status=view["status"],
        startedAt=view["started_at"],
        staleAt=view["stale_at"],
        ageSeconds=view["age_seconds"],
        isStale=view["is_stale"],
        replayEligible=view["replay_eligible"],
        attemptsUsed=view["attempts_used"],
        maxAttempts=view["max_attempts"],
        remainingAttempts=view["remaining_attempts"],
        policyId=view["policy_id"],
        policyVersion=view["policy_version"],
        blockingReason=view["blocking_reason"],
    )


@app.post(
    "/api/detection-attempts/{attempt_id}/replay",
    response_model=DetectionReplayResponse,
)
def replay_detection_attempt(
    attempt_id: str,
    request: DetectionReplayRequest,
    response: Response,
    principal: Annotated[
        access_control.Principal,
        Depends(require_permission("recovery.replay")),
    ],
) -> DetectionReplayResponse:
    """Retire a stale running attempt and execute its replacement.

    Requires an EXPLICIT operator request: nothing here happens on a timer, and
    replay remains synchronous — the one-shot initial-detection worker never
    handles replay. 201 when this request retired the stale attempt and ran the
    replacement; 200 for the byte-equivalent idempotent
    replay (the stored replay and the replacement's current terminal outcome,
    with no second execution); 404 unknown attempt or comparison; 409 with a
    stable code when the persisted state does not permit replay (not stale,
    managed by an active detection job, already replayed by a different
    request, wrong lifecycle, attempt limit reached, changed inputs or
    versions); 422 invalid operator fields or reason code; safe 500 with a
    correlation id for unexpected storage or detector faults.
    """
    operator_id = _authenticated_actor(
        request.model_dump()["operatorId"], principal, field_name="operatorId"
    )
    actor_context = _actor_context(principal, "recovery.replay")
    try:
        outcome, created = detection_recovery.replay_attempt(
            attempt_id,
            operator_id=operator_id,
            reason_code=request.reasonCode,
            operator_note=request.operatorNote,
            actor_policy_id=principal.policy_id,
            actor_policy_version=principal.policy_version,
            actor_context=actor_context,
        )
    except detection_recovery.ReplayAttemptNotFound:
        raise HTTPException(status_code=404, detail="Detection attempt not found.")
    except comparison_detector.UnknownComparison:
        raise HTTPException(status_code=404, detail="Comparison not found.")
    except detection_recovery.ReplayRequestError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except comparison_store.ComparisonPairError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_comparison_pair",
                "reasons": exc.reasons,
                "message": exc.detail,
            },
        ) from exc
    except comparison_store.DetectionStateError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except comparison_store.ComparisonLifecycleError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.status, "message": str(exc)}
        ) from exc
    except comparison_detector.DetectionInternalError as exc:
        # The replacement attempt is already durably finalized as failed; the
        # client gets a correlation id and no raw fault text. The operator note
        # is deliberately NOT logged.
        error_id = _new_error_id()
        logger.exception("Replay detection failed (error_id=%s)", error_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": exc.code,
                "message": "Replay detection failed unexpectedly. Please try again.",
                "error_id": error_id,
            },
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    return DetectionReplayResponse(
        created=created,
        replay=_to_detection_replay_dto(outcome["replay"]),
        sourceAttemptId=outcome["source_attempt_id"],
        replacementAttemptId=outcome["replacement_attempt_id"],
        replacementStatus=outcome["replacement_status"],
        result=outcome["result"],
    )


@app.get(
    "/api/detection-attempts/{attempt_id}/replays",
    response_model=list[DetectionReplayDTO],
    dependencies=[Depends(require_permission("recovery.read"))],
)
def list_detection_replays(attempt_id: str) -> list[DetectionReplayDTO]:
    """The replay that retired this attempt: zero or one row under the v1
    design (UNIQUE(source_attempt_id)). 404 when the attempt does not exist."""
    try:
        if comparison_store.get_detection_attempt(attempt_id) is None:
            raise HTTPException(
                status_code=404, detail="Detection attempt not found."
            )
        replay = comparison_store.get_detection_replay_for_source(attempt_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [] if replay is None else [_to_detection_replay_dto(replay)]


# --- Read-only reliability visibility ----------------------------------------
#
# Three GET routes over the persisted comparison lifecycle. NONE of them
# mutates anything: they open the database read-only, they never start, retire,
# retry, or replay an attempt, and observing a stale attempt here does not
# change it — that still requires the explicit replay route above for an
# eligible direct/replay attempt. These reads never enqueue, claim, or execute
# the one-shot job path, and no scheduler, daemon, notification, or external
# monitoring integration sits behind them.


def _reliability_query_error(exc: comparison_reliability.ReliabilityQueryError):
    """Stable 422 for an invalid window, filter, or limit."""
    return HTTPException(
        status_code=422, detail={"code": exc.code, "message": exc.message}
    )


def _reliability_data_error(exc: comparison_reliability.ReliabilityDataError):
    """Fail-closed 500: stored records contradict themselves.

    The report is refused rather than computed over inconsistent rows. The
    client gets the stable code and a correlation id; WHICH rows broke WHICH
    invariant stays in the server log.
    """
    error_id = _new_error_id()
    logger.error(
        "Reliability data invalid (error_id=%s) reasons=%s detail=%s",
        error_id,
        exc.reasons,
        exc.detail,
    )
    return HTTPException(
        status_code=500,
        detail={
            "code": exc.code,
            "message": (
                "Stored workflow records are internally inconsistent, so a "
                "reliability report was refused rather than computed."
            ),
            "error_id": error_id,
        },
    )


def _reliability_storage_unavailable(
    exc: comparison_reliability.ReliabilityStorageUnavailable,
):
    """Fail-closed 500: the comparison database cannot be observed at all.

    Missing or unreadable storage is NOT an empty system, so no zero-valued
    report is returned — that would tell an operator nothing needs attention at
    exactly the moment nothing can be seen. The client gets the stable code and
    a correlation id; the configured path and the SQLite fault stay in the
    server log.
    """
    error_id = _new_error_id()
    logger.error(
        "Reliability storage unavailable (error_id=%s) reason=%s detail=%s",
        error_id,
        exc.reason,
        exc.detail,
    )
    return HTTPException(
        status_code=500,
        detail={
            "code": exc.code,
            "message": (
                "Comparison workflow storage could not be read, so a "
                "reliability report was refused rather than reported as empty."
            ),
            "error_id": error_id,
        },
    )


def _reliability_dependency_error(
    exc: comparison_reliability.ReliabilityDependencyUnavailable,
):
    """Fail-closed 500: a dependency a requested metric needs cannot answer.

    Replay eligibility is a statement about filing-registry truth. When the
    registry is absent, unreadable, malformed, or empty, the report is refused
    rather than reporting zero eligible attempts — a clean-looking number would
    tell an operator nothing needs action at exactly the moment the system
    cannot tell. The client gets the stable code, the dependency name, and a
    correlation id; the configured path and the underlying fault stay in the
    server log.
    """
    error_id = _new_error_id()
    logger.error(
        "Reliability dependency unavailable (error_id=%s) dependency=%s "
        "reason=%s detail=%s",
        error_id,
        exc.dependency,
        exc.reason,
        exc.detail,
    )
    return HTTPException(
        status_code=500,
        detail={
            "code": exc.code,
            "dependency": exc.dependency,
            "message": (
                "A dependency required to evaluate replay eligibility is "
                "unavailable, so a reliability report was refused rather than "
                "reported as zero."
            ),
            "error_id": error_id,
        },
    )


@app.get(
    "/api/comparison-reliability/summary",
    response_model=ReliabilitySummaryDTO,
    dependencies=[Depends(require_permission("reliability.read"))],
)
def get_reliability_summary(
    since: str | None = None, until: str | None = None
) -> ReliabilitySummaryDTO:
    """Structured reliability aggregate. READ-ONLY.

    ``since`` / ``until`` are optional, inclusive, timezone-aware UTC ISO
    timestamps; a naive timestamp or an inverted range is a 422. They window the
    historical counters, rates, durations, and failure breakdowns only —
    current-state gauges are evaluated at query time. 500 with a stable code and
    correlation id when stored records are inconsistent or storage is
    unreadable.
    """
    try:
        report = comparison_reliability.summary(since=since, until=until)
    except comparison_reliability.ReliabilityQueryError as exc:
        raise _reliability_query_error(exc) from exc
    except comparison_reliability.ReliabilityDataError as exc:
        raise _reliability_data_error(exc) from exc
    except comparison_reliability.ReliabilityStorageUnavailable as exc:
        raise _reliability_storage_unavailable(exc) from exc
    except comparison_reliability.ReliabilityDependencyUnavailable as exc:
        raise _reliability_dependency_error(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return _to_reliability_summary_dto(report)


def _to_reliability_summary_dto(report: dict) -> ReliabilitySummaryDTO:
    """Map the service report onto the camelCase allowlist, field by field."""
    gauges, attempts = report["gauges"], report["attempts"]
    jobs, job_durations = report["jobs"], report["job_durations"]
    replays, durations = report["replays"], report["durations"]
    breakdown = report["failure_breakdown"]
    return ReliabilitySummaryDTO(
        contractVersion=report["contract_version"],
        generatedAt=report["generated_at"],
        since=report["since"],
        until=report["until"],
        detectorVersions=list(report["detector_versions"]),
        workflowVersions=list(report["workflow_versions"]),
        recoveryPolicyId=report["recovery_policy_id"],
        recoveryPolicyVersion=report["recovery_policy_version"],
        staleAfterSeconds=report["stale_after_seconds"],
        maxAttemptsPerComparison=report["max_attempts_per_comparison"],
        leasePolicyId=report["lease_policy_id"],
        leasePolicyVersion=report["lease_policy_version"],
        leaseDurationSeconds=report["lease_duration_seconds"],
        heartbeatExtensionSeconds=report["heartbeat_extension_seconds"],
        reclaimGraceSeconds=report["reclaim_grace_seconds"],
        maxClaimGenerations=report["max_claim_generations"],
        retryPolicyId=report["retry_policy_id"],
        retryPolicyVersion=report["retry_policy_version"],
        maxRetryAttempts=report["max_retry_attempts"],
        gauges=ReliabilityGaugesDTO(
            comparisonsReadyForDetection=gauges["comparisons_ready_for_detection"],
            comparisonsQueuedForDetection=gauges[
                "comparisons_queued_for_detection"
            ],
            comparisonsDetecting=gauges["comparisons_detecting"],
            comparisonsWaitingForDetectionRetry=gauges[
                "comparisons_waiting_for_detection_retry"
            ],
            comparisonsDetected=gauges["comparisons_detected"],
            comparisonsFailed=gauges["comparisons_failed"],
            runningAttempts=gauges["running_attempts"],
            staleRunningAttempts=gauges["stale_running_attempts"],
            replayEligibleAttempts=gauges["replay_eligible_attempts"],
            attemptLimitExhaustedComparisons=gauges[
                "attempt_limit_exhausted_comparisons"
            ],
            detectionJobsQueued=gauges["detection_jobs_queued"],
            detectionJobsRunning=gauges["detection_jobs_running"],
            detectionJobsWaitingForRetry=gauges[
                "detection_jobs_waiting_for_retry"
            ],
            detectionJobsSucceeded=gauges["detection_jobs_succeeded"],
            detectionJobsFailed=gauges["detection_jobs_failed"],
            activeJobLeases=gauges["active_job_leases"],
            expiredJobLeases=gauges["expired_job_leases"],
            reclaimableJobs=gauges["reclaimable_jobs"],
            claimExhaustedJobs=gauges["claim_exhausted_jobs"],
            detectionJobsRetryDue=gauges["detection_jobs_retry_due"],
            detectionJobsRetryNotDue=gauges[
                "detection_jobs_retry_not_due"
            ],
            detectionJobsRetryExhausted=gauges[
                "detection_jobs_retry_exhausted"
            ],
            unresolvedOperationalIssues=gauges["unresolved_operational_issues"],
        ),
        jobs=ReliabilityJobCountersDTO(
            jobsQueued=jobs["jobs_queued"],
            jobsClaimed=jobs["jobs_claimed"],
            jobsSucceeded=jobs["jobs_succeeded"],
            jobsFailed=jobs["jobs_failed"],
            jobHeartbeats=jobs["job_heartbeats"],
            jobsReclaimed=jobs["jobs_reclaimed"],
            jobsClaimExhausted=jobs["jobs_claim_exhausted"],
            retriesScheduled=jobs["retries_scheduled"],
            retriesClaimed=jobs["retries_claimed"],
            retriesSucceeded=jobs["retries_succeeded"],
            retriesFailed=jobs["retries_failed"],
            retriesExhausted=jobs["retries_exhausted"],
        ),
        jobDurations=ReliabilityJobDurationsDTO(
            queueWaitCount=job_durations["queue_wait_count"],
            queueWaitSecondsMin=job_durations["queue_wait_seconds_min"],
            queueWaitSecondsMax=job_durations["queue_wait_seconds_max"],
            queueWaitSecondsMean=job_durations["queue_wait_seconds_mean"],
            queueWaitSecondsP50=job_durations["queue_wait_seconds_p50"],
            queueWaitSecondsP95=job_durations["queue_wait_seconds_p95"],
            executionCount=job_durations["execution_count"],
            executionSecondsMin=job_durations["execution_seconds_min"],
            executionSecondsMax=job_durations["execution_seconds_max"],
            executionSecondsMean=job_durations["execution_seconds_mean"],
            executionSecondsP50=job_durations["execution_seconds_p50"],
            executionSecondsP95=job_durations["execution_seconds_p95"],
            negativeQueueWaitJobs=job_durations["negative_queue_wait_jobs"],
            negativeExecutionJobs=job_durations["negative_execution_jobs"],
            negativeLeaseDurationJobs=job_durations[
                "negative_lease_duration_jobs"
            ],
            percentileMethod=job_durations["percentile_method"],
        ),
        attempts=ReliabilityAttemptCountersDTO(
            attemptsStarted=attempts["attempts_started"],
            attemptsSucceeded=attempts["attempts_succeeded"],
            attemptsFailed=attempts["attempts_failed"],
            attemptsTimedOut=attempts["attempts_timed_out"],
            attemptsRunningInWindow=attempts["attempts_running_in_window"],
            terminalAttempts=attempts["terminal_attempts"],
        ),
        attemptRates=ReliabilityAttemptRatesDTO(
            successRate=_to_reliability_rate_dto(
                report["attempt_rates"]["success_rate"]
            ),
            failureRate=_to_reliability_rate_dto(
                report["attempt_rates"]["failure_rate"]
            ),
            timeoutRate=_to_reliability_rate_dto(
                report["attempt_rates"]["timeout_rate"]
            ),
        ),
        replays=ReliabilityReplayCountersDTO(
            replaysStarted=replays["replays_started"],
            replayReplacementsSucceeded=replays["replay_replacements_succeeded"],
            replayReplacementsFailed=replays["replay_replacements_failed"],
            replayReplacementsRunning=replays["replay_replacements_running"],
            replayReplacementsTimedOut=replays["replay_replacements_timed_out"],
            terminalReplayReplacements=replays["terminal_replay_replacements"],
        ),
        replayRates=ReliabilityReplayRatesDTO(
            replaySuccessRate=_to_reliability_rate_dto(
                report["replay_rates"]["replay_success_rate"]
            )
        ),
        durations=ReliabilityDurationsDTO(
            durationCount=durations["duration_count"],
            durationSecondsMin=durations["duration_seconds_min"],
            durationSecondsMax=durations["duration_seconds_max"],
            durationSecondsMean=durations["duration_seconds_mean"],
            durationSecondsP50=durations["duration_seconds_p50"],
            durationSecondsP95=durations["duration_seconds_p95"],
            negativeDurationAttempts=durations["negative_duration_attempts"],
            percentileMethod=durations["percentile_method"],
        ),
        failureBreakdown=ReliabilityFailureBreakdownDTO(
            failedAttemptsByCode=dict(breakdown["failed_attempts_by_code"]),
            timedOutAttemptsByCode=dict(breakdown["timed_out_attempts_by_code"]),
            failuresByDetectorVersion=dict(breakdown["failures_by_detector_version"]),
            failuresByWorkflowVersion=dict(breakdown["failures_by_workflow_version"]),
            retryableFailuresByCode=dict(
                breakdown["retryable_failures_by_code"]
            ),
            nonRetryableFailuresByCode=dict(
                breakdown["non_retryable_failures_by_code"]
            ),
            retryExhaustionsByOriginalCode=dict(
                breakdown["retry_exhaustions_by_original_code"]
            ),
        ),
    )


def _to_reliability_rate_dto(metric: dict) -> ReliabilityRateDTO:
    return ReliabilityRateDTO(
        metric=metric["metric"],
        value=metric["value"],
        numerator=metric["numerator"],
        denominator=metric["denominator"],
        zeroDenominator=metric["zero_denominator"],
        zeroDenominatorPolicy=metric.get("zero_denominator_policy"),
    )


@app.get(
    "/api/comparison-reliability/issues",
    response_model=ReliabilityIssuesResponse,
    dependencies=[Depends(require_permission("reliability.read"))],
)
def get_reliability_issues(
    issue_type: Literal[
        "stale_running_attempt",
        "attempt_limit_exhausted",
        "comparison_failed",
        "replacement_attempt_failed",
        "invalid_negative_duration",
        "invalid_negative_lease_duration",
        "queued_detection_job",
        "expired_detection_job_lease",
        "detection_job_claims_exhausted",
        "detection_job_waiting_for_retry",
        "detection_job_retry_overdue",
        "detection_job_retries_exhausted",
    ]
    | None = None,
    comparison_id: str | None = Query(default=None, max_length=120),
    limit: int = Query(
        default=comparison_reliability.DEFAULT_LIMIT,
        ge=1,
        le=comparison_reliability.MAX_LIMIT,
    ),
) -> ReliabilityIssuesResponse:
    """Currently unresolved operational issues. READ-ONLY, deterministic order.

    Current state only — no time window, by design (see the response model).
    Listing an issue does nothing to it: a stale attempt stays running until an
    operator explicitly replays it. Unknown ``issue_type`` values and
    out-of-range limits are 422.
    """
    try:
        report = comparison_reliability.issues(
            issue_type=issue_type, comparison_id=comparison_id, limit=limit
        )
    except comparison_reliability.ReliabilityQueryError as exc:
        raise _reliability_query_error(exc) from exc
    except comparison_reliability.ReliabilityDataError as exc:
        raise _reliability_data_error(exc) from exc
    except comparison_reliability.ReliabilityStorageUnavailable as exc:
        raise _reliability_storage_unavailable(exc) from exc
    except comparison_reliability.ReliabilityDependencyUnavailable as exc:
        raise _reliability_dependency_error(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return ReliabilityIssuesResponse(
        contractVersion=report["contract_version"],
        generatedAt=report["generated_at"],
        recoveryPolicyId=report["recovery_policy_id"],
        recoveryPolicyVersion=report["recovery_policy_version"],
        leasePolicyId=report["lease_policy_id"],
        leasePolicyVersion=report["lease_policy_version"],
        retryPolicyId=report["retry_policy_id"],
        retryPolicyVersion=report["retry_policy_version"],
        total=report["total"],
        returned=report["returned"],
        truncated=report["truncated"],
        issues=[_to_reliability_issue_dto(item) for item in report["issues"]],
    )


def _to_reliability_issue_dto(issue: dict) -> ReliabilityIssueDTO:
    """Map one issue onto the closed allowlist, field by field."""
    return ReliabilityIssueDTO(
        issueType=issue["issue_type"],
        comparisonId=issue["comparison_id"],
        jobId=issue["job_id"],
        attemptId=issue["attempt_id"],
        replayId=issue["replay_id"],
        status=issue["status"],
        failureCode=issue["failure_code"],
        startedAt=issue["started_at"],
        queuedAt=issue["queued_at"],
        claimedAt=issue["claimed_at"],
        claimGeneration=issue["claim_generation"],
        leaseStartedAt=issue["lease_started_at"],
        heartbeatAt=issue["heartbeat_at"],
        leaseExpiresAt=issue["lease_expires_at"],
        leaseState=issue["lease_state"],
        createdAt=issue["created_at"],
        detectedAt=issue["detected_at"],
        ageSeconds=issue["age_seconds"],
        staleAt=issue["stale_at"],
        attemptsUsed=issue["attempts_used"],
        maxAttempts=issue["max_attempts"],
        detectorVersion=issue["detector_version"],
        workflowVersion=issue["workflow_version"],
        recommendedActionCode=issue["recommended_action_code"],
    )


@app.get(
    "/api/comparison-reliability/failures",
    response_model=ReliabilityFailuresResponse,
    dependencies=[Depends(require_permission("reliability.read"))],
)
def get_reliability_failures(
    since: str | None = None,
    until: str | None = None,
    failure_code: str | None = Query(default=None, max_length=120),
    detector_version: str | None = Query(default=None, max_length=120),
    workflow_version: str | None = Query(default=None, max_length=120),
    comparison_id: str | None = Query(default=None, max_length=120),
    limit: int = Query(
        default=comparison_reliability.DEFAULT_LIMIT,
        ge=1,
        le=comparison_reliability.MAX_LIMIT,
    ),
) -> ReliabilityFailuresResponse:
    """Failed and timed-out attempt summaries, newest first. READ-ONLY.

    Windowed on ``started_at``; filters are exact matches. Summaries only — no
    comparison result, no evidence, no exception text.

    This listing requires no recovery evaluation today, so the
    dependency-unavailable branch below is defensive: it guarantees the contract
    holds identically here if this calculation ever grows to need eligibility.
    """
    try:
        report = comparison_reliability.failures(
            since=since,
            until=until,
            failure_code=failure_code,
            detector_version=detector_version,
            workflow_version=workflow_version,
            comparison_id=comparison_id,
            limit=limit,
        )
    except comparison_reliability.ReliabilityQueryError as exc:
        raise _reliability_query_error(exc) from exc
    except comparison_reliability.ReliabilityDataError as exc:
        raise _reliability_data_error(exc) from exc
    except comparison_reliability.ReliabilityStorageUnavailable as exc:
        raise _reliability_storage_unavailable(exc) from exc
    except comparison_reliability.ReliabilityDependencyUnavailable as exc:
        raise _reliability_dependency_error(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return ReliabilityFailuresResponse(
        contractVersion=report["contract_version"],
        generatedAt=report["generated_at"],
        since=report["since"],
        until=report["until"],
        total=report["total"],
        returned=report["returned"],
        truncated=report["truncated"],
        failures=[
            ReliabilityFailureDTO(
                attemptId=item["attempt_id"],
                comparisonId=item["comparison_id"],
                attemptNumber=item["attempt_number"],
                status=item["status"],
                failureCode=item["failure_code"],
                failureSummary=item["failure_summary"],
                detectorVersion=item["detector_version"],
                workflowVersion=item["workflow_version"],
                startedAt=item["started_at"],
                finishedAt=item["finished_at"],
                durationSeconds=item["duration_seconds"],
                replayId=item["replay_id"],
                sourceAttemptId=item["source_attempt_id"],
            )
            for item in report["failures"]
        ],
    )


def _to_governance_dto(record: dict) -> GovernanceEvaluationDTO:
    """Map a stored evaluation onto the allowlist, field by field."""
    return GovernanceEvaluationDTO(
        evaluationId=record.get("evaluation_id", ""),
        comparisonId=record.get("comparison_id", ""),
        policyId=record.get("policy_id", ""),
        policyVersion=record.get("policy_version", ""),
        riskScore=record.get("risk_score", 0.0),
        riskLevel=record.get("risk_level", ""),
        decision=record.get("decision", ""),
        reasonCodes=list(record.get("reason_codes") or []),
        evaluatedAt=record.get("evaluated_at", ""),
        comparisonResultHash=record.get("comparison_result_hash", ""),
        governedResultHash=record.get("governed_result_hash", ""),
        governedResult=record.get("governed_result") or {},
    )


@app.post(
    "/api/comparisons/{comparison_id}/governance",
    response_model=GovernanceEvaluateResponse,
)
def evaluate_comparison_governance(
    comparison_id: str,
    response: Response,
    principal: Annotated[
        access_control.Principal,
        Depends(require_permission("governance.evaluate")),
    ],
) -> GovernanceEvaluateResponse:
    """Evaluate a detected comparison under the comparison risk policy.

    201 + created=true for a new immutable evaluation (held decisions also
    create their single pending review item in the same transaction); 200 +
    created=false for the idempotent replay; 404 unknown comparison or no
    detector result; 409 when the stored result is invalid or the hash is
    stale; safe 500 with correlation id otherwise.
    """
    try:
        record, created = comparison_governance.govern(comparison_id)
    except comparison_governance.GovernanceNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except comparison_governance.GovernanceResultInvalid as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except comparison_store.ComparisonLifecycleError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.status, "message": str(exc)},
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    if created:
        comparison_reliability.log_lifecycle_event(
            comparison_reliability.EVENT_GOVERNANCE_EVALUATED,
            comparison_id=record.get("comparison_id"),
            evaluation_id=record.get("evaluation_id"),
            status=record.get("decision"),
            result_hash=record.get("governed_result_hash"),
            actor_context=_actor_context(principal, "governance.evaluate"),
        )
    return GovernanceEvaluateResponse(
        created=created, evaluation=_to_governance_dto(record)
    )


@app.get(
    "/api/comparisons/{comparison_id}/governance",
    response_model=GovernanceEvaluationDTO,
    dependencies=[Depends(require_permission("governance.read"))],
)
def get_comparison_governance(comparison_id: str) -> GovernanceEvaluationDTO:
    """The evaluation for the current policy id/version and current result
    hash; 404 when none exists yet."""
    try:
        record = comparison_governance.get_current_evaluation(comparison_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No governance evaluation exists for this comparison "
            "under the current policy.",
        )
    return _to_governance_dto(record)


@app.get(
    "/api/comparison-reviews",
    response_model=list[ComparisonReviewSummaryDTO],
    dependencies=[Depends(require_permission("review.read"))],
)
def list_comparison_reviews(
    comparison_id: str | None = None,
) -> list[ComparisonReviewSummaryDTO]:
    """Pending comparison review summaries (newest first). Read-only: no
    approve/reject exists yet, and rows carry no evidence excerpts."""
    try:
        items = comparison_store.list_comparison_reviews(
            comparison_id=comparison_id
        )
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [
        ComparisonReviewSummaryDTO(
            reviewId=item.get("review_id", ""),
            comparisonId=item.get("comparison_id", ""),
            evaluationId=item.get("evaluation_id", ""),
            status=item.get("status", "pending"),
            riskScore=item.get("risk_score", 0.0),
            riskLevel=item.get("risk_level", ""),
            reasonCodes=list(item.get("reason_codes") or []),
            createdAt=item.get("created_at", ""),
        )
        for item in items
    ]


def _to_review_event_dto(event: dict, with_result: bool) -> ComparisonReviewDecisionDTO | ComparisonReviewEventDTO:
    """Map a stored decision event onto the allowlist, field by field."""
    fields = dict(
        eventId=event.get("event_id", ""),
        reviewId=event.get("review_id", ""),
        comparisonId=event.get("comparison_id", ""),
        evaluationId=event.get("evaluation_id", ""),
        action=event.get("action", "approved"),
        reviewerId=event.get("reviewer_id", ""),
        reviewerIdBasis=event.get(
            "actor_auth_method", "legacy_self_asserted"
        ),
        reasonCode=event.get("reason_code", ""),
        reviewerNote=event.get("reviewer_note", ""),
        originalGovernedResultHash=event.get("original_governed_result_hash", ""),
        finalReviewedResultHash=event.get("final_reviewed_result_hash", ""),
        editedChangeIds=[
            edit.get("change_id", "") for edit in event.get("edits") or []
        ],
        createdAt=event.get("created_at", ""),
    )
    if with_result:
        return ComparisonReviewDecisionDTO(
            **fields, reviewedResult=event.get("reviewed_result") or {}
        )
    return ComparisonReviewEventDTO(**fields)


@app.get(
    "/api/comparison-reviews/{review_id}",
    response_model=ComparisonReviewDetailDTO,
    dependencies=[Depends(require_permission("review.read"))],
)
def get_comparison_review(review_id: str) -> ComparisonReviewDetailDTO:
    """One review item with governance metadata, the governed result, and the
    terminal decision (with final reviewed snapshot) when present."""
    try:
        item = comparison_store.get_review_item(review_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Review item not found.")
        evaluation = comparison_store.get_evaluation(item["evaluation_id"])
        events = comparison_store.list_review_events(review_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    decision = None
    if item.get("terminal_event_id") and events:
        decision = _to_review_event_dto(events[-1], with_result=True)
    return ComparisonReviewDetailDTO(
        reviewId=item.get("review_id", ""),
        comparisonId=item.get("comparison_id", ""),
        evaluationId=item.get("evaluation_id", ""),
        status=item.get("status", "pending"),
        riskScore=(evaluation or {}).get("risk_score", 0.0),
        riskLevel=(evaluation or {}).get("risk_level", ""),
        reasonCodes=list((evaluation or {}).get("reason_codes") or []),
        comparisonResultHash=item.get("comparison_result_hash", ""),
        governedResultHash=item.get("governed_result_hash", ""),
        createdAt=item.get("created_at", ""),
        decidedAt=item.get("decided_at"),
        governedResult=(evaluation or {}).get("governed_result") or {},
        decision=decision,
    )


@app.post(
    "/api/comparison-reviews/{review_id}/decision",
    response_model=ComparisonReviewDecisionResponse,
)
def decide_comparison_review(
    review_id: str,
    request: ComparisonReviewDecisionRequest,
    response: Response,
    principal: Annotated[
        access_control.Principal,
        Depends(require_permission("review.decide")),
    ],
) -> ComparisonReviewDecisionResponse:
    """Apply the single terminal decision to a pending comparison review.

    201 new decision; 200 byte-equivalent idempotent replay; 404 unknown
    review; 409 already decided by a different request; 422 invalid reviewer /
    reason / action-edit combination / unsupported edit; safe 500 otherwise.
    """
    reviewer_id = _authenticated_actor(
        request.model_dump()["reviewerId"], principal, field_name="reviewerId"
    )
    try:
        event, created = comparison_review.decide(
            review_id,
            action=request.action,
            reviewer_id=reviewer_id,
            reason_code=request.reasonCode,
            reviewer_note=request.reviewerNote,
            edits=[
                {"change_id": edit.changeId, "summary": edit.summary}
                for edit in (request.edits or [])
            ]
            or None,
            actor_policy_id=principal.policy_id,
            actor_policy_version=principal.policy_version,
            actor_context=_actor_context(principal, "review.decide"),
        )
    except comparison_review.ReviewNotFound:
        raise HTTPException(status_code=404, detail="Review item not found.")
    except comparison_store.ReviewAlreadyDecided as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_already_decided",
                "message": f"review is already {exc.status} by a different "
                "request; decisions are never overwritten",
            },
        ) from exc
    except comparison_review.ReviewDecisionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    return ComparisonReviewDecisionResponse(
        created=created,
        decision=_to_review_event_dto(event, with_result=True),
    )


@app.get(
    "/api/comparison-reviews/{review_id}/events",
    response_model=list[ComparisonReviewEventDTO],
    dependencies=[Depends(require_permission("review.read"))],
)
def list_comparison_review_events(review_id: str) -> list[ComparisonReviewEventDTO]:
    """Append-only decision history, oldest first (currently 0 or 1 terminal
    event; the list shape leaves room for future non-terminal events)."""
    try:
        if comparison_store.get_review_item(review_id) is None:
            raise HTTPException(status_code=404, detail="Review item not found.")
        events = comparison_store.list_review_events(review_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_review_event_dto(event, with_result=False) for event in events]


@app.get(
    "/api/comparisons/{comparison_id}/result",
    dependencies=[Depends(require_permission("comparison.read"))],
)
def get_comparison_result(comparison_id: str) -> dict:
    """Return the persisted, validated comparison.v1 wire document.

    404 when the comparison does not exist or has no detection result yet.
    """
    try:
        stored = comparison_store.get_result(comparison_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="No detection result exists for this comparison.",
        )
    return stored["result"]


def _to_export_summary_dto(record: dict) -> ComparisonExportSummaryDTO:
    """Map a stored export row onto the summary allowlist, field by field.

    Deliberately payload-free: no comparison evidence and no reviewer note
    ever appear in list responses.
    """
    return ComparisonExportSummaryDTO(
        exportId=record.get("export_id", ""),
        exportSchemaVersion=record.get("export_schema_version", ""),
        comparisonId=record.get("comparison_id", ""),
        evaluationId=record.get("evaluation_id", ""),
        reviewId=record.get("review_id"),
        releaseBasis=record.get("release_basis", "returned_by_policy"),
        sourceResultHash=record.get("source_result_hash", ""),
        finalResultHash=record.get("final_result_hash", ""),
        exportPayloadHash=record.get("export_payload_hash", ""),
        createdAt=record.get("created_at", ""),
    )


@app.post(
    "/api/comparisons/{comparison_id}/exports",
    response_model=ComparisonExportCreateResponse,
)
def create_comparison_export(
    comparison_id: str,
    request: ComparisonExportCreateRequest,
    response: Response,
    principal: Annotated[
        access_control.Principal,
        Depends(require_permission("export.create")),
    ],
) -> ComparisonExportCreateResponse:
    """Create (or idempotently return) the release-gated export for one
    governance evaluation.

    201 + created=true for a newly persisted export; 200 + created=false for
    the idempotent replay (the ORIGINAL payload and exported_at are returned);
    404 unknown comparison or evaluation; 409 with a stable reason code when
    the persisted workflow state does not permit release (pending or rejected
    review, stale hashes, mismatched linkage, blocked); safe 500 with a
    correlation id for unexpected storage failures.
    """
    try:
        record, created = comparison_export.export_comparison(
            comparison_id, request.evaluationId.strip()
        )
    except comparison_export.ExportNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except comparison_export.ExportNotEligible as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc

    response.status_code = 201 if created else 200
    if created:
        export_payload = record.get("export") or {}
        comparison_reliability.log_lifecycle_event(
            comparison_reliability.EVENT_EXPORT_CREATED,
            comparison_id=comparison_id,
            export_id=(
                record.get("export_id")
                or export_payload.get("export_id")
                or export_payload.get("exportId")
            ),
            status="created",
            result_hash=(
                record.get("export_payload_hash")
                or export_payload.get("exportPayloadHash")
            ),
            actor_context=_actor_context(principal, "export.create"),
        )
    return ComparisonExportCreateResponse(created=created, export=record["export"])


@app.get(
    "/api/comparisons/{comparison_id}/exports",
    response_model=list[ComparisonExportSummaryDTO],
    dependencies=[Depends(require_permission("export.read"))],
)
def list_comparison_exports(comparison_id: str) -> list[ComparisonExportSummaryDTO]:
    """Export summaries for one comparison, newest first; 404 when the
    comparison does not exist. Summaries only — the full artifact is served
    by GET /api/comparison-exports/{export_id}."""
    try:
        if comparison_store.get_comparison(comparison_id) is None:
            raise HTTPException(status_code=404, detail="Comparison not found.")
        records = comparison_export.list_exports(comparison_id)
    except HTTPException:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    return [_to_export_summary_dto(record) for record in records]


@app.get(
    "/api/comparison-exports/{export_id}",
    dependencies=[Depends(require_permission("export.read"))],
)
def get_comparison_export(export_id: str) -> dict:
    """Return the persisted comparison.export.v1 document verbatim
    (application/json) — that schema IS the API contract for exports. 404
    when no export with this id exists. No file is generated and no
    filesystem path is exposed."""
    try:
        record = comparison_export.get_export(export_id)
    except (sqlite3.Error, OSError) as exc:
        raise _comparison_storage_error(exc) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Export not found.")
    return record["export"]


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Answer a chat message using the existing RAG agent."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    try:
        result = await run_in_threadpool(
            query,
            message,
            _to_agent_history(request.history),
        )
        return ChatResponse(
            answer=result["output"],
            sources=[
                _to_chat_source(source)
                for source in result.get("sources", [])
                if isinstance(source, dict)
            ],
            audit_id=result.get("audit_id"),
            governance_report=result.get("governance_report"),
        )
    except Exception:
        error_id = _new_error_id()
        logger.exception("POST /api/chat failed (error_id=%s)", error_id)
        return JSONResponse(
            status_code=500,
            content={
                "code": SAFE_ERROR_CODE,
                "message": SAFE_ERROR_MESSAGE,
                "error_id": error_id,
            },
        )


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Stream a chat response as newline-delimited JSON events."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    def event_stream():
        try:
            for event in stream_query(message, _to_agent_history(request.history)):
                # The agent yields raw source dicts (absolute `source` path
                # included); re-shape them onto the chat allowlist before they
                # leave the API.
                if event.get("type") == "sources":
                    event = {
                        "type": "sources",
                        "sources": [
                            _to_chat_source(source).model_dump()
                            for source in (event.get("sources") or [])
                            if isinstance(source, dict)
                        ],
                    }
                yield json.dumps(event, default=str) + "\n"
        except Exception:
            error_id = _new_error_id()
            logger.exception(
                "POST /api/chat/stream failed (error_id=%s)", error_id
            )
            yield json.dumps(
                {
                    "type": "error",
                    "code": SAFE_ERROR_CODE,
                    "message": SAFE_ERROR_MESSAGE,
                    "error_id": error_id,
                }
            ) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )
