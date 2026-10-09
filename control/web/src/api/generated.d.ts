// Generated from canonical OpenAPI SHA256 0302a88a0f26555c7af357b10b18e94f22ca668e0e0bcb609c6ae11060c043f0. Do not edit.
import type {ExactNumber} from "./contract-numeric";
export interface paths {
    "/api/artifact-jobs/capabilities": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Capabilities */
        get: operations["getArtifactJobCapabilities"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/artifact-jobs/requests/{request_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Job By Request Id */
        get: operations["getArtifactJobByRequestId"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/artifact-jobs/{job_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Job Status */
        get: operations["getArtifactJobStatus"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/artifact-jobs/{job_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel Job */
        post: operations["cancelArtifactJob"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/artifact-jobs/{job_id}/finalize": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Finalize Job */
        post: operations["finalizeArtifactJob"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/artifact-jobs/{job_id}/inputs/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Upload Input */
        put: operations["uploadArtifactJobInput"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/artifact-jobs/{job_id}/results/{name}/{sha256}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Download Result */
        get: operations["downloadArtifactJobResult"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/artifact-jobs/{job_id}/submit": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Submit Job */
        post: operations["submitArtifactJob"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/cli-token": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cli Token */
        post: operations["downloadCliToken"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Login */
        post: operations["loginBrowser"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Logout */
        post: operations["logoutBrowser"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/session": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Session */
        get: operations["getBrowserSession"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/catalog/managed-recipes/sync-status": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Managed Recipe Catalog Sync Status */
        get: operations["getManagedRecipeCatalogSyncStatus"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cli/contract": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Cli Update Contract */
        get: operations["getCliUpdateContract"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Fleet Status */
        get: operations["getFleetStatus"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/enroll": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Fleet Enroll */
        post: operations["enrollFleetNode"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/enrollments/{grant_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Enrollment */
        get: operations["getFleetEnrollment"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/enrollments/{grant_id}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Revoke Enrollment */
        post: operations["revokeFleetEnrollment"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/locks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Fleet Locks */
        get: operations["getFleetAdmissionLocks"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/stream": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Fleet Event Stream */
        get: operations["streamFleetEvents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/upgrade": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Fleet Upgrade */
        post: operations["upgradeFleet"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/{selector}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Fleet Detail */
        get: operations["getFleetNode"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/{selector}/loginfo": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Fleet Loginfo */
        get: operations["getFleetLogInfo"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/{selector}/re-enroll": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Fleet Reenroll */
        post: operations["reenrollFleetNode"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/{selector}/remove": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Fleet Remove */
        post: operations["removeFleetNode"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/{selector}/rename": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Fleet Rename */
        post: operations["renameFleetNode"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/jobs/{job_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Job View */
        get: operations["getJob"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/jobs/{job_id}/resume": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Resume Job */
        post: operations["resumeJob"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/key": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Gateway Keys */
        get: operations["listGatewayKeys"];
        put?: never;
        /** Create Gateway Key */
        post: operations["createGatewayKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/key/{name}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Revoke Gateway Key */
        post: operations["revokeGatewayKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/key/{name}/roll": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Roll Gateway Key */
        post: operations["rollGatewayKey"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/library": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Model Library */
        get: operations["listModelLibrary"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/operations/{operation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Operation
         * @description Observe a submitted model mutation; any authenticated actor may read it.
         */
        get: operations["getModelOperation"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/operations/{operation_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel Operation */
        post: operations["cancelModelOperation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/requests/{request_key}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Request */
        get: operations["getModelRequest"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/{selector}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Model Detail */
        get: operations["getModel"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/{selector}/download": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Download */
        post: operations["downloadModel"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/{selector}/remove": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Remove */
        post: operations["removeModel"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/model/{selector}/remove-review": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Review Removal */
        get: operations["reviewModelRemoval"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/operations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Operations View */
        get: operations["listOperations"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/operations/{operation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Operation View */
        get: operations["getOperation"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/operations/{operation_id}/evidence": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Evidence */
        get: operations["getOperationEvidence"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/platform": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Platform Observation */
        get: operations["getPlatformObservation"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Profiles */
        get: operations["listProfiles"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/applications/{application_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Profile Application */
        get: operations["getProfileApplication"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/applications/{application_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel Profile Application */
        post: operations["cancelProfileApplication"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/applications/{application_id}/cancellations/{request_key}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Profile Application Cancellation */
        get: operations["getProfileApplicationCancellation"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/{number}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Profile */
        get: operations["getProfile"];
        /** Autosave Profile */
        put: operations["autosaveProfile"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/{number}/definition": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Profile Definition */
        get: operations["getProfileDefinition"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/{number}/endpoints": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Profile Endpoints */
        get: operations["getProfileEndpoints"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/{number}/load": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Load Profile */
        post: operations["loadProfile"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/{number}/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Profile */
        post: operations["previewProfile"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/{number}/progress": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Profile Progress */
        get: operations["getProfileProgress"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/profile/{number}/requests/{request_key}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Profile Application By Request */
        get: operations["getProfileApplicationByRequest"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/installations/{installation_id}/reconcile": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Apply Installation Reconciliation */
        post: operations["applyRecipeInstallationReconciliation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/installations/{installation_id}/reconcile/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Installation Reconciliation */
        post: operations["previewRecipeInstallationReconciliation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/library": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Recipe Library */
        get: operations["listRecipeLibrary"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/operations/{operation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Operation
         * @description Observe a submitted recipe mutation; any authenticated actor may read it.
         */
        get: operations["getRecipeOperation"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/operations/{operation_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel Operation */
        post: operations["cancelRecipeOperation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/operations/{operation_id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Retry Operation */
        post: operations["retryRecipeOperation"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/requests/{request_key}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Request */
        get: operations["getRecipeRequest"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/runs/{run_id}/artifact-jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Jobs */
        get: operations["listArtifactJobsForRun"];
        put?: never;
        /** Create Job */
        post: operations["createArtifactJob"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/update": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Update */
        post: operations["updateRecipes"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/{selector}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Recipe Detail */
        get: operations["getRecipe"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/{selector}/download": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Download */
        post: operations["downloadRecipe"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/{selector}/remove": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Remove */
        post: operations["removeRecipe"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/recipe/{selector}/remove-review": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Review Remove */
        get: operations["reviewRecipeRemoval"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/run-switch/operations/{operation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Run Switch Operation */
        get: operations["getRunSwitchOperation"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** ActivationMarker */
        ActivationMarker: {
            /** Authority Id */
            authority_id: string;
            /** Directory */
            directory: string;
            /** Evidence Set Digest */
            evidence_set_digest: string;
            /** Generation */
            generation: number | ExactNumber;
            /** Litellm Sha256 */
            litellm_sha256: string;
            /** Manifest Sha256 */
            manifest_sha256: string;
            /** Plan Digest */
            plan_digest: string;
            /** Routes Sha256 */
            routes_sha256: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * State
             * @enum {string}
             */
            state: "maintenance" | "published";
        };
        /**
         * AdmissionCode
         * @description The shared admission lock refused because capacity is held by another admission.
         * @enum {string}
         */
        AdmissionCode: "admission.capacity_busy";
        /**
         * AgentClientDecision
         * @description The client's bounded observation decision after a request outcome.
         * @enum {string}
         */
        AgentClientDecision: "retry" | "record" | "exit" | "defer";
        /**
         * AgentDiagnosticOperation
         * @description Stable diagnostic origins for client requests and local preparation.
         * @enum {string}
         */
        AgentDiagnosticOperation: "controller.request" | "workload.preload_memory" | "model.materialization_copy_fallback";
        /**
         * AgentEvidenceCode
         * @description Optional agent evidence that was dropped so the mandatory report is kept.
         *
         *     An agent report carries a mandatory core (identity, lease, capacity, outcome)
         *     and optional evidence (NICs, the NAS route, fabric details, readings,
         *     progress, diagnostics). Invalid optional evidence is never a reason to refuse
         *     the core: the evidence is dropped and one of these words names what was lost,
         *     on the agent that dropped it and on the Controller that received it.
         * @enum {string}
         */
        AgentEvidenceCode: "agent_evidence.claim_hint_dropped" | "agent_evidence.failure_diagnostics_dropped" | "agent_evidence.inventory_fabric_dropped" | "agent_evidence.inventory_nas_route_dropped" | "agent_evidence.inventory_network_dropped" | "agent_evidence.inventory_network_interface_dropped" | "agent_evidence.progress_dropped" | "agent_evidence.telemetry_reading_dropped";
        /**
         * AgentFailureKind
         * @enum {string}
         */
        AgentFailureKind: "temporary-dependency" | "uncertain-effect" | "invalid-authority" | "invalid-contract" | "integrity-failure" | "resource-prerequisite";
        /** AgentFailureResult */
        AgentFailureResult: {
            /** Diagnostic */
            diagnostic?: string | null;
            diagnostics?: components["schemas"]["FailureDiagnostics"] | null;
            /** Error Code */
            error_code?: string | null;
            failure_kind?: components["schemas"]["AgentFailureKind"] | null;
            /** Helper Error Code */
            helper_error_code?: string | null;
            /** Helper Exit Code */
            helper_exit_code?: number | null;
            operation?: components["schemas"]["AgentOperation"] | null;
            package_activation?: components["schemas"]["PackageActivationReceipt"] | null;
            /** Reason */
            reason?: string | null;
            /** Recovery */
            recovery?: string | null;
            /** Retry After Seconds */
            retry_after_seconds?: number | null;
            /** Stage */
            stage?: string | null;
            /** Status */
            status?: "failed" | null;
            /** Summary */
            summary?: string | null;
            /** Uncertain */
            uncertain?: boolean | null;
            wait_reason?: components["schemas"]["WaitReason"] | null;
        };
        /** AgentInstallResult */
        AgentInstallResult: {
            /** Installed Bytes */
            installed_bytes: number;
        };
        /**
         * AgentOperation
         * @enum {string}
         */
        AgentOperation: "runtime.preflight.v1" | "agent.upgrade.v1" | "artifact.distribution.v1" | "recipe.build.v1" | "recipe.build.cleanup.v1" | "recipe.install" | "recipe.start" | "recipe.job.run.v1" | "recipe.stop" | "recipe.uninstall" | "recipe.reconcile";
        /** AgentOperationChange */
        AgentOperationChange: {
            /** Entity Id */
            entity_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            entity_kind: "agent-operation";
            fields: components["schemas"]["AgentOperationPayload"];
            /** Node Id */
            node_id: string;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /** AgentOperationPayload */
        AgentOperationPayload: {
            /** Attempt */
            attempt: number;
            /** Entity Id */
            entity_id: string;
            /**
             * Entity Kind
             * @constant
             */
            entity_kind: "agent-operation";
            /** Kind */
            kind: string;
            /** Node Id */
            node_id: string;
            /** Parent Job Id */
            parent_job_id: string;
            /** State */
            state: string;
        };
        /** AgentPackageSource */
        AgentPackageSource: {
            /**
             * Architecture
             * @constant
             */
            architecture: "linux-arm64";
            /** Build Digest */
            build_digest: string;
            package: components["schemas"]["PackageRollbackSource"];
            /** Package Bytes */
            package_bytes: number;
            /** Package Url */
            package_url: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /**
         * AgentResultState
         * @description The state words of an agent result on the wire.
         *
         *     Unknown effects are observed by the Controller with bounded retries. The
         *     retired operator-wait spelling is adopted only when reading old receipts.
         * @enum {string}
         */
        AgentResultState: "succeeded" | "failed" | "cancelled" | "observing";
        /**
         * AgentTransportKind
         * @description Safe transport classifications; unknown never invents a network cause.
         * @enum {string}
         */
        AgentTransportKind: "timeout" | "connect" | "body" | "protocol" | "unknown";
        /** AgentUpgradeDiagnosticsResponse */
        AgentUpgradeDiagnosticsResponse: {
            expected_identity: components["schemas"]["AgentUpgradeIdentityResponse"];
            /** Failure Details Unavailable */
            failure_details_unavailable: boolean;
            /** Next Action */
            next_action?: string | null;
            /** Operator Summary */
            operator_summary?: string | null;
            /** Targets */
            targets: components["schemas"]["AgentUpgradeTargetDiagnosticsResponse"][];
        };
        /** AgentUpgradeIdentityResponse */
        AgentUpgradeIdentityResponse: {
            /** Binary Digest */
            binary_digest?: string | null;
            /** Build Digest */
            build_digest?: string | null;
            /** Version */
            version?: string | null;
        };
        /**
         * AgentUpgradePackage
         * @description The signed package a rollout installs on every Spark it targets.
         */
        AgentUpgradePackage: {
            /**
             * Architecture
             * @constant
             */
            architecture: "linux-arm64";
            /** Package Bytes */
            package_bytes: number;
            /** Package Sha256 */
            package_sha256: string;
            /** Package Signature */
            package_signature: string;
            /** Package Url */
            package_url: string;
            /** Package Version */
            package_version: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Target Binary Digest */
            target_binary_digest: string;
            /** Target Build Digest */
            target_build_digest: string;
        };
        /**
         * AgentUpgradePayload
         * @description Signed package authority for the current agent upgrade operation.
         */
        AgentUpgradePayload: {
            /**
             * Architecture
             * @constant
             */
            architecture: "linux-arm64";
            /** Package Bytes */
            package_bytes: number;
            /** Package Sha256 */
            package_sha256: string;
            /** Package Signature */
            package_signature: string;
            /** Package Url */
            package_url: string;
            /** Package Version */
            package_version: string;
            rollback: components["schemas"]["PackageRollbackAuthority"];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Source Package Bytes */
            source_package_bytes: number;
            /** Source Package Url */
            source_package_url: string;
            /** Target Binary Digest */
            target_binary_digest: string;
            /** Target Build Digest */
            target_build_digest: string;
        };
        /**
         * AgentUpgradeRepairManifest
         * @description The repair capsule's authority, bound to one Spark and the package.
         */
        AgentUpgradeRepairManifest: {
            /** Authority Sha256 */
            authority_sha256: string;
            /**
             * Kind
             * @constant
             */
            kind: "agent-upgrade-repair";
            /** Node Id */
            node_id: string;
            package: components["schemas"]["AgentUpgradePackage"];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /**
         * AgentUpgradeRequestIntent
         * @description Which Sparks the operator asked for: all of them, or an explicit list.
         */
        AgentUpgradeRequestIntent: {
            /** All */
            all: boolean;
            /** Selectors */
            selectors: string[] | null;
        };
        /**
         * AgentUpgradeResult
         * @description Evidence emitted after the Rust agent reports an exact upgrade.
         */
        AgentUpgradeResult: {
            activation_receipt: components["schemas"]["PackageActivationReceipt"];
            /**
             * Architecture
             * @constant
             */
            architecture: "linux-arm64";
            /** Binary Digest */
            binary_digest: string;
            /** Build Digest */
            build_digest: string;
            /** Package Sha256 */
            package_sha256: string;
            /** Package Version */
            package_version: string;
            /**
             * Status
             * @constant
             */
            status: "upgraded";
        };
        /**
         * AgentUpgradeRolloutPayload
         * @description An upgrade rollout: the package, the order, and each Spark's rollback source.
         */
        AgentUpgradeRolloutPayload: {
            /** Node Order */
            node_order: string[];
            package: components["schemas"]["AgentUpgradePackage"];
            /** @default null */
            repair_manifest?: components["schemas"]["AgentUpgradeRepairManifest"] | null;
            request_intent: components["schemas"]["AgentUpgradeRequestIntent"];
            /** Sources */
            sources: {
                [key: string]: components["schemas"]["AgentPackageSource"];
            };
        };
        /**
         * AgentUpgradeRolloutResult
         * @description What a rollout skipped, and the newer rollout that replaced it.
         */
        AgentUpgradeRolloutResult: {
            /**
             * Skipped
             * @default null
             */
            skipped?: {
                [key: string]: string;
            } | null;
            /**
             * Superseded By
             * @default null
             */
            superseded_by?: string | null;
        };
        /** AgentUpgradeTargetDiagnosticsResponse */
        AgentUpgradeTargetDiagnosticsResponse: {
            /** Attempts */
            attempts: number | ExactNumber;
            /** Node Id */
            node_id: string;
            observed_identity: components["schemas"]["AgentUpgradeIdentityResponse"];
            /** Raw Reason */
            raw_reason?: string | null;
            /** Retry Not Before */
            retry_not_before?: string | null;
            /** Retry Queued */
            retry_queued: boolean;
            /** State */
            state: string;
            /** Target Proven */
            target_proven: boolean;
        };
        /** ApiRuntimeObservation */
        ApiRuntimeObservation: {
            /** Control Contract Sha256 */
            control_contract_sha256: string | null;
            /** Source Sha */
            source_sha: string | null;
        };
        /**
         * ArtifactDistributionPayload
         * @description The complete payload accepted by the artifact transfer operation.
         */
        ArtifactDistributionPayload: {
            /** Plan Digest */
            plan_digest: string;
        };
        /** ArtifactDistributionResult */
        ArtifactDistributionResult: {
            /** Downloaded Bytes */
            downloaded_bytes: number;
        };
        /** ArtifactFileDeclaration */
        ArtifactFileDeclaration: {
            /** Media Type */
            media_type: string;
            /** Name */
            name: string;
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number;
            /** Slot */
            slot: string;
        };
        /** ArtifactInputContract */
        ArtifactInputContract: {
            /** Max Bytes */
            max_bytes: number;
            /** Media Types */
            media_types: string[];
            /** Required */
            required: boolean;
            /** Slots */
            slots: components["schemas"]["ArtifactSlotContract"][];
        };
        /** ArtifactInputProjection */
        ArtifactInputProjection: {
            /** Artifact Key */
            artifact_key: string;
            /** Selection Id */
            selection_id: string;
        };
        /** ArtifactJobCapabilitiesResponse */
        ArtifactJobCapabilitiesResponse: {
            storage: components["schemas"]["ArtifactJobStorageCapabilities"];
            transport: components["schemas"]["ArtifactJobTransportCapabilities"];
        };
        /** ArtifactJobCreate */
        ArtifactJobCreate: {
            /** Inputs */
            inputs?: components["schemas"]["ArtifactFileDeclaration"][];
            /** Interface */
            interface: string;
            output_limits: components["schemas"]["OutputLimits"];
            /** Parameters */
            parameters?: {
                [key: string]: components["schemas"]["ParameterScalar"];
            };
            /** Timeout Seconds */
            timeout_seconds: number;
        };
        /** ArtifactJobListResponse */
        ArtifactJobListResponse: {
            /** Jobs */
            jobs: components["schemas"]["ArtifactJobResponse"][];
        };
        /** ArtifactJobResponse */
        ArtifactJobResponse: {
            /** Cancel Requested At */
            cancel_requested_at?: string | null;
            compiled_contract: components["schemas"]["CompiledArtifactContract"] | null;
            /** Contract Sha256 */
            contract_sha256: string;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Id */
            id: string;
            /** Input Declarations */
            input_declarations: components["schemas"]["ArtifactFileDeclaration"][] | null;
            /** Input Files */
            input_files: components["schemas"]["ArtifactFileDeclaration"][];
            /** Input Manifest Sha256 */
            input_manifest_sha256: string;
            /** Input Total Bytes */
            input_total_bytes: number | ExactNumber;
            /**
             * Interface
             * @enum {string}
             */
            interface: "audio-job" | "video-job" | "image-job" | "mesh-job" | "artifact-job";
            /** Operation Id */
            operation_id?: string | null;
            /** Output Files */
            output_files: components["schemas"]["ArtifactOutputFile"][];
            output_limits: components["schemas"]["OutputLimits"] | null;
            /** Output Manifest Sha256 */
            output_manifest_sha256?: string | null;
            /** Preparation */
            preparation?: ("draft" | "ready") | null;
            result_evidence?: components["schemas"]["ArtifactJobResultEvidence"] | null;
            /** Run Id */
            run_id: string;
            /** State */
            state: ("queued" | "running" | "backoff" | "observing" | "needs-operator" | "succeeded" | "failed" | "cancelled") | null;
            /** Status Reason */
            status_reason?: string | null;
            /** Submit Request Id */
            submit_request_id?: string | null;
            /**
             * Supported Actions
             * @default []
             */
            supported_actions?: "stop"[];
            /** Timeout Seconds */
            timeout_seconds: number;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /**
         * ArtifactJobResultEvidence
         * @description What the Controller knows about how a job ended, to every field.
         */
        ArtifactJobResultEvidence: {
            /** Active Scope May Remain */
            active_scope_may_remain?: boolean | null;
            /** Cancel Actor */
            cancel_actor?: string | null;
            /** Cancel Reason */
            cancel_reason?: string | null;
            /** Cancel Request Id */
            cancel_request_id?: string | null;
            /** Elapsed Milliseconds */
            elapsed_milliseconds?: (number | ExactNumber) | null;
            /** Failure Kind */
            failure_kind?: ("cancellation-stop-uncertain" | "agent-lease-expired") | null;
            /** Late Results Accepted */
            late_results_accepted?: boolean | null;
            /** Peak Memory Bytes */
            peak_memory_bytes?: (number | ExactNumber) | null;
            /** Recoverable */
            recoverable?: boolean | null;
            /** Residue Resolved By */
            residue_resolved_by?: "exact-stop" | null;
        };
        /** ArtifactJobStorageCapabilities */
        ArtifactJobStorageCapabilities: {
            /** In Flight Uploads */
            in_flight_uploads: number | ExactNumber;
            /** Max Stored Bytes */
            max_stored_bytes: number | ExactNumber;
            /** Remaining Bytes */
            remaining_bytes: number | ExactNumber;
            /** Reserved Bytes */
            reserved_bytes: number | ExactNumber;
            /** Used Bytes */
            used_bytes: number | ExactNumber;
        };
        /** ArtifactJobTransportCapabilities */
        ArtifactJobTransportCapabilities: {
            /** Max Input File Bytes */
            max_input_file_bytes: number;
            /** Max Input Files */
            max_input_files: number;
            /** Max Input Total Bytes */
            max_input_total_bytes: number;
            /** Max Output File Bytes */
            max_output_file_bytes: number;
            /** Max Output Files */
            max_output_files: number;
            /** Max Output Total Bytes */
            max_output_total_bytes: number;
            /** Max Timeout Seconds */
            max_timeout_seconds: number;
            /** Reserved Input Names */
            reserved_input_names: string[];
        };
        /**
         * ArtifactLifecycleCode
         * @description Why an artifact (model file, image archive, blob) cannot be removed, referenced or changed right now.
         * @enum {string}
         */
        ArtifactLifecycleCode: "artifact.asset_availability_unknown" | "artifact.deletion_busy" | "artifact.deletion_fence_lost" | "artifact.deletion_in_progress" | "artifact.reference_busy" | "artifact.reference_changed" | "artifact.reference_identity_mismatch" | "artifact.reference_scan_failed" | "artifact.reference_scan_limited" | "artifact.reference_timeout" | "artifact.reference_unavailable" | "artifact.removal_owner_invalid" | "artifact.removal_owner_unresolved";
        /** ArtifactOutputContract */
        ArtifactOutputContract: {
            /** Max Total Bytes */
            max_total_bytes: number;
            /**
             * Path
             * @constant
             */
            path: "/outputs";
            /** Slots */
            slots: components["schemas"]["ArtifactSlotContract"][];
        };
        /** ArtifactOutputFile */
        ArtifactOutputFile: {
            /** Media Type */
            media_type: string;
            /** Name */
            name: string;
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number;
        };
        /** ArtifactOutputLimits */
        ArtifactOutputLimits: {
            /** Allowed Media Types */
            allowed_media_types: string[];
            /** Max File Bytes */
            max_file_bytes: number;
            /** Max Files */
            max_files: number;
            /** Max Total Bytes */
            max_total_bytes: number;
        };
        /**
         * ArtifactPreparation
         * @description The stages of an artifact job before it is submitted.
         *
         *     These are preparation, not execution: a job is ``draft`` while its inputs are
         *     uploaded and ``ready`` once they are complete.  Its lifecycle ``state`` begins
         *     at ``queued`` on submit and is absent until then.  The old spelling kept both
         *     in the one ``state`` word, which is why :func:`legacy_preparation` exists.
         * @enum {string}
         */
        ArtifactPreparation: "draft" | "ready";
        /** ArtifactSlotContract */
        ArtifactSlotContract: {
            /** Description */
            description: string;
            /** Extensions */
            extensions: string[];
            /** Id */
            id: string;
            /** Label */
            label: string;
            /** Max File Bytes */
            max_file_bytes: number;
            /** Max Files */
            max_files: number;
            /** Max Total Bytes */
            max_total_bytes: number;
            /** Media Types */
            media_types: string[];
            /** Min Files */
            min_files: number;
        };
        /**
         * ArtifactStorageImpact
         * @description Byte impact with unknown values preserved as unknown, never guessed.
         */
        ArtifactStorageImpact: {
            /** Artifact Digests */
            artifact_digests?: string[];
            /** Artifact Set Bytes */
            artifact_set_bytes?: (number | ExactNumber) | null;
            /** Artifact Set Sha256 */
            artifact_set_sha256?: string | null;
            /**
             * Copied Bytes
             * @default 0
             */
            copied_bytes?: number | ExactNumber;
            /** Missing Nas Bytes */
            missing_nas_bytes?: (number | ExactNumber) | null;
            /** Missing Spark Bytes */
            missing_spark_bytes?: (number | ExactNumber) | null;
            /** Missing Spark Bytes By Node */
            missing_spark_bytes_by_node?: {
                [key: string]: number | ExactNumber;
            } | null;
            /**
             * Nas Coverage
             * @enum {string}
             */
            nas_coverage: "complete" | "partial" | "unknown";
            /**
             * Reclaimable Bytes
             * @default 0
             */
            reclaimable_bytes?: number | ExactNumber;
            /** Reclaimable Digests */
            reclaimable_digests?: string[];
            /**
             * Reclaimed Bytes
             * @default 0
             */
            reclaimed_bytes?: number | ExactNumber;
            /** Required Bytes */
            required_bytes?: (number | ExactNumber) | null;
            /**
             * Retention
             * @enum {string}
             */
            retention: "retain-cached" | "reclaim-unreferenced";
            /**
             * Reused Bytes
             * @default 0
             */
            reused_bytes?: number | ExactNumber;
            /**
             * Running Coverage
             * @default unknown
             * @enum {string}
             */
            running_coverage?: "complete" | "partial" | "unknown";
            /**
             * Spark Coverage
             * @enum {string}
             */
            spark_coverage: "complete" | "partial" | "unknown";
        };
        /**
         * ArtifactVerificationEvidence
         * @description One node's immutable artifact handoff evidence.
         */
        ArtifactVerificationEvidence: {
            /** Copied Bytes */
            copied_bytes?: (number | ExactNumber) | null;
            /** Diagnostic */
            diagnostic?: string | null;
            /** Downloaded Bytes */
            downloaded_bytes?: (number | ExactNumber) | null;
            /** Error */
            error?: string | null;
            /** Error Code */
            error_code?: string | null;
            /** Failure Kind */
            failure_kind?: string | null;
            /** Node Id */
            node_id: string;
            /** Reason */
            reason?: string | null;
            /**
             * Uncertain
             * @default false
             */
            uncertain?: boolean;
        };
        /**
         * AssetAvailability
         * @description What is known about a model asset on a Spark's disk.
         * @enum {string}
         */
        AssetAvailability: "verified" | "partial" | "missing" | "unknown";
        /** AuthSession */
        AuthSession: {
            /**
             * Expires At
             * Format: date-time
             */
            expires_at: string;
            /**
             * Role
             * @constant
             */
            role: "administrator";
            /** Subject */
            subject: string;
        };
        /**
         * AvailabilityJobPayload
         * @description One image-availability operation: intent, identity, progress and what it holds.
         */
        AvailabilityJobPayload: {
            /**
             * Blockers
             * @default null
             */
            blockers?: components["schemas"]["OperationBlocker"][] | null;
            /** @default null */
            build_dependency?: components["schemas"]["RecipeBuildDependency"] | null;
            /**
             * Build Input Sha256
             * @default null
             */
            build_input_sha256?: string | null;
            /** @default null */
            cancellation?: components["schemas"]["RecipeOperationCancellationResult"] | null;
            /**
             * Claim Owner
             * @default null
             */
            claim_owner?: string | null;
            /**
             * Claim Until
             * @default null
             */
            claim_until?: string | null;
            /**
             * Effective Execution Key
             * @default null
             */
            effective_execution_key?: string | null;
            /** @default null */
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /** Force Rebuild */
            force_rebuild: boolean;
            /**
             * Identity Key
             * @default null
             */
            identity_key?: string | null;
            /** @default null */
            image_reference_intent?: components["schemas"]["RuntimeImageReferenceIntent"] | null;
            /** @default null */
            image_result?: components["schemas"]["RuntimeImageReceipt"] | null;
            /**
             * Kind
             * @constant
             */
            kind: "recipe.image.availability.v2";
            /** @default null */
            model_child?: components["schemas"]["AvailabilityModelChild"] | null;
            /**
             * Model Digest
             * @default null
             */
            model_digest?: string | null;
            /**
             * Prebuilt Pull
             * @default null
             */
            prebuilt_pull?: boolean | null;
            progress: components["schemas"]["OperationProgress"];
            recipe: components["schemas"]["RecipeDefinition"];
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /**
             * Removal Archives
             * @default null
             */
            removal_archives?: string[] | null;
            /**
             * Removal Fence
             * @default null
             */
            removal_fence?: string | null;
            /** Request */
            request: components["schemas"]["RecipeSelectorIntent"] | components["schemas"]["RecipeRevisionIntent"] | components["schemas"]["RecipeRetryIntent"];
            retry: components["schemas"]["AvailabilityRetry"];
            /**
             * Retry After At
             * @default null
             */
            retry_after_at?: string | null;
            runtime: components["schemas"]["AvailabilityRuntime"];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Stage
             * @default null
             */
            stage?: "available" | null;
            /** @default null */
            supersession?: components["schemas"]["AvailabilitySupersession"] | null;
        };
        /**
         * AvailabilityJobResult
         * @description The image an availability operation produced, with its model child.
         */
        AvailabilityJobResult: {
            /**
             * Build Id
             * @default null
             */
            build_id?: string | null;
            /**
             * Build Input Sha256
             * @default null
             */
            build_input_sha256?: string | null;
            /** Image Bytes */
            image_bytes: number | ExactNumber;
            /** Image Digest */
            image_digest: string;
            /**
             * Local Image Config Id
             * @default null
             */
            local_image_config_id?: string | null;
            /** @default null */
            model_child?: components["schemas"]["AvailabilityModelChild"] | null;
            /**
             * Model Digest
             * @default null
             */
            model_digest?: string | null;
            /** Oci Archive Sha256 */
            oci_archive_sha256: string;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /**
         * AvailabilityModelChild
         * @description The model-cache child an availability operation waits on, as last seen.
         */
        AvailabilityModelChild: {
            /**
             * Artifact Set Sha256
             * @default null
             */
            artifact_set_sha256?: string | null;
            /**
             * Artifacts
             * @default null
             */
            artifacts?: components["schemas"]["RecipeImageAvailabilityArtifact"][] | null;
            /** @default null */
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /** Id */
            id: string;
            /**
             * Model Content Digests
             * @default null
             */
            model_content_digests?: string[] | null;
            /**
             * Plan Digest
             * @default null
             */
            plan_digest?: string | null;
            /** @default null */
            progress?: components["schemas"]["OperationProgress"] | null;
            /**
             * Request Key
             * @default null
             */
            request_key?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "backoff" | "observing" | "succeeded" | "failed" | "cancelled";
        };
        /**
         * AvailabilityOperationFailure
         * @description Shared failure wire contract for model and image availability.
         */
        AvailabilityOperationFailure: {
            /** Artifact Key */
            artifact_key?: string | null;
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Free Bytes */
            free_bytes?: (number | ExactNumber) | null;
            /** Log Excerpt */
            log_excerpt?: string | null;
            /** Recovery Actions */
            recovery_actions?: components["schemas"]["AvailabilityRecoveryAction"][];
            /** Required Bytes */
            required_bytes?: (number | ExactNumber) | null;
            /** Retry After Seconds */
            retry_after_seconds?: (number | ExactNumber) | null;
            /** Retry Time */
            retry_time?: string | null;
            /**
             * Retryable
             * @default false
             */
            retryable?: boolean;
            /** Shortfall Bytes */
            shortfall_bytes?: (number | ExactNumber) | null;
        };
        /**
         * AvailabilityRecoveryAction
         * @enum {string}
         */
        AvailabilityRecoveryAction: "retry" | "resume" | "download_again" | "force_rebuild" | "open_model_access" | "configure_hf_token" | "check_access_and_resume" | "free_space" | "inspect";
        /** AvailabilityRetry */
        AvailabilityRetry: {
            /** Automatic Attempts */
            automatic_attempts: number | ExactNumber;
            /** Operator Retries */
            operator_retries: number | ExactNumber;
        };
        /**
         * AvailabilityRuntime
         * @description The runtime projection an availability operation prepares an image for.
         *
         *     The compiled adapter fields appear once the runtime is resolved; an
         *     operation that only reuses a cached image carries the identity subset.
         */
        AvailabilityRuntime: {
            /**
             * Adapter
             * @default null
             */
            adapter?: string | null;
            /**
             * Adapter Version
             * @default null
             */
            adapter_version?: (number | ExactNumber) | null;
            /** Architecture */
            architecture: string;
            /**
             * Arguments
             * @default null
             */
            arguments?: components["schemas"]["RuntimeArgument"][] | null;
            /**
             * Build Input Sha256
             * @default null
             */
            build_input_sha256?: string | null;
            /**
             * Builder Node Id
             * @default null
             */
            builder_node_id?: string | null;
            /**
             * Entrypoint
             * @default null
             */
            entrypoint?: string[] | null;
            /**
             * Environment
             * @default null
             */
            environment?: components["schemas"]["RuntimeEnvironmentEntry"][] | null;
            /**
             * Image
             * @default null
             */
            image?: string | null;
            /**
             * Image Bytes
             * @default null
             */
            image_bytes?: (number | ExactNumber) | null;
            /**
             * Input Intent Sha256
             * @default null
             */
            input_intent_sha256?: string | null;
            /** Interface */
            interface: string;
            /**
             * Placement Environment
             * @default null
             */
            placement_environment?: {
                [key: string]: string;
            } | null;
            /**
             * Recipe Revision Id
             * @default null
             */
            recipe_revision_id?: string | null;
            /** @default null */
            telemetry?: components["schemas"]["RuntimeTelemetryProjection"] | null;
            /**
             * Writable Paths
             * @default null
             */
            writable_paths?: components["schemas"]["WritablePath"][] | null;
        };
        /** AvailabilitySupersession */
        AvailabilitySupersession: {
            /** Code */
            code: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /**
             * Superseded At
             * Format: date-time
             */
            superseded_at: string;
        };
        /**
         * AvailabilityUnknownEnd
         * @description An ended owner whose execution intent could not be re-derived.
         *
         *     Contains no lease, dependency or publication authority. It is evidence of
         *     ending, never an alternate executable preparation payload.
         */
        AvailabilityUnknownEnd: {
            /**
             * Claim Owner
             * @default null
             */
            claim_owner?: null;
            /**
             * Claim Until
             * @default null
             */
            claim_until?: null;
            residue: components["schemas"]["BookkeepingReason"];
        };
        /**
         * BlockerCategory
         * @description The categories of the blocker allowlist (fail-closed raises).
         * @enum {string}
         */
        BlockerCategory: "security-edge" | "input-validation" | "already-retried" | "bookkeeping-debt";
        /**
         * BookkeepingReason
         * @description Why bookkeeping was treated as unknown instead of refused.
         * @enum {string}
         */
        BookkeepingReason: "persisted-state-damaged" | "evidence-mismatch" | "evidence-unavailable" | "row-incomplete";
        /** BooleanParameter */
        BooleanParameter: {
            /** Allowed Values */
            allowed_values?: components["schemas"]["ParameterScalar"][];
            /** Default */
            default: boolean;
            /**
             * Maximum
             * @default null
             */
            maximum?: null;
            /**
             * Minimum
             * @default null
             */
            minimum?: null;
            /** Name */
            name: string;
            /** Pattern */
            pattern?: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "boolean";
        };
        /** BoundedErrorResponse */
        BoundedErrorResponse: {
            context?: components["schemas"]["ErrorContextResponse"] | null;
            /** Detail */
            detail: string;
        };
        /** BuildCleanupPhaseOperation */
        BuildCleanupPhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeBuildCleanupRequest"];
        };
        /** BuildCompatibilityEvidence */
        BuildCompatibilityEvidence: {
            /** Detail */
            detail?: string | null;
            /** Evidence Digest */
            evidence_digest?: string | null;
            /** Expected Architecture */
            expected_architecture: string;
            /** Observed Architecture */
            observed_architecture?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "compatible" | "incompatible" | "unknown";
        };
        /** BuildContext */
        BuildContext: {
            /** Path */
            path: string;
        };
        /** BuildModelArtifactProjection */
        BuildModelArtifactProjection: {
            /** Path */
            path: string;
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number | ExactNumber;
        };
        /** BuildNetwork */
        BuildNetwork: {
            /** Hosts */
            hosts: string[];
        };
        /** BuildPatch */
        BuildPatch: {
            /** Path */
            path: string;
        };
        /** BuildPhaseOperation */
        BuildPhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeBuildRequest"];
        };
        /** BuildResourcesProjection */
        BuildResourcesProjection: {
            /** Cpu Cores */
            cpu_cores: number | ExactNumber;
            /** Download Bytes */
            download_bytes: number | ExactNumber;
            /** Memory Bytes */
            memory_bytes: number | ExactNumber;
            /** Processes */
            processes: number | ExactNumber;
            /** Temporary Bytes */
            temporary_bytes: number | ExactNumber;
            /** Timeout Seconds */
            timeout_seconds: number | ExactNumber;
        };
        /** BuildSecurityProjection */
        BuildSecurityProjection: {
            /** Capabilities */
            capabilities: string[];
        };
        /** BuildSourceEvidence */
        BuildSourceEvidence: {
            /** Detail */
            detail?: string | null;
            /** Source Bundle Sha256 */
            source_bundle_sha256?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "available" | "missing" | "unknown";
        };
        /** CacheManifest */
        CacheManifest: {
            /** Artifacts */
            artifacts: components["schemas"]["CacheManifestArtifact"][];
            /** Model Content Digests */
            model_content_digests: string[];
            /** Model Content Sha256 */
            model_content_sha256: string | null;
            model_definition_ref: components["schemas"]["ModelReference"] | null;
            /** Recipe Revision Sha256 */
            recipe_revision_sha256: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Source Policy
             * @constant
             */
            source_policy: "nas-first";
        };
        /** CacheManifestArtifact */
        CacheManifestArtifact: {
            /** Download Bytes */
            download_bytes: number | ExactNumber;
            /** Id */
            id: string;
            /** Key */
            key: string;
            /** Kind */
            kind: string;
            /** Model Content Sha256 */
            model_content_sha256: string | null;
            /**
             * Parts
             * @default null
             */
            parts?: components["schemas"]["CacheManifestArtifactPart"][] | null;
            /** Path */
            path: string;
            /** Repository */
            repository: string | null;
            /** Revision */
            revision: string | null;
            /** Roles */
            roles: string[];
            /** Sha256 */
            sha256: string;
            /** Source */
            source: string;
        };
        /**
         * CacheManifestArtifactPart
         * @description One source-published piece of a split artifact (joined in list order).
         */
        CacheManifestArtifactPart: {
            /** Download Bytes */
            download_bytes: number | ExactNumber;
            /** Path */
            path: string;
            /** Sha256 */
            sha256: string;
            /** Source */
            source: string;
        };
        /**
         * CacheReferenceReason
         * @description What keeps a cached artifact from being removed.
         * @enum {string}
         */
        CacheReferenceReason: "recipe-installation" | "running-model" | "saved-profile";
        /**
         * CacheRemovalAsset
         * @description One exact cache identity and its owner-reported storage condition.
         */
        CacheRemovalAsset: {
            availability: components["schemas"]["AssetAvailability"];
            /** Available Bytes */
            available_bytes?: (number | ExactNumber) | null;
            /**
             * Disposition
             * @enum {string}
             */
            disposition: "remove" | "retain-shared";
            /** Expected Bytes */
            expected_bytes?: (number | ExactNumber) | null;
            /**
             * Kind
             * @enum {string}
             */
            kind: "model-set" | "model-object" | "runtime-image";
            /** Sha256 */
            sha256: string;
        };
        /** CacheRemovalBlocker */
        CacheRemovalBlocker: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Recovery Actions */
            recovery_actions?: string[];
            /** Retryable */
            retryable: boolean;
        };
        /**
         * CacheRemovalFinding
         * @description One existing owner predicate's exact reference or active-work finding.
         */
        CacheRemovalFinding: {
            /**
             * Asset Kind
             * @enum {string}
             */
            asset_kind: "model-set" | "model-object" | "runtime-image";
            /** Asset Sha256 */
            asset_sha256: string;
            /**
             * Classification
             * @enum {string}
             */
            classification: "saved-reference" | "active-work";
            /** Detail */
            detail: string;
            /** Owner Id */
            owner_id: string;
            /** Owner Kind */
            owner_kind: string;
            /** Reason */
            reason: string;
            /** State */
            state: string;
        };
        /**
         * CacheRemovalReview
         * @description A complete review whose digest is verified during model validation.
         */
        CacheRemovalReview: {
            /**
             * Action
             * @default remove
             * @constant
             */
            action?: "remove";
            /** Active Work */
            active_work: components["schemas"]["CacheRemovalFinding"][];
            /** Assets */
            assets: components["schemas"]["CacheRemovalAsset"][];
            /** Blockers */
            blockers: components["schemas"]["CacheRemovalBlocker"][];
            /** Observed At */
            observed_at: string;
            /** References */
            references: components["schemas"]["CacheRemovalFinding"][];
            /**
             * Resource Kind
             * @enum {string}
             */
            resource_kind: "model" | "recipe";
            /** Review Digest */
            review_digest: string;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Selector */
            selector: string;
            /** Target Identity */
            target_identity: string;
            /** With Model */
            with_model: boolean | null;
        };
        /**
         * CachedResourceEstimate
         * @description What loading the resolved revision needs; ``None`` is unknown.
         */
        CachedResourceEstimate: {
            /** Additional Disk Bytes */
            additional_disk_bytes?: (number | ExactNumber) | null;
            /** Image Bytes */
            image_bytes?: (number | ExactNumber) | null;
            /** Model Bytes */
            model_bytes?: (number | ExactNumber) | null;
            /** Per Spark Memory Bytes */
            per_spark_memory_bytes?: (number | ExactNumber) | null;
        };
        /** CancelRequest */
        CancelRequest: {
            /** Reason */
            reason: string;
        };
        /**
         * CapabilityAvailability
         * @enum {string}
         */
        CapabilityAvailability: "available" | "unavailable";
        /**
         * CapabilityReason
         * @enum {string}
         */
        CapabilityReason: "capability.initializing" | "capability.configuration_invalid" | "capability.storage_unavailable" | "capability.dependency_unavailable";
        /** CapabilityStatus */
        CapabilityStatus: {
            availability: components["schemas"]["CapabilityAvailability"];
            capability: components["schemas"]["ControllerCapability"];
            /** Next Attempt At */
            next_attempt_at?: string | null;
            reason?: components["schemas"]["CapabilityReason"] | null;
        };
        /** CapabilityUnavailableReply */
        CapabilityUnavailableReply: {
            capability: components["schemas"]["ControllerCapability"];
            reason: components["schemas"]["CapabilityReason"];
            /**
             * Retryable
             * @default true
             */
            retryable?: boolean;
        };
        /** CapacityReservations */
        CapacityReservations: {
            /** Disk Bytes */
            disk_bytes: number | ExactNumber;
            /** Gpu Memory Bytes */
            gpu_memory_bytes: number | ExactNumber;
            /** Host Memory Bytes */
            host_memory_bytes: number | ExactNumber;
            /** Port Count */
            port_count: number | ExactNumber;
            /** Unified Memory Bytes */
            unified_memory_bytes: number | ExactNumber;
        };
        /**
         * CatalogCode
         * @description Refusals of the recipe catalog and the recipe library documents.
         * @enum {string}
         */
        CatalogCode: "catalog.actor" | "catalog.candidate_exists" | "catalog.conflict" | "catalog.document_exists" | "catalog.document_invalid" | "catalog.document_missing" | "catalog.head_missing" | "catalog.identities" | "catalog.identity_changed" | "catalog.insufficient_role" | "catalog.invalid_request" | "catalog.model_artifact_missing" | "catalog.model_reference_invalid" | "catalog.model_reference_missing" | "catalog.not_candidate" | "catalog.not_found" | "catalog.recipe_invalid" | "catalog.reference" | "catalog.reference_missing" | "catalog.request_failed" | "catalog.revision_missing" | "catalog.stale_revision" | "catalog.unavailable" | "recipe_library.document_invalid" | "recipe_library.hash_mismatch" | "recipe_library.model_document_invalid" | "recipe_library.package_handle_invalid" | "recipe_library.release_invalid" | "recipe_library.source_invalid" | "recipe_release.signature_invalid";
        /** CatalogProblem */
        CatalogProblem: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Request Id */
            request_id: string;
        };
        /**
         * CatalogSyncCode
         * @description Catalog synchronisation refusals and per-item problems.
         * @enum {string}
         */
        CatalogSyncCode: "catalog.sync_actor_invalid" | "catalog.sync_commit_invalid" | "catalog.sync_failed" | "catalog.sync_identity_changed" | "catalog.sync_in_progress" | "catalog.sync_item_failed" | "catalog.sync_lease_expired" | "catalog.sync_model_failed" | "catalog.sync_not_found" | "catalog.sync_prebuilt_images_failed" | "catalog.sync_preview_changed" | "catalog.sync_repository_changed" | "catalog.sync_request_invalid" | "catalog.sync_request_reused" | "catalog.sync_result_unreadable" | "catalog.sync_revision_changed" | "catalog.sync_state_invalid" | "catalog.sync_trigger_invalid" | "recipe.topology_changed";
        /**
         * CatalogSyncState
         * @description The outcome of a catalog synchronization, as the catalog shows it.
         * @enum {string}
         */
        CatalogSyncState: "syncing" | "current" | "partial" | "failed";
        /**
         * CertificateCode
         * @description Owned certificate issuance admission refusals.
         * @enum {string}
         */
        CertificateCode: "certificate.response_unrepresentable" | "certificate.request_invalid" | "certificate.authentication_refused" | "certificate.binding_refused" | "certificate.source_revoked" | "certificate.source_identity_refused" | "certificate.issuance_in_progress" | "certificate.issuance_unavailable" | "certificate.request_binding_mismatch" | "certificate.serial_already_reserved" | "certificate.serial_already_issued" | "certificate.attempt_superseded" | "certificate.issuance_revoked" | "certificate.rotation_source_revoked";
        /** CertificateIssuanceBinding */
        CertificateIssuanceBinding: {
            /** Csr Sha256 */
            csr_sha256: string;
            /** Generation */
            generation: number | ExactNumber;
            /** Issuer Fingerprint */
            issuer_fingerprint: string;
            /** Node Id */
            node_id: string;
            /** Not After */
            not_after: string;
            /** Not Before */
            not_before: string;
            /** Policy Sha256 */
            policy_sha256: string;
            /** Provisioner Kid */
            provisioner_kid: string;
            /** Provisioner Name */
            provisioner_name: string;
            purpose: components["schemas"]["CertificateIssuancePurpose"];
            /** Request Id */
            request_id: string;
            /** Serial */
            serial: string;
            /** Source Serial */
            source_serial: string | null;
        };
        /**
         * CertificateIssuancePurpose
         * @enum {string}
         */
        CertificateIssuancePurpose: "enrollment" | "rotation";
        /**
         * CertificateJournalState
         * @enum {string}
         */
        CertificateJournalState: "issued" | "pending" | "absent";
        /**
         * CertificateRecordState
         * @enum {string}
         */
        CertificateRecordState: "active" | "staged" | "revoked";
        /**
         * CertificateRequestMode
         * @enum {string}
         */
        CertificateRequestMode: "issue" | "observe";
        /**
         * CertificateRotationState
         * @enum {string}
         */
        CertificateRotationState: "issuing" | "manual-recovery" | "revocation-pending" | "revoked";
        /**
         * CertificateState
         * @description The standing of a node's client certificate, as the fleet projection shows it.
         * @enum {string}
         */
        CertificateState: "valid" | "missing" | "not-yet-valid" | "expired" | "revoked" | "inactive";
        /**
         * CliTokenDownload
         * @description What the browser learns from a token download: when the token stops working.
         *
         *     The token itself is the response body, exact bytes; the expiry travels in the
         *     ``X-Vonk-Token-Expires-At`` header, which the web client reads into this shape.
         */
        CliTokenDownload: {
            /** Expires At */
            expires_at: string;
        };
        /** CliUpdateContract */
        CliUpdateContract: {
            api: components["schemas"]["ApiRuntimeObservation"];
            /** Compatibility Schema Sha256 */
            compatibility_schema_sha256: string;
            /** Expected Worker Contract Sha256 */
            expected_worker_contract_sha256: string | null;
            /**
             * Observed At
             * Format: date-time
             */
            observed_at: string;
            /**
             * Worker Compatibility
             * @enum {string}
             */
            worker_compatibility: "compatible" | "unknown" | "incompatible";
            /** Worker Contract Sha256 */
            worker_contract_sha256: string | null;
            /** Worker Count */
            worker_count: number | ExactNumber;
            /** Worker Issue */
            worker_issue: ("worker-observation-unavailable" | "worker-provenance-unavailable" | "worker-source-mixed" | "worker-contract-mixed" | "api-worker-contract-unavailable" | "worker-contract-incompatible") | null;
            /** Worker Membership Sha256 */
            worker_membership_sha256: string;
            /** Worker Source Sha */
            worker_source_sha: string | null;
        };
        /**
         * ClusterMappingCode
         * @description Refusals of a cluster mapping (recipe-to-Spark assignment) request.
         * @enum {string}
         */
        ClusterMappingCode: "mapping.actor" | "mapping.endpoint_owner" | "mapping.node_count" | "mapping.node_incompatible" | "mapping.node_unknown" | "mapping.nodes_invalid" | "mapping.option_invalid" | "mapping.parameter_type" | "mapping.parameter_unknown" | "mapping.parameter_value" | "mapping.parameters_invalid" | "mapping.ready_immutable" | "mapping.recipe_unresolved" | "mapping.stale_plan" | "mapping.topology_invalid";
        /**
         * CompatibilityIdentity
         * @description Immutable inputs for an exceptional reusable preparation artifact.
         */
        CompatibilityIdentity: {
            /** Hardware Profile Sha256 */
            hardware_profile_sha256?: string | null;
            /** Model Content Sha256 */
            model_content_sha256: string;
            /** Parameters Sha256 */
            parameters_sha256: string;
            /** Recipe Revision Sha256 */
            recipe_revision_sha256: string;
            /** Runtime Image Digest */
            runtime_image_digest: string;
        };
        /**
         * CompatibilityPreparation
         * @description Explicit reusable work that cannot be embedded in the base image.
         */
        CompatibilityPreparation: {
            /** Artifact Sha256 */
            artifact_sha256?: string | null;
            compatibility: components["schemas"]["CompatibilityIdentity"];
            /** Compatibility Key Sha256 */
            compatibility_key_sha256: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "engine-generation" | "jit" | "tuning";
            /** Node Ids */
            node_ids?: string[];
            /** Reason */
            reason?: string | null;
            /**
             * Reusable
             * @constant
             */
            reusable: true;
            /**
             * Stage
             * @enum {string}
             */
            stage: "controller-prepare" | "target-prepare";
            /**
             * State
             * @enum {string}
             */
            state: "unknown" | "missing" | "preparing" | "verifying" | "ready" | "failed" | "unsupported";
        };
        /** CompiledArtifact */
        CompiledArtifact: {
            /** File Id */
            file_id: string;
            model: components["schemas"]["CompiledModelIdentity"];
            mount: components["schemas"]["CompiledArtifactMount"];
            /** Path */
            path: string;
            /** Roles */
            roles: string[];
            /** Selection Id */
            selection_id: string;
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number;
        };
        /**
         * CompiledArtifactContract
         * @description The canonical typed artifact execution contract.
         */
        CompiledArtifactContract: {
            input: components["schemas"]["ArtifactInputContract"];
            /**
             * Interface
             * @enum {string}
             */
            interface: "audio-job" | "video-job" | "image-job" | "mesh-job" | "artifact-job";
            /** Max Timeout Seconds */
            max_timeout_seconds: number;
            output: components["schemas"]["ArtifactOutputContract"];
            output_limits: components["schemas"]["ArtifactOutputLimits"];
            /** Parameters */
            parameters: components["schemas"]["ParameterDefinition"][];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /**
         * CompiledArtifactMount
         * @description A read-only model mount.
         */
        CompiledArtifactMount: {
            /** Target */
            target: string;
        };
        /**
         * CompiledEndpoint
         * @description An OpenAI-compatible endpoint.
         */
        CompiledEndpoint: {
            /** Health Path */
            health_path: string;
            /** Model Aliases */
            model_aliases: string[];
            /** Port */
            port: number;
        };
        /** CompiledEnvironmentEntry */
        CompiledEnvironmentEntry: {
            /** Name */
            name: string;
            /** Value */
            value: string;
        };
        /** CompiledExecutionPlan */
        CompiledExecutionPlan: {
            /** Artifacts */
            artifacts: components["schemas"]["CompiledArtifact"][];
            endpoint: components["schemas"]["CompiledEndpoint"] | null;
            identity: components["schemas"]["CompiledIdentity"];
            job: components["schemas"]["CompiledJob"] | null;
            lifecycle: components["schemas"]["CompiledLifecycle"];
            runtime: components["schemas"]["CompiledRuntime"];
            runtime_image: components["schemas"]["CompiledRuntimeImage"];
            security: components["schemas"]["CompiledSecurity"];
            topology: components["schemas"]["CompiledTopology"];
        };
        /** CompiledIdentity */
        CompiledIdentity: {
            /** Model Artifact Set Sha256 */
            model_artifact_set_sha256: string;
            /** Recipe Revision Sha256 */
            recipe_revision_sha256: string;
        };
        /** CompiledJob */
        CompiledJob: {
            input: components["schemas"]["CompiledJobInput"] | null;
            /**
             * Interface
             * @enum {string}
             */
            interface: "image-job" | "audio-job" | "video-job" | "mesh-job" | "artifact-job";
            /** Timeout Seconds */
            timeout_seconds: number;
        };
        /**
         * CompiledJobInput
         * @description Typed compiled form of the public ``RecipeJobInput`` declaration;
         *     inputs are staged read-only under /inputs.
         */
        CompiledJobInput: {
            /** Max Bytes */
            max_bytes: number;
            /** Media Types */
            media_types: string[];
            /** Required */
            required: boolean;
            /**
             * Slots
             * @default null
             */
            slots?: components["schemas"]["CompiledJobInputSlot"][] | null;
        };
        /**
         * CompiledJobInputSlot
         * @description Agent-side parity model for the public ``RecipeInputSlot`` contract.
         *
         *     The agent protocol wheel intentionally cannot import the public recipe
         *     contracts wheel.  Keep this fixed wire structure in lockstep with that
         *     source contract; engine-specific job parameters remain elsewhere in the
         *     job request and are deliberately extensible.
         */
        CompiledJobInputSlot: {
            /** Description */
            description: string;
            /** Extensions */
            extensions: string[];
            /** Id */
            id: string;
            /** Label */
            label: string;
            /** Max File Bytes */
            max_file_bytes: number;
            /** Max Files */
            max_files: number;
            /** Max Total Bytes */
            max_total_bytes: number;
            /** Media Types */
            media_types: string[];
            /** Min Files */
            min_files: number;
        };
        /** CompiledLifecycle */
        CompiledLifecycle: {
            /** Stop Timeout Seconds */
            stop_timeout_seconds: number;
        };
        /** CompiledModelIdentity */
        CompiledModelIdentity: {
            /** Content Sha256 */
            content_sha256: string;
            /** Publisher */
            publisher: string;
            /** Slug */
            slug: string;
        };
        /** CompiledPlacement */
        CompiledPlacement: {
            /**
             * Endpoint Address
             * Format: ip
             */
            endpoint_address: string | null;
            /**
             * Local Address
             * Format: ip
             */
            local_address: string | null;
            /**
             * Master Address
             * Format: ip
             */
            master_address: string | null;
            /** Master Port */
            master_port: number | null;
            /** Memory Floor Bytes */
            memory_floor_bytes: number;
            /** Port */
            port: number | null;
            /** Rank */
            rank: number | ExactNumber;
            /** Reserved Memory Bytes */
            reserved_memory_bytes: number;
            /** Role */
            role: string;
            /** World Size */
            world_size: number | ExactNumber;
        };
        /** CompiledRuntime */
        CompiledRuntime: {
            /** Argv */
            argv: string[];
            /** Env */
            env: components["schemas"]["CompiledEnvironmentEntry"][];
            /** Executable */
            executable: string;
            placement: components["schemas"]["CompiledPlacement"];
        };
        /**
         * CompiledRuntimeImage
         * @description A runtime image in the Controller's layered store.
         *
         *     ``image_digest`` is its manifest digest and ``oci_layout_sha256`` the same
         *     digest's hex, its address in the store; ``image_bytes`` is the size of its
         *     layers. Sparks pull it into Docker as
         *     ``localhost/vonk/compiled-runtime-<oci_layout_sha256>@<image_digest>``.
         */
        CompiledRuntimeImage: {
            /** Build Id */
            build_id: string;
            /** Image Bytes */
            image_bytes: number;
            /** Image Digest */
            image_digest: string;
            /** Local Image Config Id */
            local_image_config_id: string;
            /** Oci Layout Sha256 */
            oci_layout_sha256: string;
            /** Runtime Interface Label */
            runtime_interface_label: string;
        };
        /**
         * CompiledSecurity
         * @description Per-workload security choices; everything else is a platform constant.
         */
        CompiledSecurity: {
            /** Gpu */
            gpu: boolean;
            /** Mounts */
            mounts: components["schemas"]["CompiledSecurityMount"][];
            /**
             * Network Mode
             * @enum {string}
             */
            network_mode: "none" | "bridge" | "host";
            /** User */
            user: string;
        };
        /**
         * CompiledSecurityMount
         * @description Model and input mounts are read-only; the output mount is writable.
         */
        CompiledSecurityMount: {
            /**
             * Source
             * @enum {string}
             */
            source: "model" | "inputs" | "outputs";
            /** Target */
            target: string;
        };
        /** CompiledTopology */
        CompiledTopology: {
            /** Name */
            name: string;
            /** Node Count */
            node_count: number | ExactNumber;
        };
        /**
         * ConditionalPostStopMemoryCheck
         * @description Fresh inventory and ordinary memory admission required after stops.
         */
        ConditionalPostStopMemoryCheck: {
            /** Stop Run Ids */
            stop_run_ids: string[];
        };
        /**
         * ControllerAssetState
         * @description Availability of one immutable asset in Controller/NAS storage.
         */
        ControllerAssetState: {
            /** Expected Bytes */
            expected_bytes?: (number | ExactNumber) | null;
            /** Missing Bytes */
            missing_bytes?: (number | ExactNumber) | null;
            /** Reason */
            reason?: string | null;
            /**
             * Source
             * @enum {string}
             */
            source: "controller-build" | "nas-cache" | "unknown";
            /**
             * State
             * @enum {string}
             */
            state: "unknown" | "missing" | "preparing" | "verifying" | "ready" | "failed" | "unsupported";
            /** Verified At */
            verified_at?: string | null;
            /**
             * Verified Bytes
             * @default 0
             */
            verified_bytes?: number | ExactNumber;
            /** Verified Sha256 */
            verified_sha256?: string | null;
        };
        /**
         * ControllerCapability
         * @enum {string}
         */
        ControllerCapability: "certificate-authority" | "enrollment-bootstrap" | "host-runtime-authority" | "model-cache" | "runtime-image-storage" | "artifact-storage" | "management-policy" | "fabric-policy" | "agent-presence" | "image-collection" | "route-publisher" | "recipe-routes" | "distribution" | "recipe-library" | "token-auth" | "cursor-auth" | "browser-auth" | "metrics-auth" | "agent-proxy-auth" | "gateway-keys";
        /**
         * ControllerErrorCode
         * @description Generic Controller request and fleet-operation problem codes.
         * @enum {string}
         */
        ControllerErrorCode: "controller.conflict" | "controller.fleet.revocation_uncertain" | "controller.fleet.upgrade_conflict" | "controller.http_" | "controller.internal_error" | "controller.invalid_request" | "controller.not_found" | "controller.rate_limited" | "controller.request_too_large" | "superseded_operation_cancelled" | "controller.timeout" | "controller.unavailable";
        /**
         * DesiredAssignmentState
         * @description What a fleet-profile assignment is asked to become on its Sparks.
         * @enum {string}
         */
        DesiredAssignmentState: "installed" | "running";
        /**
         * DistributedRecoveryMarker
         * @description What a recovery Stop remembers so the Start it interrupted can be re-issued.
         */
        DistributedRecoveryMarker: {
            /**
             * Deadline
             * Format: date-time
             */
            deadline: string;
            /** Failed Rank */
            failed_rank: number;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Start Phases
             * @default null
             */
            start_phases?: components["schemas"]["RecoveryStartItem"][][] | null;
        };
        /**
         * DistributionAssignmentState
         * @description Whether a node may still fetch the artifacts of a distribution assignment.
         * @enum {string}
         */
        DistributionAssignmentState: "active" | "revoked" | "expired";
        /**
         * DistributionCode
         * @description Why a distribution assignment object cannot be served to a Spark.
         * @enum {string}
         */
        DistributionCode: "distribution.assignment_conflict" | "distribution.expired" | "distribution.model_set_identity_unavailable" | "distribution.model_set_mismatch" | "distribution.object_invalid" | "distribution.object_unavailable" | "distribution.runtime_image_mismatch" | "distribution.unassigned" | "distribution.wrong_node";
        /**
         * DistributionJobPayload
         * @description One target-copy child and its exact per-node content binding.
         */
        DistributionJobPayload: {
            /** Assignments */
            assignments: {
                [key: string]: components["schemas"]["NodeDistributionAssignment"];
            };
            /** Cached Nodes */
            cached_nodes: string[];
            /**
             * Phase
             * @enum {string}
             */
            phase: "transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify";
            /** Plan Digest */
            plan_digest: string;
            progress: components["schemas"]["DistributionTransferProgress"];
            /** Target Order */
            target_order: string[];
            /** Target Totals */
            target_totals: {
                [key: string]: number | ExactNumber;
            };
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * DistributionObject
         * @description One model file referenced by an assignment.
         */
        DistributionObject: {
            /** Bytes */
            bytes: number;
            /**
             * Kind
             * @constant
             */
            kind: "model";
            /** Name */
            name: string;
            /** Sha256 */
            sha256: string;
        };
        /** DistributionTransferProgress */
        DistributionTransferProgress: {
            /** Completed Bytes */
            completed_bytes: number | ExactNumber;
            /** Members */
            members: components["schemas"]["RunSwitchMemberReceipt"][];
            /**
             * Phase
             * @enum {string}
             */
            phase: "transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify";
            /** Total Bytes */
            total_bytes: number | ExactNumber;
            /** Total Bytes Known */
            total_bytes_known: boolean;
        };
        /**
         * EffectiveParallelism
         * @description Derived from topology; never an editable settings field.
         */
        EffectiveParallelism: {
            /** Backend */
            backend: string;
            /** Data */
            data: number;
            /** Pipeline */
            pipeline: number;
            /** Tensor */
            tensor: number;
            /** World Size */
            world_size: number;
        };
        /**
         * EffectiveSettingsSelection
         * @description Canonical effective settings bound into the Run/Switch plan digest.
         */
        EffectiveSettingsSelection: {
            /** Change Effects */
            change_effects: {
                [key: string]: "none" | "restart" | "reprepare" | "rebuild" | "reinstall";
            };
            /** Concurrency */
            concurrency?: (number | ExactNumber) | null;
            /** Context Tokens */
            context_tokens?: (number | ExactNumber) | null;
            /** Identity Sha256 */
            identity_sha256: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "generation" | "embedding" | "job";
            /** Knobs */
            knobs?: {
                [key: string]: string | (number | ExactNumber) | boolean | (number | ExactNumber);
            };
            /** Max Batch Tokens */
            max_batch_tokens?: (number | ExactNumber) | null;
            parallelism: components["schemas"]["EffectiveParallelism"];
        };
        /**
         * EmptyJobResult
         * @description A kind that records its outcome elsewhere stores no result document.
         */
        EmptyJobResult: Record<string, never>;
        /**
         * EndpointResponse
         * @description One published alias: clients use `api_base` with `alias` as the model.
         *
         *     `backend_api_base` is the Spark-local serving address LiteLLM routes to.
         *     It is diagnostic only and usually unreachable from a client.
         */
        EndpointResponse: {
            /** Alias */
            alias: string;
            /** Api Base */
            api_base: string;
            /** Backend Api Base */
            backend_api_base: string;
            /** Generation */
            generation: number | ExactNumber;
            /** Node Id */
            node_id: string;
            /** Observed At */
            observed_at: string;
            /** Plan Digest */
            plan_digest: string;
        };
        /**
         * EndpointState
         * @description Whether the endpoint of a fleet-profile assignment can be reached.
         * @enum {string}
         */
        EndpointState: "installed-only" | "not-published-yet" | "published" | "expired" | "withdrawn" | "unavailable";
        /** EnrollmentGrantResponse */
        EnrollmentGrantResponse: {
            /** Ca Fingerprint */
            ca_fingerprint: string;
            /** Controller Address */
            controller_address?: string | null;
            /** Controller Endpoint */
            controller_endpoint: string;
            /** Enrollment Endpoint */
            enrollment_endpoint: string;
            /** Expires At */
            expires_at: string;
            /** Id */
            id: string;
            /**
             * Installer Url
             * @enum {string}
             */
            installer_url: "https://install.vonkforge.ai/spark" | "https://install.vonkforge.ai/dev/spark";
            purpose: components["schemas"]["EnrollmentPurpose"];
            /** Service Hostnames */
            service_hostnames?: string[];
            /** Token */
            token: string;
        };
        /**
         * EnrollmentGrantState
         * @description The standing of an enrollment grant.
         * @enum {string}
         */
        EnrollmentGrantState: "pending" | "expired" | "consumed" | "revoked";
        /** EnrollmentGrantStatus */
        EnrollmentGrantStatus: {
            /** Consumed At */
            consumed_at: string | null;
            /** Display Name */
            display_name: string | null;
            /**
             * Expires At
             * Format: date-time
             */
            expires_at: string;
            /** Id */
            id: string;
            /** Node Id */
            node_id: string | null;
            purpose: components["schemas"]["EnrollmentPurpose"] | null;
            /** Revoked At */
            revoked_at: string | null;
            state: components["schemas"]["EnrollmentGrantState"];
        };
        /** EnrollmentObservationOutcome */
        EnrollmentObservationOutcome: {
            /**
             * Category
             * @constant
             */
            category: "unknown";
            reason: components["schemas"]["WaitReason"];
            state: components["schemas"]["LifecycleState"];
        };
        /**
         * EnrollmentProfileState
         * @enum {string}
         */
        EnrollmentProfileState: "ready";
        /**
         * EnrollmentPurpose
         * @enum {string}
         */
        EnrollmentPurpose: "new-node" | "re-enroll";
        /**
         * EnrollmentRecordState
         * @enum {string}
         */
        EnrollmentRecordState: "ended" | "issuing" | "certificate_issued";
        /** EnrollmentRevocationStatus */
        EnrollmentRevocationStatus: {
            /** Ca Confirmation Complete */
            ca_confirmation_complete: boolean;
            /** Local Denial Complete */
            local_denial_complete: boolean;
        };
        /** EnumParameter */
        EnumParameter: {
            /** Allowed Values */
            allowed_values: components["schemas"]["ParameterScalar"][];
            default: components["schemas"]["ParameterScalar"];
            /**
             * Maximum
             * @default null
             */
            maximum?: null;
            /**
             * Minimum
             * @default null
             */
            minimum?: null;
            /** Name */
            name: string;
            /** Pattern */
            pattern?: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "enum";
        };
        /**
         * ErrorCatalog
         * @description Carrier that publishes the error-category union into every generated surface.
         *
         *     Never sent: the wire schema, OpenAPI and the TypeScript client all emit the
         *     tagged union as one ``OperationError``.
         */
        ErrorCatalog: {
            /** Error */
            error: components["schemas"]["SecurityRefusal"] | components["schemas"]["InvalidRequest"] | components["schemas"]["UnknownError"];
        };
        /**
         * ErrorCategory
         * @description The only three things a lifecycle adapter may raise or report.
         *
         *     ``security-refusal`` and ``invalid-request`` are decided at submit time and
         *     fail closed.  Everything else is ``unknown``: it is observed and reconciled,
         *     never parked.  The blocker allowlist's ``already-retried`` and
         *     ``bookkeeping-debt`` families are both ``unknown`` (see
         *     :func:`error_category_of`).
         * @enum {string}
         */
        ErrorCategory: "security-refusal" | "invalid-request" | "unknown";
        /**
         * ErrorContextResponse
         * @description Safe context shared by public errors and generated clients.
         */
        ErrorContextResponse: {
            /** Code */
            code: string;
            /**
             * Decision
             * @enum {string}
             */
            decision: "retry" | "defer" | "exit";
            /** Endpoint */
            endpoint?: string | null;
            /** Http Status */
            http_status?: number | null;
            /** Operation */
            operation: string;
            /** Request Id */
            request_id?: string | null;
            /**
             * Retryable
             * @default false
             */
            retryable?: boolean;
            /**
             * Source
             * @enum {string}
             */
            source: "remote_rejection" | "transport" | "local_io" | "protocol" | "unknown";
        };
        /** EvidenceContext */
        EvidenceContext: {
            /** Attempt */
            attempt: number;
            /** Kind */
            kind: string;
            /** Node Ids */
            node_ids: string[];
            /** Operation Id */
            operation_id: string;
            /** Rank */
            rank?: number | null;
            /**
             * Source
             * @enum {string}
             */
            source: "agent" | "controller";
            /** Updated At */
            updated_at: string;
        };
        /**
         * FailureCode
         * @description Closed codes of a definite failed outcome reported by the agent.
         * @enum {string}
         */
        FailureCode: "hook.vm_busy" | "hook.vm_process_killed" | "hook.vm_timeout" | "workload.host_memory_exhausted" | "operation_failed" | "operation_cancelled" | "agent_upgrade_failed" | "artifact_distribution_failed" | "recipe_build_failed" | "recipe_job_run_failed" | "recipe_install_failed" | "recipe_start_failed" | "recipe_stop_failed" | "recipe_uninstall_failed" | "runtime_observation_unavailable" | "installation_reconciliation_busy" | "recipe_reconciliation_dependency_unavailable" | "retained_container_foreign";
        /** FailureDiagnostics */
        FailureDiagnostics: {
            /**
             * Category
             * @enum {string}
             */
            category: "platform-policy" | "capacity" | "network" | "digest" | "timeout" | "runtime" | "unknown";
            /** Collected At */
            collected_at: string;
            /** Collector Errors */
            collector_errors: string[];
            /** Phase */
            phase: string;
            /** Preflight */
            preflight: components["schemas"]["FailureProperty"][];
            /** Sandbox */
            sandbox: components["schemas"]["FailureProperty"][];
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version?: number | ExactNumber;
            stderr: components["schemas"]["FailureLogTail"];
            stdout: components["schemas"]["FailureLogTail"];
            /** Storage */
            storage: components["schemas"]["FailureProperty"][];
            /** Versions */
            versions: components["schemas"]["FailureProperty"][];
        };
        /** FailureEvidenceBundle */
        FailureEvidenceBundle: {
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            /** Collected At */
            collected_at: string;
            /** Collector Errors */
            collector_errors: string[];
            context: components["schemas"]["EvidenceContext"];
            /** Detail */
            detail?: string | null;
            diagnostics: components["schemas"]["FailureDiagnostics"];
            /** Error Code */
            error_code: string;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Summary */
            summary: string;
        };
        /** FailureLogTail */
        FailureLogTail: {
            /** Dropped Bytes */
            dropped_bytes: (number | ExactNumber) | null;
            /** Dropped Lines */
            dropped_lines: (number | ExactNumber) | null;
            /** Text */
            text: string;
            /** Truncated */
            truncated: boolean;
        };
        /** FailureProperty */
        FailureProperty: {
            /** Name */
            name: string;
            /** Value */
            value: string;
        };
        /**
         * FailureStage
         * @description The step an operation stopped at: a short, stable, secret-free word.
         * @enum {string}
         */
        FailureStage: "agent-restart" | "agent-upgrade-installed" | "artifact-distribution" | "base-image-import" | "bounded-build-process" | "egress-address" | "egress-image-import" | "egress-network-create" | "egress-readiness" | "egress-service-start" | "helper-runtime-reconciliation" | "helper-runtime-reconciliation-lock" | "image-build" | "image-upload" | "image-verification" | "installation-checkpoint-storage" | "installation-directory" | "installation-metadata" | "installation-path" | "installation-receipt" | "installation-reconciliation-lock" | "installation-removal" | "installation-validation" | "job-cancel-stop" | "job-inputs" | "job-state" | "job-stop" | "lifecycle-metadata" | "model-custody" | "model-materialization" | "observation-identity" | "output-storage" | "retained-container" | "run-storage" | "runtime-adapter" | "runtime-cache" | "runtime-cache-cleanup" | "runtime-metadata" | "runtime-projection" | "source-bundle-fetch" | "stop" | "stop-cleanup" | "stop-metadata" | "stop-plan" | "unknown";
        /** FleetActionResponse */
        FleetActionResponse: {
            /**
             * Action
             * @enum {string}
             */
            action: "enroll" | "re-enroll" | "remove" | "upgrade";
            /** Detail */
            detail?: string | null;
            /** Display Name */
            display_name?: string | null;
            grant?: components["schemas"]["EnrollmentGrantResponse"] | null;
            grant_status?: components["schemas"]["EnrollmentGrantStatus"] | null;
            /** Node Id */
            node_id?: string | null;
            observation?: components["schemas"]["EnrollmentObservationOutcome"] | null;
            /** Operation Id */
            operation_id?: string | null;
            /** Plan Digest */
            plan_digest?: string | null;
            /** Request Key */
            request_key?: string | null;
            revocation?: components["schemas"]["EnrollmentRevocationStatus"] | null;
            /** State */
            state: components["schemas"]["EnrollmentGrantState"] | components["schemas"]["LifecycleState"] | components["schemas"]["ModelCacheOperatorStatus"];
            /** Targets */
            targets?: string[];
        };
        /**
         * FleetAssignmentModelView
         * @description The model an assignment runs, as the operator reads it.
         */
        FleetAssignmentModelView: {
            /** Content Sha256 */
            content_sha256?: string | null;
            /** Name */
            name?: string | null;
            /** Selector */
            selector?: string | null;
            /** State */
            state: string;
            /** Variant */
            variant?: string | null;
        };
        /**
         * FleetAssignmentRecipeView
         * @description The recipe revision an assignment runs, as the operator reads it.
         */
        FleetAssignmentRecipeView: {
            /** Name */
            name?: string | null;
            /** Revision Id */
            revision_id?: string | null;
            /** Selector */
            selector: string;
            /** State */
            state: string;
        };
        /**
         * FleetCacheSummary
         * @description How many assignments of a profile are cached, missing or unknown.
         */
        FleetCacheSummary: {
            /**
             * Cached
             * @default 0
             */
            cached?: number | ExactNumber;
            /**
             * Missing
             * @default 0
             */
            missing?: number | ExactNumber;
            /**
             * Unknown
             * @default 0
             */
            unknown?: number | ExactNumber;
        };
        FleetChange: components["schemas"]["NodeProfileChange"] | components["schemas"]["RecipeInstallationChange"] | components["schemas"]["InstallationNodeChange"] | components["schemas"]["RecipeRunChange"] | components["schemas"]["RunNodeChange"] | components["schemas"]["JobChange"] | components["schemas"]["AgentOperationChange"];
        /** FleetChangeEvent */
        FleetChangeEvent: {
            change: components["schemas"]["FleetChange"];
            /** Event Cursor */
            event_cursor: number | ExactNumber;
            /**
             * Projection Refresh Required
             * @default true
             * @constant
             */
            projection_refresh_required?: true;
        };
        /** FleetEnrollRequest */
        FleetEnrollRequest: {
            /** Name */
            name: string;
            /** Request Key */
            request_key: string;
        };
        /** FleetFrameIssue */
        FleetFrameIssue: {
            /** Budget Bytes */
            budget_bytes: number;
            /** Observed Bytes At Least */
            observed_bytes_at_least: (number | ExactNumber) | null;
            /**
             * Reason Code
             * @enum {string}
             */
            reason_code: "fleet.frame_budget_exceeded" | "fleet.frame_encoding_unavailable" | "fleet.stored_event_payload_unavailable";
        } & (unknown & unknown);
        /** FleetLockHolder */
        FleetLockHolder: {
            /** Holder */
            holder: string;
            /** Namespace */
            namespace: string;
            /** Node Id */
            node_id?: string | null;
            /** Query */
            query?: string | null;
            /** State */
            state?: string | null;
            /** Transaction Age Seconds */
            transaction_age_seconds: number | ExactNumber;
        };
        /** FleetLocksResponse */
        FleetLocksResponse: {
            /** Held */
            held: components["schemas"]["FleetLockHolder"][];
            /** Open Transactions */
            open_transactions: components["schemas"]["FleetOpenTransaction"][];
        };
        /** FleetLogEntry */
        FleetLogEntry: {
            /** Evidence Id */
            evidence_id: string;
            /**
             * Level
             * @enum {string}
             */
            level: "debug" | "info" | "warning" | "error";
            /** Message */
            message: string;
            /**
             * Observed At
             * Format: date-time
             */
            observed_at: string;
            /**
             * Source
             * @enum {string}
             */
            source: "client" | "monitor" | "runtime" | "job";
        };
        /** FleetLogResponse */
        FleetLogResponse: {
            /** Entries */
            entries: components["schemas"]["FleetLogEntry"][];
            /** Follow */
            follow: boolean;
            /** Lines */
            lines: number;
            /** Node Id */
            node_id: string;
            /** Retained */
            retained: boolean;
            /** Since */
            since: string | null;
        };
        /** FleetNode */
        FleetNode: {
            connection: components["schemas"]["NodeConnection"];
            /** Display Name */
            display_name: string;
            /** Hostname */
            hostname: string;
            /** Id */
            id: string;
            /** Installed */
            installed: (components["schemas"]["RecipePresence"] | components["schemas"]["UnavailableRecipePresence"])[];
            inventory: components["schemas"]["InventoryState"] | null;
            /** Ip Address */
            ip_address?: string | null;
            /** Labels */
            labels: {
                [key: string]: string;
            } | null;
            /** Lifecycle */
            lifecycle: string;
            /** Loaded */
            loaded: (components["schemas"]["RunPresence"] | components["schemas"]["UnavailableRunPresence"])[];
            /** Projection Issues */
            projection_issues?: string[] | null;
            reservations: components["schemas"]["CapacityReservations"];
            telemetry: components["schemas"]["TelemetryState"] | null;
            /** Warnings */
            warnings: components["schemas"]["ProjectionReason"][];
        };
        /** FleetNodeIdentity */
        FleetNodeIdentity: {
            /** Display Name */
            display_name: string;
            /** Hostname */
            hostname: string;
            /** Id */
            id: string;
            /** Ip Address */
            ip_address?: string | null;
        };
        /**
         * FleetNodeView
         * @description One Spark of the fleet and whether the profile assigns it.
         */
        FleetNodeView: {
            /** Display Name */
            display_name: string;
            /** Selector */
            selector: string;
            /** State */
            state: string;
        };
        /** FleetOpenTransaction */
        FleetOpenTransaction: {
            /** Application Name */
            application_name?: string | null;
            /** Query */
            query?: string | null;
            /** State */
            state?: string | null;
            /** Transaction Age Seconds */
            transaction_age_seconds: number | ExactNumber;
        };
        /** FleetProfileAdmissionDecision */
        FleetProfileAdmissionDecision: {
            /** Alias */
            alias: string | null;
            /** Allowed */
            allowed: boolean;
            /** Assignment Id */
            assignment_id: string;
            /** Blockers */
            blockers: components["schemas"]["RunSwitchReason"][];
            effective_settings: components["schemas"]["EffectiveSettingsSelection"] | null;
            post_stop_memory_check?: components["schemas"]["ConditionalPostStopMemoryCheck"] | null;
            /** Requirements */
            requirements: components["schemas"]["FleetProfileResourceRequirement"][];
            /** Stop Before Prepare */
            stop_before_prepare: boolean;
            /** Stop Before Transfer */
            stop_before_transfer: boolean;
            /** Stops */
            stops: components["schemas"]["StopImpact"][];
        };
        /**
         * FleetProfileAdoptedApplicationEffect
         * @description An exact continuing executor authorized by the newer reviewed snapshot.
         */
        FleetProfileAdoptedApplicationEffect: {
            /** Application Id */
            application_id: string;
            /** Assignment Ids */
            assignment_ids?: string[];
            /** Node Ids */
            node_ids: string[];
            /** Plan Digest */
            plan_digest: string;
            /** Stops */
            stops?: components["schemas"]["FleetProfileAdoptedStopEffect"][];
            /** Workload Intent Ordinal */
            workload_intent_ordinal: number;
        };
        /**
         * FleetProfileAdoptedStopEffect
         * @description Exact original cleanup, retained by a newer whole-fleet decision.
         */
        FleetProfileAdoptedStopEffect: {
            effect: components["schemas"]["FleetProfileRunEffect"];
            /** Operation Id */
            operation_id: string;
            /** Queue Index */
            queue_index: number | ExactNumber;
            /** Request Key */
            request_key: string;
        };
        /** FleetProfileApplicationCancelRequest */
        FleetProfileApplicationCancelRequest: {
            /** Profile Number */
            profile_number: number;
            /** Request Key */
            request_key: string;
        };
        /**
         * FleetProfileApplicationCancellationIntent
         * @description Durable identity and authority for an explicit or superseding cancel.
         */
        FleetProfileApplicationCancellationIntent: {
            /** Actor */
            actor: string;
            /**
             * Cause
             * @enum {string}
             */
            cause: "operator" | "superseded";
            /** Observation Deadline At */
            observation_deadline_at?: string | null;
            /** Observation Due At */
            observation_due_at?: string | null;
            /** Pending Operation Ids */
            pending_operation_ids?: string[];
            /** Request Key */
            request_key: string;
            /**
             * Requested At
             * Format: date-time
             */
            requested_at: string;
            /**
             * State
             * @default observing
             * @enum {string}
             */
            state?: "observing" | "cancelled";
            /** Successor Application Id */
            successor_application_id?: string | null;
            /** Workload Intent Ordinal */
            workload_intent_ordinal?: number | null;
        };
        /**
         * FleetProfileApplicationCancellationView
         * @description Live projection of completed, pending, and unissued cancelled effects.
         */
        FleetProfileApplicationCancellationView: {
            /** Actor */
            actor: string;
            /** Cancelled Effects */
            cancelled_effects: components["schemas"]["FleetProfileApplicationEffect"][];
            /**
             * Cause
             * @enum {string}
             */
            cause: "operator" | "superseded";
            /** Completed Effects */
            completed_effects: components["schemas"]["FleetProfileApplicationEffect"][];
            /** Deadline At */
            deadline_at?: string | null;
            /** Dependency */
            dependency?: string | null;
            /** Owner */
            owner?: string | null;
            /** Pending Effects */
            pending_effects: components["schemas"]["FleetProfileApplicationEffect"][];
            /** Request Key */
            request_key: string;
            /**
             * Requested At
             * Format: date-time
             */
            requested_at: string;
            /**
             * State
             * @enum {string}
             */
            state: "observing" | "cancelled";
        };
        /**
         * FleetProfileApplicationEffect
         * @description One exact profile or child effect in the cancellation receipt.
         */
        FleetProfileApplicationEffect: {
            /** Effect Id */
            effect_id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "profile-step" | "run" | "install" | "stop" | "cleanup" | "agent-operation";
            /** Label */
            label: string;
            /** Operation Id */
            operation_id?: string | null;
            /**
             * Outcome
             * @enum {string}
             */
            outcome: "succeeded" | "failed" | "cancelled" | "pending" | "not-issued";
        };
        /**
         * FleetProfileApplicationProgress
         * @description Typed progress tree persisted with every profile application.
         */
        FleetProfileApplicationProgress: {
            /**
             * Admission Attempt
             * @default 0
             */
            admission_attempt?: number | ExactNumber;
            /**
             * Admission Pending
             * @default false
             */
            admission_pending?: boolean;
            /** Admission Retry At */
            admission_retry_at?: string | null;
            /**
             * Attempt
             * @default 1
             */
            attempt?: number | ExactNumber;
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            cancellation?: components["schemas"]["FleetProfileApplicationCancellationIntent"] | null;
            child_progress?: components["schemas"]["FleetProfileChildProgress"] | null;
            /** Child Source */
            child_source?: "switch-adapter" | null;
            /**
             * Completed Steps
             * @default 0
             */
            completed_steps?: number;
            /** Current Label */
            current_label?: string | null;
            /** Effects */
            effects?: components["schemas"]["FleetProfileEffectProgress"][];
            intended_profile?: components["schemas"]["FleetProfileIntendedConfiguration"] | null;
            /** Operation Kind */
            operation_kind?: "fleet-profile.apply" | null;
            /** Retry Due At */
            retry_due_at?: string | null;
            /** Retry Of Application Id */
            retry_of_application_id?: string | null;
            /** Step Results */
            step_results?: {
                [key: string]: components["schemas"]["FleetProfileStepResult"];
            };
            /** Storage Wait Since */
            storage_wait_since?: string | null;
            supersede_code?: components["schemas"]["SupersedeCode"] | null;
            /** Superseded By */
            superseded_by?: string | null;
            switch_adapter?: components["schemas"]["FleetProfileSwitchAdapterState"] | null;
            /**
             * Total Steps
             * @default 0
             */
            total_steps?: number;
            /** Workload Intent Ordinal */
            workload_intent_ordinal?: number | null;
        };
        /**
         * FleetProfileApplicationProjectionIssue
         * @description Historical state is retained while its metadata cannot be verified.
         */
        FleetProfileApplicationProjectionIssue: {
            /**
             * Code
             * @constant
             */
            code: "profile.application_intent.invalid";
            /** Detail */
            detail: string;
            /**
             * Observation
             * @default unknown
             * @constant
             */
            observation?: "unknown";
        };
        /**
         * FleetProfileApplicationResult
         * @description Terminal result for one profile application.
         */
        FleetProfileApplicationResult: {
            /** Changed */
            changed: boolean;
            /** Completed Steps */
            completed_steps: number;
        };
        /** FleetProfileApplicationView */
        FleetProfileApplicationView: {
            /**
             * Attempt
             * @default 1
             */
            attempt?: number | ExactNumber;
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            cancellation?: components["schemas"]["FleetProfileApplicationCancellationView"] | null;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Current Operation Id */
            current_operation_id: string | null;
            /** Current Step */
            current_step: number;
            /** Id */
            id: string;
            /** Next Attempt At */
            next_attempt_at?: string | null;
            /** Plan Digest */
            plan_digest: string;
            /** Profile Digest */
            profile_digest: string;
            /** Profile Id */
            profile_id: string;
            progress: components["schemas"]["FleetProfileApplicationProgress"];
            projection_issue?: components["schemas"]["FleetProfileApplicationProjectionIssue"] | null;
            reason_code?: components["schemas"]["SupersedeCode"] | null;
            /** Request Key */
            request_key: string;
            result: components["schemas"]["FleetProfileApplicationResult"] | null;
            /** Retry Of Application Id */
            retry_of_application_id?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "needs-operator" | "succeeded" | "failed" | "cancelled" | "superseded";
            /** Status Reason */
            status_reason: string | null;
            /** Superseded By */
            superseded_by?: string | null;
            /** Total Steps */
            total_steps: number;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** FleetProfileAssignment */
        FleetProfileAssignment: {
            /** Alias */
            alias?: string | null;
            desired_state: components["schemas"]["DesiredAssignmentState"];
            /** Id */
            id: string;
            /** Model Title */
            model_title?: string | null;
            /** Nodes */
            nodes: components["schemas"]["FleetProfileNode"][];
            /** Option Choices */
            option_choices?: {
                [key: string]: string;
            };
            /** Recipe Id */
            recipe_id: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Recipe Title */
            recipe_title: string;
            /** Topology Name */
            topology_name: string;
        };
        /** FleetProfileAssignmentAssessment */
        FleetProfileAssignmentAssessment: {
            assessment: components["schemas"]["RunSwitchAssessment"];
            /** Assignment Id */
            assignment_id: string;
        };
        /** FleetProfileAssignmentFailure */
        FleetProfileAssignmentFailure: {
            /** Assignment Id */
            assignment_id?: string | null;
            /** Operation Id */
            operation_id?: string | null;
            /** Queue Index */
            queue_index?: (number | ExactNumber) | null;
            /** Reason */
            reason: string;
            /**
             * Terminal
             * @default false
             */
            terminal?: boolean;
        };
        /**
         * FleetProfileAssignmentInput
         * @description Permissive autosaved recipe choice, not an execution assignment.
         *
         *     A logical model variant is selected by its canonical identity.  The
         *     content digest is resolved from that identity for a load and is never a
         *     profile-pinned execution revision.  Topology/rank validation belongs to
         *     preview/load, so incomplete distributed drafts can be saved.
         */
        FleetProfileAssignmentInput: {
            /** Assignment Name */
            assignment_name?: string | null;
            /** @default running */
            desired_state?: components["schemas"]["DesiredAssignmentState"];
            /** Model Variant */
            model_variant?: string | null;
            /** Option Choices */
            option_choices?: {
                [key: string]: string;
            };
            /** Recipe Selector */
            recipe_selector: string;
            /** Spark Ids */
            spark_ids: string[];
        };
        /** FleetProfileAssignmentPreparation */
        FleetProfileAssignmentPreparation: {
            /** Assignment Id */
            assignment_id: string;
            preparation: components["schemas"]["RolloutPreparation"];
        };
        /** FleetProfileAssignmentPreview */
        FleetProfileAssignmentPreview: {
            /** Actions */
            actions: ("switch" | "keep" | "adopt")[];
            /** Assignment Id */
            assignment_id: string;
            current_state: components["schemas"]["ObservedAssignmentState"];
            desired_state: components["schemas"]["DesiredAssignmentState"];
            /** Node Ids */
            node_ids: string[];
            /** Option Choices */
            option_choices?: {
                [key: string]: string;
            };
            /** Reasons */
            reasons: components["schemas"]["FleetProfileReason"][];
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Recipe Title */
            recipe_title: string;
        };
        /**
         * FleetProfileAssignmentView
         * @description Canonical read projection of one logical profile assignment.
         */
        FleetProfileAssignmentView: {
            /** Assigned Sparks */
            assigned_sparks: number;
            /** Display Name */
            display_name: string;
            model: components["schemas"]["FleetAssignmentModelView"];
            /**
             * Observed State
             * @default Not loaded
             */
            observed_state?: string;
            /** Option Choices */
            option_choices?: {
                [key: string]: string;
            };
            recipe: components["schemas"]["FleetAssignmentRecipeView"];
            /** Recipe Id */
            recipe_id?: string | null;
            /** Recipe Selector */
            recipe_selector: string;
            recipe_update?: components["schemas"]["RecipeUpdateNotice"] | null;
            /** Required Sparks */
            required_sparks?: number | null;
            resources?: components["schemas"]["CachedResourceEstimate"];
            /** Selector */
            selector: string;
            /** Spark Ids */
            spark_ids: string[];
        };
        /**
         * FleetProfileChildProgress
         * @description Typed progress emitted by the profile-owned Run switch adapter.
         */
        FleetProfileChildProgress: {
            /** Bytes */
            bytes?: (number | ExactNumber) | null;
            /** Node Ids */
            node_ids?: string[];
            operation?: components["schemas"]["OperationProgress"] | null;
            /**
             * Phase
             * @enum {string}
             */
            phase: "model-download" | "container-download" | "container-build" | "target-copy" | "runtime-install" | "start" | "final-verify" | "transfer" | "verify" | "prepare" | "cleanup" | "stop" | "uninstall" | "final_verify";
            /** Start Deadline */
            start_deadline?: string | null;
            /** Startup Budget Seconds */
            startup_budget_seconds?: (number | ExactNumber) | null;
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
        };
        /** FleetProfileCompatibilityDecision */
        FleetProfileCompatibilityDecision: {
            /** Artifact Sha256 */
            artifact_sha256: string | null;
            compatibility: components["schemas"]["CompatibilityIdentity"];
            /**
             * Kind
             * @enum {string}
             */
            kind: "engine-generation" | "jit" | "tuning";
            /** Node Ids */
            node_ids: string[];
            /** Ready */
            ready: boolean;
            /**
             * Stage
             * @enum {string}
             */
            stage: "controller-prepare" | "target-prepare";
        };
        /**
         * FleetProfileDefinition
         * @description Saved authoring intent, independent of execution and cache projections.
         */
        FleetProfileDefinition: {
            /** Assignments */
            assignments?: components["schemas"]["FleetProfileAssignmentInput"][];
            /**
             * Description
             * @default
             */
            description?: string;
            /**
             * Favorite
             * @default false
             */
            favorite?: boolean;
            /**
             * Installation Policy
             * @default keep-cached
             * @enum {string}
             */
            installation_policy?: "keep-cached" | "exact";
            /** Labels */
            labels?: {
                [key: string]: string;
            };
            /**
             * Name
             * @default Default
             */
            name?: string;
        };
        /** FleetProfileDefinitionView */
        FleetProfileDefinitionView: {
            definition: components["schemas"]["FleetProfileDefinition"] | null;
            /** Id */
            id: string | null;
            /** Number */
            number: number;
            projection_issue?: components["schemas"]["SavedProfileProjectionIssue"] | null;
            /** Revision */
            revision: number;
        };
        /**
         * FleetProfileEffectProgress
         * @description Observational receipt for one immutable accepted queue effect.
         */
        FleetProfileEffectProgress: {
            /** Application Id */
            application_id: string;
            /** Effect Id */
            effect_id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "install" | "run" | "stop" | "cleanup";
            /** Node Ids */
            node_ids: string[];
            /** Operation Id */
            operation_id?: string | null;
            /** Original Operation Id */
            original_operation_id?: string | null;
            /** Plan Digest */
            plan_digest: string;
            progress?: components["schemas"]["RunSwitchProgress"] | null;
            /** Queue Index */
            queue_index: number | ExactNumber;
            /** Request Key */
            request_key: string;
            result?: components["schemas"]["FleetProfileSwitchChildResult"] | null;
            /**
             * State
             * @enum {string}
             */
            state: "not-issued" | "pending" | "succeeded" | "failed" | "cancelled" | "unknown";
            stop_effect?: components["schemas"]["FleetProfileRunEffect"] | null;
            /** Target Id */
            target_id: string;
            /** Workload Intent Ordinal */
            workload_intent_ordinal: number | ExactNumber;
        };
        /**
         * FleetProfileEffects
         * @description Identified live effects, including complete distributed membership.
         */
        FleetProfileEffects: {
            /** Adopted */
            adopted?: components["schemas"]["FleetProfileAdoptedApplicationEffect"][];
            /** Installations */
            installations: components["schemas"]["FleetProfileInstallationEffect"][];
            /** Runs */
            runs: components["schemas"]["FleetProfileRunEffect"][];
            /** Superseded */
            superseded: components["schemas"]["FleetProfilePendingEffect"][];
        };
        /** FleetProfileEndpointAssignmentView */
        FleetProfileEndpointAssignmentView: {
            /** Alias */
            alias?: string | null;
            /** Assignment Id */
            assignment_id: string;
            desired_state: components["schemas"]["DesiredAssignmentState"];
            endpoint?: components["schemas"]["EndpointResponse"] | null;
            /** Recipe Title */
            recipe_title: string;
            state: components["schemas"]["EndpointState"];
        };
        /**
         * FleetProfileEndpointProjectionIssue
         * @description Safe diagnostic for immutable profile history that cannot be read.
         */
        FleetProfileEndpointProjectionIssue: {
            /**
             * Code
             * @constant
             */
            code: "profile.application_intent.invalid";
            /** Detail */
            detail: string;
        };
        /** FleetProfileEndpointsView */
        FleetProfileEndpointsView: {
            /** Application Id */
            application_id?: string | null;
            /** Application State */
            application_state?: ("queued" | "running" | "needs-operator" | "succeeded" | "failed" | "cancelled" | "superseded") | null;
            /** Assignments */
            assignments: components["schemas"]["FleetProfileEndpointAssignmentView"][] | null;
            /** Number */
            number: number;
            /**
             * Observed At
             * Format: date-time
             */
            observed_at: string;
            /** Profile Id */
            profile_id?: string | null;
            projection_issue?: components["schemas"]["FleetProfileEndpointProjectionIssue"] | null;
        };
        /** FleetProfileInput */
        FleetProfileInput: {
            /** Assignments */
            assignments?: components["schemas"]["FleetProfileAssignmentInput"][];
            /**
             * Description
             * @default
             */
            description?: string;
            /**
             * Expected Revision
             * @default 0
             */
            expected_revision?: number;
            /**
             * Favorite
             * @default false
             */
            favorite?: boolean;
            /**
             * Installation Policy
             * @default keep-cached
             * @enum {string}
             */
            installation_policy?: "keep-cached" | "exact";
            /** Labels */
            labels?: {
                [key: string]: string;
            };
            /**
             * Name
             * @default Default
             */
            name?: string;
        };
        /** FleetProfileInstallationEffect */
        FleetProfileInstallationEffect: {
            /**
             * Action
             * @enum {string}
             */
            action: "keep" | "remove";
            /** Installation Id */
            installation_id: string;
            /** Node Ids */
            node_ids: string[];
        };
        /**
         * FleetProfileIntendedConfiguration
         * @description Immutable desired configuration captured when execution is admitted.
         */
        FleetProfileIntendedConfiguration: {
            /** Assignments */
            assignments: components["schemas"]["FleetProfileAssignment"][];
            /**
             * Installation Policy
             * @enum {string}
             */
            installation_policy: "keep-cached" | "exact";
            /** Profile Digest */
            profile_digest: string;
            /** Reviewed Application Id */
            reviewed_application_id: string;
            /** Reviewed Plan Digest */
            reviewed_plan_digest: string;
            scope: components["schemas"]["FleetProfileScope"];
        };
        /** FleetProfileList */
        FleetProfileList: {
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            /** Profiles */
            profiles: (components["schemas"]["FleetProfileView"] | components["schemas"]["UnavailableFleetProfileView"])[];
        };
        /** FleetProfileLoadRequest */
        FleetProfileLoadRequest: {
            /** Request Key */
            request_key: string;
            review?: components["schemas"]["FleetProfileLoadReview"] | null;
        };
        /** FleetProfileLoadReview */
        FleetProfileLoadReview: {
            /** Effects Digest */
            effects_digest: string;
        };
        /**
         * FleetProfileNode
         * @description One rank of a profile assignment, the same shape a Spark group names it.
         */
        FleetProfileNode: {
            /**
             * Endpoint Owner
             * @default false
             */
            endpoint_owner?: boolean;
            /** Node Id */
            node_id: string;
            /** Rank */
            rank: number;
            /** Role */
            role: string;
        };
        /** FleetProfilePendingEffect */
        FleetProfilePendingEffect: {
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "job" | "profile-application";
            /** Node Ids */
            node_ids: string[];
        };
        /** FleetProfilePlanStep */
        FleetProfilePlanStep: {
            /** Index */
            index: number;
            /**
             * Kind
             * @enum {string}
             */
            kind: "switch" | "prepare";
            /** Label */
            label: string;
            /** Node Ids */
            node_ids?: string[];
        };
        /** FleetProfilePlanSummary */
        FleetProfilePlanSummary: {
            /** Already Correct */
            already_correct: number;
            /** Blockers */
            blockers: number;
            /** Builds */
            builds: number;
            /** Distributions */
            distributions: number;
            /** Installs */
            installs: number;
            /** Placements */
            placements: number;
            /** Starts */
            starts: number;
            /** Stops */
            stops: number;
            /** Uninstalls */
            uninstalls: number;
        };
        /**
         * FleetProfilePreparationDecision
         * @description Exact assets and reuse decisions; byte counters are observations only.
         */
        FleetProfilePreparationDecision: {
            /** Assignment Id */
            assignment_id: string;
            /** Blockers */
            blockers: components["schemas"]["PreparationReason"][];
            /** Exceptions */
            exceptions: components["schemas"]["FleetProfileCompatibilityDecision"][];
            /** Image Controller Ready */
            image_controller_ready: boolean;
            /** Image Reuse Node Ids */
            image_reuse_node_ids: string[];
            model: components["schemas"]["ModelArtifactIdentity"];
            /** Model Complete */
            model_complete: boolean;
            /** Model Controller Ready */
            model_controller_ready: boolean;
            /** Model Reuse Node Ids */
            model_reuse_node_ids: string[];
            runtime_image: components["schemas"]["RuntimeImageIdentity"];
        };
        /** FleetProfilePreview */
        FleetProfilePreview: {
            /** Admission Decisions */
            admission_decisions: components["schemas"]["FleetProfileAdmissionDecision"][];
            /** Allowed */
            allowed: boolean;
            /** Assessments */
            assessments: components["schemas"]["FleetProfileAssignmentAssessment"][];
            /** Assignments */
            assignments: components["schemas"]["FleetProfileAssignmentPreview"][];
            effects: components["schemas"]["FleetProfileEffects"];
            /** Effects Digest */
            effects_digest: string;
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            /** Plan Digest */
            plan_digest: string;
            /** Preparation Decisions */
            preparation_decisions: components["schemas"]["FleetProfilePreparationDecision"][];
            /** Preparation Steps */
            preparation_steps?: components["schemas"]["FleetProfilePlanStep"][];
            /** Preparations */
            preparations?: components["schemas"]["FleetProfileAssignmentPreparation"][];
            profile_definition: components["schemas"]["FleetProfileDefinition"] | null;
            /** Profile Digest */
            profile_digest: string;
            /** Profile Id */
            profile_id: string;
            /** Profile Name */
            profile_name: string;
            /** Profile Revision */
            profile_revision: number | null;
            /** Reasons */
            reasons: components["schemas"]["FleetProfileReason"][];
            /** Resolved Assignments */
            resolved_assignments: components["schemas"]["FleetProfileAssignment"][];
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            scope: components["schemas"]["FleetProfileScopePreview"];
            /** Steps */
            steps: components["schemas"]["FleetProfilePlanStep"][];
            summary: components["schemas"]["FleetProfilePlanSummary"];
            /**
             * Waits For Preparation
             * @default false
             */
            waits_for_preparation?: boolean;
        };
        /** FleetProfileReason */
        FleetProfileReason: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /**
             * Severity
             * @enum {string}
             */
            severity: "info" | "warning" | "error";
        };
        /**
         * FleetProfileResourceRequirement
         * @description Stable demand and eligibility, separate from observed free capacity.
         */
        FleetProfileResourceRequirement: {
            /** Allowed */
            allowed: boolean;
            /** Disk Required Bytes */
            disk_required_bytes: (number | ExactNumber) | null;
            /** Memory Floor Bytes */
            memory_floor_bytes: (number | ExactNumber) | null;
            /** Memory Kind */
            memory_kind: ("unified" | "host" | "accelerator") | null;
            /** Memory Pool */
            memory_pool: ("shared" | "separate") | null;
            /** Memory Required Bytes */
            memory_required_bytes: (number | ExactNumber) | null;
            /** Node Id */
            node_id: string;
            /** Ports Required */
            ports_required: number[];
            resource_demand: components["schemas"]["ResourceDemandEvidence"] | null;
        };
        /** FleetProfileRunEffect */
        FleetProfileRunEffect: {
            /**
             * Action
             * @enum {string}
             */
            action: "keep" | "stop";
            /** Alias */
            alias: string;
            /** Installation Id */
            installation_id: string;
            /** Node Ids */
            node_ids: string[];
            profile_stop_scope?: components["schemas"]["RunSwitchProfileStopScope"] | null;
            /** Run Id */
            run_id: string;
        };
        /**
         * FleetProfileScope
         * @description Frozen complete fleet boundary for a single execution plan.
         *
         *     User profiles do not author this field.  It is captured from the enrolled
         *     roster when preview/load admits an operation and is retained so a running
         *     operation cannot silently expand or shrink with fleet membership changes.
         */
        FleetProfileScope: {
            /** Node Ids */
            node_ids: string[];
        };
        /** FleetProfileScopePreview */
        FleetProfileScopePreview: {
            /** Idle Node Ids */
            idle_node_ids?: string[];
            /** Node Ids */
            node_ids: string[];
        };
        /**
         * FleetProfileStepResult
         * @description Result receipt for one completed profile plan step.
         */
        FleetProfileStepResult: {
            /**
             * Kind
             * @constant
             */
            kind: "switch";
            /** Operation Id */
            operation_id: string;
            /** Result */
            result?: components["schemas"]["FleetProfileSwitchChildResult"] | components["schemas"]["FleetProfileSwitchAdapterResult"] | components["schemas"]["FleetProfileVerificationResult"] | null;
        };
        /**
         * FleetProfileSwitchAdapterResult
         * @description Complete result of reconciling every assignment in a profile scope.
         */
        FleetProfileSwitchAdapterResult: {
            /** Assignment Ids */
            assignment_ids: string[];
            /** Children */
            children: components["schemas"]["FleetProfileSwitchChildState"][];
        };
        /**
         * FleetProfileSwitchAdapterState
         * @description Persisted state used to resume a profile switch after restart.
         */
        FleetProfileSwitchAdapterState: {
            /** Actor */
            actor: string;
            /** Assignment Failures */
            assignment_failures?: components["schemas"]["FleetProfileAssignmentFailure"][];
            /** Assignment Ids */
            assignment_ids: string[];
            /** Assignments */
            assignments: components["schemas"]["FleetProfileAssignment"][];
            /** Child Id */
            child_id: string;
            child_progress?: components["schemas"]["FleetProfileChildProgress"] | null;
            /** Children */
            children?: components["schemas"]["FleetProfileSwitchChildState"][];
            /** Observation Deadline At */
            observation_deadline_at?: string | null;
            /** Observation Due At */
            observation_due_at?: string | null;
            /** Pending Children */
            pending_children?: components["schemas"]["FleetProfileSwitchPendingChild"][];
            /** Pending Operation Ids */
            pending_operation_ids?: string[];
            /**
             * Position
             * @default 0
             */
            position?: number;
            /** Queue */
            queue: components["schemas"]["FleetProfileSwitchQueueItem"][];
            /** Request Id */
            request_id: string;
            result?: components["schemas"]["FleetProfileSwitchAdapterResult"] | null;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Scope Node Ids */
            scope_node_ids: string[];
            /** Skipped Indices */
            skipped_indices?: (number | ExactNumber)[];
            /**
             * State
             * @default queued
             * @enum {string}
             */
            state?: "queued" | "running" | "needs-operator" | "succeeded" | "failed" | "cancelled" | "superseded";
            /** Status Reason */
            status_reason?: string | null;
            /**
             * Stop Reissue Attempt
             * @default 0
             */
            stop_reissue_attempt?: number;
        };
        /**
         * FleetProfileSwitchChildResult
         * @description Profile child receipt containing the public Run/Switch result tree.
         */
        FleetProfileSwitchChildResult: {
            run_switch: components["schemas"]["RunSwitchOperationResult"];
            /** Run Switch Operation Id */
            run_switch_operation_id: string;
        };
        /**
         * FleetProfileSwitchChildState
         * @description Terminal receipt for a child already completed by the adapter.
         */
        FleetProfileSwitchChildState: {
            /**
             * Kind
             * @enum {string}
             */
            kind: "install" | "run" | "stop" | "cleanup";
            /** Operation Id */
            operation_id: string;
            /** Original Operation Id */
            original_operation_id?: string | null;
            /** Queue Index */
            queue_index: number | ExactNumber;
            result?: components["schemas"]["FleetProfileSwitchChildResult"] | null;
            /**
             * State
             * @enum {string}
             */
            state: "succeeded" | "failed" | "cancelled";
        };
        /**
         * FleetProfileSwitchPendingChild
         * @description An issued queue child whose exact outcome remains to be observed.
         */
        FleetProfileSwitchPendingChild: {
            /**
             * Kind
             * @enum {string}
             */
            kind: "install" | "run" | "stop" | "cleanup";
            /** Operation Id */
            operation_id: string;
            /** Original Operation Id */
            original_operation_id?: string | null;
            /** Queue Index */
            queue_index: number | ExactNumber;
        };
        /**
         * FleetProfileSwitchQueueItem
         * @description One durable Run/Switch child in the profile reconciliation queue.
         */
        FleetProfileSwitchQueueItem: {
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "install" | "run" | "stop" | "cleanup";
            profile_stop_scope?: components["schemas"]["RunSwitchProfileStopScope"] | null;
        };
        /**
         * FleetProfileVerificationResult
         * @description Small result used by profile switch adapters that verify directly.
         */
        FleetProfileVerificationResult: {
            /** Verified */
            verified: boolean;
        };
        /** FleetProfileView */
        FleetProfileView: {
            /** Assignments */
            assignments: components["schemas"]["FleetProfileAssignmentView"][];
            cache_summary?: components["schemas"]["FleetCacheSummary"];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Created By */
            created_by: string;
            definition: components["schemas"]["FleetProfileDefinition"];
            /** Description */
            description: string;
            /** Favorite */
            favorite: boolean;
            /** Fleet */
            fleet?: components["schemas"]["FleetNodeView"][];
            /** Id */
            id: string;
            /**
             * Installation Policy
             * @enum {string}
             */
            installation_policy: "keep-cached" | "exact";
            /** Labels */
            labels: {
                [key: string]: string;
            };
            /** Loaded Revision */
            loaded_revision?: number | null;
            /** Name */
            name: string;
            /** Next Actions */
            next_actions?: string[];
            /** Number */
            number: number;
            /** Profile Digest */
            profile_digest: string;
            /** Revision */
            revision: number;
            /**
             * Status
             * @default draft
             */
            status?: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Warnings */
            warnings?: string[];
        };
        /** FleetReenrollRequest */
        FleetReenrollRequest: {
            /** Request Key */
            request_key: string;
        };
        /** FleetRefreshEvent */
        FleetRefreshEvent: {
            /** Event Cursor */
            event_cursor: number | ExactNumber;
            issue?: components["schemas"]["FleetFrameIssue"] | null;
            /**
             * Reset Reason
             * @enum {string}
             */
            reset_reason: "initial" | "cursor-ahead" | "retention-gap" | "missing-telemetry-sample" | "frame-unavailable";
        };
        /** FleetRenameRequest */
        FleetRenameRequest: {
            /** Display Name */
            display_name: string;
        };
        /** FleetSnapshot */
        FleetSnapshot: {
            /** Authority Revision */
            authority_revision: string;
            /** Event Cursor */
            event_cursor: number | ExactNumber;
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            /** Nodes */
            nodes: components["schemas"]["FleetNode"][];
        };
        /**
         * FleetStreamEvent
         * @description OpenAPI union for the JSON payload carried by one SSE frame.
         */
        FleetStreamEvent: components["schemas"]["FleetRefreshEvent"] | components["schemas"]["FleetTelemetryEvent"] | components["schemas"]["FleetChangeEvent"];
        /** FleetTelemetryEvent */
        FleetTelemetryEvent: {
            /** Event Cursor */
            event_cursor: number | ExactNumber;
            /** Node Id */
            node_id: string;
            sample: components["schemas"]["TelemetryPoint"];
        };
        /** FleetUpgradeRequest */
        FleetUpgradeRequest: {
            /**
             * All
             * @default false
             */
            all?: boolean;
            /** Request Key */
            request_key: string;
            /** Selectors */
            selectors?: string[] | null;
        };
        /** FloatParameter */
        FloatParameter: {
            /** Allowed Values */
            allowed_values?: components["schemas"]["ParameterScalar"][];
            /** Default */
            default: (number | ExactNumber) | (number | ExactNumber);
            /**
             * Maximum
             * @default null
             */
            maximum?: (number | ExactNumber) | (number | ExactNumber) | null;
            /**
             * Minimum
             * @default null
             */
            minimum?: (number | ExactNumber) | (number | ExactNumber) | null;
            /** Name */
            name: string;
            /** Pattern */
            pattern?: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "float";
        };
        /** FreshnessEvidence */
        FreshnessEvidence: {
            /** Age Seconds */
            age_seconds?: (number | ExactNumber) | null;
            /** Evidence Digest */
            evidence_digest?: string | null;
            /** Maximum Age Seconds */
            maximum_age_seconds?: number | null;
            /** Observed At */
            observed_at?: string | null;
            /** Source */
            source: string;
            /**
             * State
             * @enum {string}
             */
            state: "fresh" | "stale" | "unknown";
        };
        /** FreshnessPolicy */
        FreshnessPolicy: {
            /**
             * Inventory Fresh Seconds
             * @default 300
             */
            inventory_fresh_seconds?: number;
            /**
             * Telemetry Delayed Seconds
             * @default 20
             */
            telemetry_delayed_seconds?: number;
            /**
             * Telemetry Live Seconds
             * @default 6
             */
            telemetry_live_seconds?: number;
        };
        /** GatewayKeyCreateRequest */
        GatewayKeyCreateRequest: {
            /** Expires */
            expires?: string | null;
            /** Models */
            models?: string[];
            /** Name */
            name: string;
        };
        /**
         * GatewayKeyCreated
         * @description The only response that carries the key. It is not shown again.
         */
        GatewayKeyCreated: {
            /** Created At */
            created_at?: string | null;
            /** Expires At */
            expires_at?: string | null;
            /** Key */
            key: string;
            /** Last Used At */
            last_used_at?: string | null;
            /** Models */
            models: string[];
            /** Name */
            name: string;
        };
        /** GatewayKeyList */
        GatewayKeyList: {
            /** Keys */
            keys: components["schemas"]["GatewayKeyView"][];
        };
        /** GatewayKeyRevoked */
        GatewayKeyRevoked: {
            /** Name */
            name: string;
        };
        /**
         * GatewayKeyView
         * @description One gateway client key without its secret. Empty `models` means all.
         */
        GatewayKeyView: {
            /** Created At */
            created_at?: string | null;
            /** Expires At */
            expires_at?: string | null;
            /** Last Used At */
            last_used_at?: string | null;
            /** Models */
            models: string[];
            /** Name */
            name: string;
        };
        /**
         * GatewayRouteState
         * @description What the inference gateway currently serves: published routes, or maintenance.
         *
         *     ``unavailable`` is the Controller's own word for a gateway whose marker it
         *     cannot read; the gateway never writes it.
         * @enum {string}
         */
        GatewayRouteState: "published" | "maintenance" | "unavailable";
        /**
         * GitHubReleaseAsset
         * @description One GitHub release asset selected for an existing model file.
         */
        GitHubReleaseAsset: {
            /** Asset Id */
            asset_id: number | ExactNumber;
            /** File Id */
            file_id: string;
        };
        /**
         * GitHubReleaseSource
         * @description An exact asset from one release in a canonical GitHub repository.
         */
        GitHubReleaseSource: {
            /** Assets */
            assets: components["schemas"]["GitHubReleaseAsset"][];
            /**
             * Provider
             * @constant
             */
            provider: "github-release";
            /** Release Id */
            release_id: number | ExactNumber;
            /** Repository */
            repository: string;
        };
        /**
         * GpuUnavailableReason
         * @enum {string}
         */
        GpuUnavailableReason: "gpu.command-failed" | "gpu.command-timeout" | "gpu.invalid-output" | "gpu.unsupported-metrics";
        /**
         * HelperErrorCode
         * @description Every code the privileged helper, or the agent speaking about it, names as an error.
         *
         *     The helper builds its rejection from a member; the agent reads a reply code
         *     through the enum, so a word outside it is a malformed rejection, never a code
         *     the agent invents or forwards.  ``runtime_helper_*`` is the spelling of an
         *     agent-side cause in failure evidence.
         * @enum {string}
         */
        HelperErrorCode: "call_join_failed" | "concurrency_limit" | "grant_invalid" | "grant_node_mismatch" | "grant_unauthorized" | "inspection_outcome_invalid" | "installation_reconciliation_busy" | "installation_reconciliation_storage_unavailable" | "installation_intent_observation_required" | "message_framing_invalid" | "operation_command_failed" | "operation_failed" | "operation_invalid" | "operation_invalid_artifact" | "operation_io" | "operation_stop_uncertain" | "operation_unsafe_path" | "outcome_malformed" | "package_custody_failed" | "package_install_failed" | "package_metadata_failed" | "package_preparation_unavailable" | "package_preflight_failed" | "package_verification_failed" | "peer_identity_invalid" | "rejection_malformed" | "request_arguments_presence_invalid" | "request_argument_nul_byte" | "request_attempt_invalid" | "request_bytes_invalid" | "request_document_invalid" | "request_encoding_invalid" | "request_installation_identity_invalid" | "request_invalid" | "request_ledger_failed" | "request_plan_binding_invalid" | "request_plan_bytes_invalid" | "request_replayed" | "request_schema_version_invalid" | "request_storage_invalid" | "response_unbound" | "runtime_authority_unavailable" | "runtime_endpoint_firewall_rejected" | "runtime_fabric_firewall_rejected" | "runtime_fabric_unavailable" | "runtime_helper_call_join_failed" | "runtime_helper_inspection_outcome_invalid" | "runtime_helper_message_framing_invalid" | "runtime_helper_outcome_malformed" | "runtime_helper_protocol_invalid" | "runtime_helper_rejection_malformed" | "runtime_helper_request_arguments_presence_invalid" | "runtime_helper_request_argument_nul_byte" | "runtime_helper_request_attempt_invalid" | "runtime_helper_request_bytes_invalid" | "runtime_helper_request_document_invalid" | "runtime_helper_request_encoding_invalid" | "runtime_helper_request_installation_identity_invalid" | "runtime_helper_request_plan_binding_invalid" | "runtime_helper_request_plan_bytes_invalid" | "runtime_helper_request_schema_version_invalid" | "runtime_helper_request_storage_invalid" | "runtime_helper_response_unbound" | "runtime_helper_stop_uncertain" | "runtime_helper_system_clock_invalid" | "runtime_helper_unavailable" | "runtime_image_identity_invalid" | "runtime_image_inspect_failed" | "runtime_image_load_failed" | "runtime_image_receipt_failed" | "runtime_process_exited" | "runtime_run_missing" | "system_clock_invalid";
        /**
         * HelperOperationCode
         * @description Stable diagnostic codes returned by the privileged operation executor.
         * @enum {string}
         */
        HelperOperationCode: "helper.artifact_invalid" | "helper.command_failed" | "helper.installation_reconciliation_busy" | "helper.installation_reconciliation_storage_unavailable" | "helper.installation_intent_observation_required" | "helper.io_failed" | "helper.operation_invalid" | "helper.package_install_failed" | "helper.package_metadata_invalid" | "helper.package_preparation_unavailable" | "helper.package_preflight_failed" | "helper.runtime_endpoint_firewall_rejected" | "helper.runtime_fabric_firewall_rejected" | "helper.runtime_fabric_unavailable" | "helper.runtime_image_identity_invalid" | "helper.runtime_image_inspect_failed" | "helper.runtime_image_load_failed" | "helper.runtime_image_receipt_failed" | "helper.runtime_invocation_limit_exceeded" | "helper.runtime_invocation_limits_unavailable" | "helper.runtime_invocation_string_limit_exceeded" | "helper.runtime_process_exited" | "helper.runtime_run_missing" | "helper.stop_uncertain" | "helper.unsafe_path";
        /**
         * HostHelperResponseStatus
         * @description The verdict a privileged-helper reply carries.
         * @enum {string}
         */
        HostHelperResponseStatus: "rejected" | "package-installed" | "package-activation-confirmed" | "container-runtime-request-executed" | "container-runtime-stop-uncertain";
        /**
         * ImageStoreCode
         * @description Refusals and damage found by the Controller OCI image store.
         * @enum {string}
         */
        ImageStoreCode: "image_store.busy" | "image_store.collection_deferred" | "image_store.copy_failed" | "image_store.damaged_receipt_evicted" | "image_store.digest_invalid" | "image_store.import_incomplete" | "image_store.manifest_corrupt" | "image_store.manifest_invalid" | "image_store.manifest_unreadable" | "image_store.manifest_unsupported" | "image_store.reference_scan_failed" | "image_store.reference_unpinned" | "image_store.referenced_manifest_damaged";
        /**
         * InstallAdmissionCode
         * @description Why an installation is not admitted (or is waiting) on a Spark.
         * @enum {string}
         */
        InstallAdmissionCode: "install.agent_upgrade_required" | "install.artifact_size_underdeclared" | "install.artifact_store_read_only" | "install.capacity_busy" | "install.compiled_plan_unavailable" | "install.dependencies_stale" | "install.image_distribution_pending" | "install.image_size_underdeclared" | "install.insufficient_disk" | "install.inventory_missing" | "install.model_identity_unavailable" | "install.plan_invalid" | "install.plan_stale" | "install.stale_inventory";
        /**
         * InstallDegradedReason
         * @description Why an installation is shown partial in the fleet projection.
         * @enum {string}
         */
        InstallDegradedReason: "external-member" | "mapping-incomplete" | "missing-ranks" | "unexpected-ranks" | "rank-membership-mismatch" | "installation-not-installed" | "rank-not-installed" | "rank-incomplete-bytes";
        /**
         * InstallPartialEvidence
         * @description Why one recipe installation group on a Spark is not complete.
         */
        InstallPartialEvidence: {
            /** Affected Ranks */
            affected_ranks: number[];
            /** Expected Rank Count */
            expected_rank_count: number;
            group_state: components["schemas"]["InstallationState"];
            /** Installation Id */
            installation_id: string;
            /** Installed Bytes */
            installed_bytes?: (number | ExactNumber) | null;
            /** Present Ranks */
            present_ranks: number[];
            /** Rank */
            rank: number;
            rank_state: components["schemas"]["InstallationState"];
            reason: components["schemas"]["InstallDegradedReason"];
            /** Recipe Id */
            recipe_id: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Required Bytes */
            required_bytes?: (number | ExactNumber) | null;
            /** Title */
            title: string;
        };
        /** InstallPhaseOperation */
        InstallPhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeInstallPayload"];
        };
        /** InstallationNodeChange */
        InstallationNodeChange: {
            /** Entity Id */
            entity_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            entity_kind: "installation-node";
            fields: components["schemas"]["InstallationNodePayload"];
            /** Node Id */
            node_id: string;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /** InstallationNodePayload */
        InstallationNodePayload: {
            /** Entity Id */
            entity_id: string;
            /**
             * Entity Kind
             * @constant
             */
            entity_kind: "installation-node";
            /** Installation Id */
            installation_id: string;
            /** Installed Bytes */
            installed_bytes: number | ExactNumber;
            /** Node Id */
            node_id: string;
            /** Rank */
            rank: number;
            /** Required Bytes */
            required_bytes: number | ExactNumber;
            /** Role */
            role: string;
            /** State */
            state: string;
        };
        /**
         * InstallationNodeState
         * @description The condition of one rank of an installation on its Spark.
         * @enum {string}
         */
        InstallationNodeState: "planned" | "installed" | "failed" | "uninstalled";
        /**
         * InstallationReconcileRequest
         * @description Reconcile the reviewed plan, or use the Controller's one-step decision.
         */
        InstallationReconcileRequest: {
            /** Plan Digest */
            plan_digest?: string | null;
            /** Request Key */
            request_key: string;
        };
        /**
         * InstallationState
         * @description The condition of a recipe installation across its Sparks.
         * @enum {string}
         */
        InstallationState: "planned" | "installing" | "installed" | "partial" | "failed" | "uninstalled";
        /** IntegerParameter */
        IntegerParameter: {
            /** Allowed Values */
            allowed_values?: components["schemas"]["ParameterScalar"][];
            /** Default */
            default: number | ExactNumber;
            /**
             * Maximum
             * @default null
             */
            maximum?: (number | ExactNumber) | null;
            /**
             * Minimum
             * @default null
             */
            minimum?: (number | ExactNumber) | null;
            /** Name */
            name: string;
            /** Pattern */
            pattern?: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "integer";
        };
        /**
         * InvalidRequest
         * @description A malformed or out-of-contract request; it fails closed at submit time.
         */
        InvalidRequest: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            category: "invalid-request";
            /**
             * Field
             * @default null
             */
            field?: string | null;
            reason: components["schemas"]["InvalidRequestReason"];
        };
        /**
         * InvalidRequestReason
         * @description Closed reason codes of an invalid request (submit-time input validation).
         * @enum {string}
         */
        InvalidRequestReason: "malformed" | "out-of-range" | "limit-exceeded" | "unknown-field" | "incomplete" | "immutable" | "duplicate" | "conflict" | "not-found" | "not-ready" | "superseded" | "unsupported";
        /** InventoryState */
        InventoryState: {
            /** Age Seconds */
            age_seconds: number | ExactNumber;
            /** Artifact Store Read Only */
            artifact_store_read_only: boolean;
            /** Capabilities */
            capabilities: string[];
            /** Container Runtime Version */
            container_runtime_version: string;
            /** Disk Free Bytes */
            disk_free_bytes: number | ExactNumber;
            /** Disk Total Bytes */
            disk_total_bytes: number | ExactNumber;
            /** Fabric Address */
            fabric_address?: string | null;
            /** Fabric Bandwidth Mbps */
            fabric_bandwidth_mbps?: (number | ExactNumber) | null;
            /**
             * Freshness
             * @enum {string}
             */
            freshness: "fresh" | "stale";
            /** Gpu Count */
            gpu_count: number;
            /** Gpu Memory Free Bytes */
            gpu_memory_free_bytes: number | ExactNumber;
            /** Gpu Memory Total Bytes */
            gpu_memory_total_bytes: number | ExactNumber;
            /** Host Memory Free Bytes */
            host_memory_free_bytes: number | ExactNumber;
            /** Host Memory Total Bytes */
            host_memory_total_bytes: number | ExactNumber;
            /** Nas Route Interface */
            nas_route_interface?: string | null;
            /** Network Interfaces */
            network_interfaces?: components["schemas"]["NetworkInterface"][] | null;
            /** Nvidia Driver Version */
            nvidia_driver_version: string;
            /**
             * Observed At
             * Format: date-time
             */
            observed_at: string;
            /**
             * Received At
             * Format: date-time
             */
            received_at: string;
        };
        /**
         * InvocationMetadata
         * @description Context for audit and tracing which has no decision-making authority.
         */
        InvocationMetadata: {
            /** Context */
            context?: {
                [key: string]: string;
            };
            /** Correlation Id */
            correlation_id?: string | null;
            /**
             * Origin
             * @default operator
             */
            origin?: string;
            /** Reason */
            reason?: string | null;
        };
        /** JobChange */
        JobChange: {
            /** Entity Id */
            entity_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            entity_kind: "job";
            fields: components["schemas"]["JobPayload"];
            /** Node Id */
            node_id?: null;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /** JobDetailResponse */
        JobDetailResponse: {
            agent_upgrade_diagnostics?: components["schemas"]["AgentUpgradeDiagnosticsResponse"] | null;
            /** Authority Revision */
            authority_revision: string;
            /** Current Attempt */
            current_attempt: number;
            /** Id */
            id: string;
            /** Kind */
            kind: string;
            /** Operation Next Cursor */
            operation_next_cursor?: string | null;
            /** Operation Total */
            operation_total: (number | ExactNumber) | null;
            /** Operations */
            operations: components["schemas"]["JobOperationResponse"][] | null;
            progress: components["schemas"]["JobProgress"] | null;
            /** Projection Issue */
            projection_issue?: string | null;
            recovery?: components["schemas"]["OperationRecovery"] | null;
            /** State */
            state: string;
            /** Status Reason */
            status_reason?: string | null;
            /** Target Next Cursor */
            target_next_cursor?: string | null;
            /** Target Total */
            target_total: number | ExactNumber;
            /** Targets */
            targets: string[];
        };
        /** JobOperationResponse */
        JobOperationResponse: {
            /** Attempt */
            attempt: number;
            evidence_download?: components["schemas"]["OperationEvidenceDownload"] | null;
            /** Failure */
            failure?: components["schemas"]["AgentFailureResult"] | components["schemas"]["AvailabilityOperationFailure"] | components["schemas"]["OperationFailureEvidence"] | null;
            /** Id */
            id: string;
            /** Kind */
            kind: string;
            /** Node Id */
            node_id: string;
            progress?: components["schemas"]["OperationProgress"] | null;
            recovery?: components["schemas"]["OperationRecovery"] | null;
            /** State */
            state: string;
            /** Updated At */
            updated_at?: string | null;
        };
        /** JobPayload */
        JobPayload: {
            /** Entity Id */
            entity_id: string;
            /**
             * Entity Kind
             * @constant
             */
            entity_kind: "job";
            /** Kind */
            kind: string;
            /** State */
            state: string;
            /** Target Count */
            target_count: number | ExactNumber;
        };
        /** JobProgress */
        JobProgress: {
            /** Completed */
            completed: number | ExactNumber;
            /** Failed */
            failed: number | ExactNumber;
            operation?: components["schemas"]["OperationProgress"] | null;
            /** Running */
            running: number | ExactNumber;
            /** Total */
            total: number | ExactNumber;
        };
        /**
         * JobResumeRequest
         * @description What the operator wants the parked job's bounded authorisation to do.
         *
         *     ``resume`` is the default and preserves the historic request shape: an
         *     omitted body or an omitted field authorises one more claim.  ``retire`` is
         *     the terminal disposition for work whose retry budget is already spent.
         */
        JobResumeRequest: {
            /**
             * Disposition
             * @default resume
             * @enum {string}
             */
            disposition?: "resume" | "retire";
        };
        /** JobResumeResponse */
        JobResumeResponse: {
            /** Id */
            id: string;
            /** State */
            state: string;
        };
        /** JobRunPhaseOperation */
        JobRunPhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeJobRunRequest"];
        };
        /**
         * JobRunStopScope
         * @description The exact issued transient targets within an accepted run Stop.
         */
        JobRunStopScope: {
            /** Installation Id */
            installation_id: string;
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Missing Node Ids */
            missing_node_ids?: string[];
            /** Plan Digest */
            plan_digest: string;
            /** Reachable Node Ids */
            reachable_node_ids: string[];
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Run Generation */
            run_generation: number | ExactNumber;
            /** Run Id */
            run_id: string;
            /** Run Node Ids */
            run_node_ids: string[];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Stop Plan Digest */
            stop_plan_digest: string;
            /** Targets */
            targets?: components["schemas"]["ProfileJobRunStopTarget"][];
            /** Unissued Artifact Job Ids */
            unissued_artifact_job_ids?: string[];
            /** Workload Intent Ordinal */
            workload_intent_ordinal: number;
        };
        /**
         * JournalRepairPurpose
         * @enum {string}
         */
        JournalRepairPurpose: "measurement" | "cancellation" | "owner_observation";
        /**
         * LibraryAssessmentCode
         * @description Why a library entry is not assessed runnable on the current fleet.
         * @enum {string}
         */
        LibraryAssessmentCode: "library.assessment_unavailable" | "library.cache_missing" | "library.capacity_unavailable" | "library.insufficient_nodes";
        /** LibraryFacetValues */
        LibraryFacetValues: {
            /** Alignment */
            alignment?: string[];
            /** Creator */
            creator?: string[];
            /** Engine */
            engine?: string[];
            /** Family */
            family: string[];
            /** Publisher */
            publisher?: string[];
            /** Quantization */
            quantization: string[];
            /** Sparks */
            sparks?: (number | ExactNumber)[];
            /** Usage */
            usage: string[];
            /** Version */
            version: string[];
        };
        /**
         * LibraryFilterValues
         * @description The filters that produced a Library page, echoed to the client.
         *
         *     This is a typed echo rather than a free-form map so the request and the
         *     response describe the same vocabulary. Every field is optional, so a page
         *     that applied no filter stays valid without inventing values.
         */
        LibraryFilterValues: {
            /** Alignment */
            alignment?: string[];
            /** Cached */
            cached?: boolean | null;
            /** Creator */
            creator?: string[];
            /** Engine */
            engine?: string[];
            /** Family */
            family?: string[];
            /** Fits Fleet */
            fits_fleet?: boolean | null;
            /** Model */
            model?: string[];
            /** Publisher */
            publisher?: string[];
            /** Quantization */
            quantization?: string[];
            /** Ready */
            ready?: boolean | null;
            /** Search */
            search?: string | null;
            /** Sort */
            sort?: ("updated" | "name") | null;
            /** Sparks */
            sparks?: (number | ExactNumber)[];
            /** Updated Since */
            updated_since?: string | null;
            /** Usage */
            usage?: string[];
            /** Version */
            version?: string[];
        };
        /**
         * LibraryLocalProgress
         * @description Observable progress for a Controller-local preparation operation.
         */
        LibraryLocalProgress: {
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Operation Id */
            operation_id?: string | null;
            /** Phase */
            phase?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "backoff" | "observing" | "succeeded" | "failed" | "cancelled";
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
        };
        /**
         * LibraryLocalState
         * @description Controller cache and Spark-local runtime evidence kept separate.
         */
        LibraryLocalState: {
            /**
             * Controller
             * @enum {string}
             */
            controller: "cached" | "preparing" | "not_cached" | "failed" | "unknown";
            preparation?: components["schemas"]["LibraryLocalProgress"] | null;
            /** Running On */
            running_on?: string[];
        };
        /**
         * LibraryModelIdentity
         * @description Content-addressed identity for a canonical Model document.
         */
        LibraryModelIdentity: {
            /** Content Sha256 */
            content_sha256: string;
            /**
             * Kind
             * @default model
             * @constant
             */
            kind?: "model";
            /** Publisher */
            publisher: string;
            /** Slug */
            slug: string;
        };
        /**
         * LibraryModelProjection
         * @description One exact canonical model variant with operator-facing projections.
         */
        LibraryModelProjection: {
            /** Alignment */
            alignment?: string[];
            document: components["schemas"]["ModelDefinition"];
            /** Family */
            family: string;
            identity: components["schemas"]["LibraryModelIdentity"];
            local: components["schemas"]["LibraryLocalState"];
            /** Quantization */
            quantization: string;
            resources: components["schemas"]["LibraryResourceProjection"];
            /** Selector */
            selector: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Usage */
            usage: string[];
            /** Variant */
            variant: string;
            /** Version */
            version: string;
        };
        /**
         * LibraryProjectionCode
         * @description Reasons the library projection truncates what it lists.
         * @enum {string}
         */
        LibraryProjectionCode: "projection.evidence_truncated" | "projection.reasons_truncated";
        /** LibraryRecipeIdentity */
        LibraryRecipeIdentity: {
            /** Content Sha256 */
            content_sha256: string;
            /** Description */
            description: string;
            /** Publisher */
            publisher: string;
            /** Recipe Id */
            recipe_id: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Slug */
            slug: string;
            /** Title */
            title: string;
        };
        /** LibraryRecipeModel */
        LibraryRecipeModel: {
            model_document: components["schemas"]["ModelDefinition"];
            selection: components["schemas"]["RecipeModelSelection"];
        };
        /**
         * LibraryRecipeProjection
         * @description One exact canonical recipe and its model/resource/local projections.
         */
        LibraryRecipeProjection: {
            /** Alignment */
            alignment?: string | null;
            assessment?: components["schemas"]["RecipeReadiness"] | null;
            /** Creator */
            creator?: string | null;
            document: components["schemas"]["RecipeDefinition"];
            /** Engine */
            engine: string;
            identity: components["schemas"]["LibraryRecipeIdentity"];
            local: components["schemas"]["LibraryLocalState"];
            /** Model Selectors */
            model_selectors: string[];
            /** Node Count */
            node_count: number | ExactNumber;
            resources: components["schemas"]["LibraryResourceProjection"];
            /** Selector */
            selector: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Usage */
            usage: string[];
        };
        /**
         * LibraryRelease
         * @description The recipe library release this Controller last synchronized.
         */
        LibraryRelease: {
            /** Commit */
            commit: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Version */
            version: string;
        };
        /**
         * LibraryResourceProjection
         * @description Declared resource facts; unknown values remain null.
         */
        LibraryResourceProjection: {
            /** Disk Bytes */
            disk_bytes?: (number | ExactNumber) | null;
            /** Image Bytes */
            image_bytes?: (number | ExactNumber) | null;
            /** Memory Bytes */
            memory_bytes?: (number | ExactNumber) | null;
            /** Runtime Memory Bytes */
            runtime_memory_bytes?: (number | ExactNumber) | null;
        };
        /**
         * LifecycleCodeFailureResult
         * @description Bounded failure marker used by the Controller's node projector.
         */
        LifecycleCodeFailureResult: {
            /** Code */
            code: string;
            /** Detail */
            detail?: string | null;
        };
        /**
         * LifecycleEffect
         * @description What is known about the real-world effect of the work.
         * @enum {string}
         */
        LifecycleEffect: "unknown" | "none" | "issued" | "established" | "stopped";
        /**
         * LifecycleEventKind
         * @description The kinds of event the pure transition function accepts.
         * @enum {string}
         */
        LifecycleEventKind: "submitted" | "claimed" | "heartbeat" | "reported" | "lease-lapsed" | "cancel-requested" | "observed" | "operator-action" | "tick";
        /** LifecyclePreflightCheckpoint */
        LifecyclePreflightCheckpoint: {
            /** Attempts */
            attempts?: {
                [key: string]: number | ExactNumber;
            };
            /** Last Failure Code */
            last_failure_code?: string | null;
            /** Last Failure Detail */
            last_failure_detail?: string | null;
            /** Next Check At */
            next_check_at?: string | null;
            /** Pending Job Id */
            pending_job_id?: string | null;
            /** Pending Node Id */
            pending_node_id?: string | null;
            /** Phase Index */
            phase_index: number;
            /** Receipts */
            receipts?: {
                [key: string]: components["schemas"]["RuntimePreflightResult"];
            };
        };
        /**
         * LifecycleState
         * @description The nine lifecycle states: the one vocabulary a stored ``state`` speaks.
         *
         *     ``superseded`` is a definite, non-failed end: a newer request replaced the
         *     work, so nothing is left for anyone to do.  The legacy spellings
         *     (``waiting``, ``partial``, ``cancelling``, ``expired``,
         *     ``waiting-for-operator``) are *aliases*: see :data:`STATE_ALIASES`, the one
         *     table that says what each of them means, per subject.
         * @enum {string}
         */
        LifecycleState: "queued" | "running" | "observing" | "backoff" | "succeeded" | "failed" | "cancelled" | "superseded" | "needs-operator";
        /**
         * LifecycleSubject
         * @description The persisted models whose ``state`` the lifecycle core owns.
         * @enum {string}
         */
        LifecycleSubject: "Job" | "JobAttempt" | "AgentOperation" | "AgentOperationAttempt" | "ModelCacheOperation" | "ArtifactJob" | "FleetProfileApplication";
        /**
         * LifecycleVocabulary
         * @description Carrier that publishes every vocabulary enum into the wire schema.
         *
         *     The model is never sent: it exists so the schema exporter, the Rust
         *     generator and the OpenAPI/TypeScript generators emit each closed word set
         *     from this one module.
         */
        LifecycleVocabulary: {
            agent_client_decision: components["schemas"]["AgentClientDecision"];
            agent_diagnostic_operation: components["schemas"]["AgentDiagnosticOperation"];
            agent_result_state: components["schemas"]["AgentResultState"];
            agent_transport_kind: components["schemas"]["AgentTransportKind"];
            artifact_preparation: components["schemas"]["ArtifactPreparation"];
            asset_availability: components["schemas"]["AssetAvailability"];
            blocker_category: components["schemas"]["BlockerCategory"];
            catalog_sync_state: components["schemas"]["CatalogSyncState"];
            certificate_issuance_purpose: components["schemas"]["CertificateIssuancePurpose"];
            certificate_journal_state: components["schemas"]["CertificateJournalState"];
            certificate_record_state: components["schemas"]["CertificateRecordState"];
            certificate_request_mode: components["schemas"]["CertificateRequestMode"];
            certificate_rotation_state: components["schemas"]["CertificateRotationState"];
            certificate_state: components["schemas"]["CertificateState"];
            desired_assignment_state: components["schemas"]["DesiredAssignmentState"];
            distribution_assignment_state: components["schemas"]["DistributionAssignmentState"];
            effect: components["schemas"]["LifecycleEffect"];
            endpoint_state: components["schemas"]["EndpointState"];
            enrollment_grant_state: components["schemas"]["EnrollmentGrantState"];
            enrollment_profile_state: components["schemas"]["EnrollmentProfileState"];
            enrollment_purpose: components["schemas"]["EnrollmentPurpose"];
            enrollment_record_state: components["schemas"]["EnrollmentRecordState"];
            error_category: components["schemas"]["ErrorCategory"];
            event_kind: components["schemas"]["LifecycleEventKind"];
            failure_code: components["schemas"]["FailureCode"];
            failure_stage: components["schemas"]["FailureStage"];
            gateway_route_state: components["schemas"]["GatewayRouteState"];
            host_helper_response_status: components["schemas"]["HostHelperResponseStatus"];
            installation_node_state: components["schemas"]["InstallationNodeState"];
            installation_state: components["schemas"]["InstallationState"];
            invalid_request_reason: components["schemas"]["InvalidRequestReason"];
            lifecycle_subject: components["schemas"]["LifecycleSubject"];
            migration_step: components["schemas"]["MigrationStep"];
            model_cache_operator_status: components["schemas"]["ModelCacheOperatorStatus"];
            model_file_state: components["schemas"]["ModelFileState"];
            node_identity_state: components["schemas"]["NodeIdentityState"];
            observation_cause: components["schemas"]["ObservationCause"];
            observed_assignment_state: components["schemas"]["ObservedAssignmentState"];
            oci_failure_category: components["schemas"]["OciFailureCategory"];
            operator_action: components["schemas"]["OperatorActionName"];
            operator_surface: components["schemas"]["OperatorSurface"];
            outcome_kind: components["schemas"]["OutcomeKind"];
            placement_install_state: components["schemas"]["PlacementInstallState"];
            placement_load_state: components["schemas"]["PlacementLoadState"];
            profile_action: components["schemas"]["ProfileAction"];
            profile_cancellation_cause: components["schemas"]["ProfileCancellationCause"];
            profile_child_job_kind: components["schemas"]["ProfileChildJobKind"];
            profile_child_phase: components["schemas"]["ProfileChildPhase"];
            profile_child_source: components["schemas"]["ProfileChildSource"];
            profile_document_state: components["schemas"]["ProfileDocumentState"];
            profile_effect_state: components["schemas"]["ProfileEffectState"];
            profile_installation_policy: components["schemas"]["ProfileInstallationPolicy"];
            profile_operation_kind: components["schemas"]["ProfileOperationKind"];
            profile_projection_kind: components["schemas"]["ProfileProjectionKind"];
            profile_reason_severity: components["schemas"]["ProfileReasonSeverity"];
            profile_reported_phase: components["schemas"]["ProfileReportedPhase"];
            profile_retry_disposition: components["schemas"]["ProfileRetryDisposition"];
            profile_switch_child_kind: components["schemas"]["ProfileSwitchChildKind"];
            progress_phase: components["schemas"]["ProgressPhase"];
            recipe_run_disposition: components["schemas"]["RecipeRunDispositionValue"];
            reservation_state: components["schemas"]["ReservationState"];
            resource_blocker_code: components["schemas"]["ResourceBlockerCode"];
            route_publication_state: components["schemas"]["RoutePublicationState"];
            route_state: components["schemas"]["RouteState"];
            run_admission_code: components["schemas"]["RunAdmissionCode"];
            run_state: components["schemas"]["RunState"];
            security_refusal_reason: components["schemas"]["SecurityRefusalReason"];
            state: components["schemas"]["LifecycleState"];
            state_alias: components["schemas"]["StateAlias"];
            state_write_kind: components["schemas"]["StateWriteKind"];
            stop_outcome: components["schemas"]["StopOutcome"];
            wait_reason: components["schemas"]["WaitReason"];
            wait_verdict: components["schemas"]["WaitVerdict"];
        };
        /** LoginRequest */
        LoginRequest: {
            /** Password */
            password: string;
            /**
             * Subject
             * @constant
             */
            subject: "admin";
        };
        /** LoginRequestInvalid */
        LoginRequestInvalid: {
            /**
             * Detail
             * @constant
             */
            detail: "login request is invalid";
        };
        /** ManagedCatalogStaleRecipe */
        ManagedCatalogStaleRecipe: {
            /** Current Revision Id */
            current_revision_id: string;
            /** Recipe Id */
            recipe_id: string;
            /** Stale Installation Count */
            stale_installation_count: number | ExactNumber;
            /** Stale Run Count */
            stale_run_count: number | ExactNumber;
        };
        /** ManagedCatalogSyncFailure */
        ManagedCatalogSyncFailure: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Occurred At */
            occurred_at: string;
        };
        /** ManagedCatalogSyncProblem */
        ManagedCatalogSyncProblem: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Recipe Uri */
            recipe_uri?: string | null;
        };
        /** ManagedCatalogSyncResponse */
        ManagedCatalogSyncResponse: {
            /** Commit */
            commit?: string | null;
            /** Completed At */
            completed_at: string | null;
            /** Created At */
            created_at: string;
            /** Expected Commit */
            expected_commit?: string | null;
            /** Imported Count */
            imported_count: number | ExactNumber;
            last_error?: components["schemas"]["ManagedCatalogSyncFailure"] | null;
            /** Library Updated At */
            library_updated_at?: string | null;
            /** Library Version */
            library_version?: string | null;
            /** Problems */
            problems: components["schemas"]["ManagedCatalogSyncProblem"][];
            /** Processed Count */
            processed_count: number | ExactNumber;
            /** Repository */
            repository: string;
            /** Request Key */
            request_key: string;
            /** Skipped Count */
            skipped_count: number | ExactNumber;
            /** Stale Recipes */
            stale_recipes: components["schemas"]["ManagedCatalogStaleRecipe"][];
            state: components["schemas"]["CatalogSyncState"];
            /** Sync Id */
            sync_id: string;
            /** Total Count */
            total_count: number | ExactNumber;
            /**
             * Trigger
             * @enum {string}
             */
            trigger: "manual" | "automatic";
            /** Unchanged Count */
            unchanged_count: number | ExactNumber;
            /** Updated Count */
            updated_count: number | ExactNumber;
            /** Withdrawn Count */
            withdrawn_count: number | ExactNumber;
            /** Withdrawn Recipes */
            withdrawn_recipes: components["schemas"]["ManagedCatalogWithdrawnRecipe"][];
        };
        /** ManagedCatalogSyncResult */
        ManagedCatalogSyncResult: {
            /** Imported Count */
            imported_count: number | ExactNumber;
            /** Problems */
            problems: components["schemas"]["ManagedCatalogSyncProblem"][];
            /**
             * Reviewed Content Sha256
             * @default null
             */
            reviewed_content_sha256?: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Skipped Count */
            skipped_count: number | ExactNumber;
            /** Stale Recipes */
            stale_recipes: components["schemas"]["ManagedCatalogStaleRecipe"][];
            /**
             * State
             * @enum {string}
             */
            state: "current" | "partial" | "failed";
            /** Unchanged Count */
            unchanged_count: number | ExactNumber;
            /** Updated Count */
            updated_count: number | ExactNumber;
            /** Withdrawn Count */
            withdrawn_count: number | ExactNumber;
            /** Withdrawn Recipes */
            withdrawn_recipes: components["schemas"]["ManagedCatalogWithdrawnRecipe"][];
        };
        /** ManagedCatalogWithdrawnRecipe */
        ManagedCatalogWithdrawnRecipe: {
            /** Recipe Id */
            recipe_id: string;
            /** Recipe Uri */
            recipe_uri?: string | null;
            /** Release Version */
            release_version?: string | null;
        };
        /** MappingSelection */
        MappingSelection: {
            /**
             * Action
             * @enum {string}
             */
            action: "reuse" | "create";
            /** Mapping Generation */
            mapping_generation?: number | null;
            /** Mapping Id */
            mapping_id: string | null;
            /** Nodes */
            nodes: components["schemas"]["SparkGroupNode"][];
            /** Option Choices */
            option_choices?: {
                [key: string]: string;
            };
            /** Parameters */
            parameters?: {
                [key: string]: string | (number | ExactNumber) | boolean | (number | ExactNumber) | components["schemas"]["pydantic__types__JsonValue"];
            };
            /** Placement Digest */
            placement_digest: string;
            /** Topology Name */
            topology_name: string;
        };
        /**
         * MemoryUsageUncertainty
         * @description Fresh aggregate capacity lacks per-run resident usage evidence.
         */
        MemoryUsageUncertainty: {
            /** Inventory Evidence Digest */
            inventory_evidence_digest: string;
            /**
             * Inventory Observed At
             * Format: date-time
             */
            inventory_observed_at: string;
            /** Residual Ranges */
            residual_ranges: components["schemas"]["RunMemoryResidualRange"][];
            /**
             * Source
             * @constant
             */
            source: "aggregate_inventory_without_run_usage";
        };
        /**
         * MigrationStep
         * @description The migration steps of the blocker audit (section 5.6) that retire a writer.
         * @enum {string}
         */
        MigrationStep: "step-2" | "step-3" | "step-4" | "step-5" | "step-6" | "step-7";
        /**
         * ModelArtifactIdentity
         * @description Exact model set, independent of transfer progress and verification time.
         */
        ModelArtifactIdentity: {
            /** Artifact Count */
            artifact_count: number;
            /** Artifact Set Bytes */
            artifact_set_bytes: number | ExactNumber;
            /** Artifact Set Sha256 */
            artifact_set_sha256: string;
            /** Dependency Model Content Sha256 */
            dependency_model_content_sha256?: string[];
            /** Model Content Sha256 */
            model_content_sha256: string;
            /** Recipe Revision Sha256 */
            recipe_revision_sha256?: string | null;
        };
        /**
         * ModelArtifactPreparation
         * @description Complete exact model set, including auxiliary and dependency files.
         */
        ModelArtifactPreparation: {
            /** Artifact Count */
            artifact_count: number;
            /** Artifact Set Bytes */
            artifact_set_bytes: number | ExactNumber;
            /** Artifact Set Sha256 */
            artifact_set_sha256: string;
            /**
             * Completeness
             * @enum {string}
             */
            completeness: "complete" | "incomplete" | "unknown";
            controller: components["schemas"]["ControllerAssetState"];
            /** Dependency Model Content Sha256 */
            dependency_model_content_sha256?: string[];
            /** Model Content Sha256 */
            model_content_sha256: string;
            /** Recipe Revision Sha256 */
            recipe_revision_sha256?: string | null;
            /** Targets */
            targets: components["schemas"]["TargetAssetState"][];
        };
        /** ModelCacheAccessRecheck */
        ModelCacheAccessRecheck: {
            /** Authorized */
            authorized: boolean;
            /** Checked At */
            checked_at: string;
            /** Request Key */
            request_key: string;
        };
        /**
         * ModelCacheBlockerCode
         * @description Why a model download plan is blocked or waiting.
         * @enum {string}
         */
        ModelCacheBlockerCode: "insufficient-reserved-storage" | "model-not-cached" | "recipe-not-cached";
        /**
         * ModelCacheCancellation
         * @description Durable record of the one accepted cancellation request.
         */
        ModelCacheCancellation: {
            /** Actor */
            actor: string;
            observation?: components["schemas"]["ModelCacheCancellationObservation"] | null;
            /** Reason */
            reason: string;
            /** Request Key */
            request_key: string;
            /** Requested At */
            requested_at: string;
        };
        /**
         * ModelCacheCancellationObservation
         * @description The lifecycle owner's actual terminal cancellation evidence.
         */
        ModelCacheCancellationObservation: {
            /** Detail */
            detail: string;
            /**
             * Effect
             * @enum {string}
             */
            effect: "unknown" | "none" | "stopped";
            /** Observed At */
            observed_at: string;
        };
        /**
         * ModelCacheCancellationRequest
         * @description Stable identity and operator explanation for one cancellation request.
         */
        ModelCacheCancellationRequest: {
            /** Reason */
            reason: string;
            /** Request Key */
            request_key: string;
        };
        /** ModelCacheClaim */
        ModelCacheClaim: {
            /** Expires At */
            expires_at: string;
            /** Owner */
            owner: string;
        };
        /**
         * ModelCacheCode
         * @description Model cache resolution, storage, removal and operation problems.
         * @enum {string}
         */
        ModelCacheCode: "model_cache.access_recheck_unavailable" | "model_cache.artifact_count" | "model_cache.artifact_duplicate" | "model_cache.artifact_invalid" | "model_cache.artifact_missing" | "model_cache.artifact_unverified" | "model_cache.cancellation_invalid" | "model_cache.cancellation_key_reused" | "model_cache.capacity" | "model_cache.coverage_incomplete" | "model_cache.credentials_missing" | "model_cache.cursor_invalid" | "model_cache.dependency_count" | "model_cache.digest_invalid" | "model_cache.digest_mismatch" | "model_cache.digest_size_conflict" | "model_cache.document_unreadable" | "model_cache.download_blocked" | "model_cache.entry_missing" | "model_cache.fixture_sources_forbidden" | "model_cache.identity_conflict" | "model_cache.identity_mismatch" | "model_cache.interrupted" | "model_cache.lock_unavailable" | "model_cache.manifest_identity_mismatch" | "model_cache.manifest_invalid" | "model_cache.manifest_too_large" | "model_cache.model_content_digests_invalid" | "model_cache.model_definition_invalid" | "model_cache.model_definition_missing" | "model_cache.model_dependency_cycle" | "model_cache.model_pin_invalid" | "model_cache.model_variant_invalid" | "model_cache.not_cancellable" | "model_cache.object_busy" | "model_cache.operation_failed" | "model_cache.operation_missing" | "model_cache.operation_not_retryable" | "model_cache.operation_unreadable" | "model_cache.payload_invalid" | "model_cache.pin_mismatch" | "model_cache.pin_required" | "model_cache.plan_invalid" | "model_cache.range_invalid" | "model_cache.rate_limited" | "model_cache.recipe_identity_ambiguous" | "model_cache.recipe_identity_invalid" | "model_cache.recipe_identity_missing" | "model_cache.recipe_invalid" | "model_cache.recipe_model_missing" | "model_cache.recipe_revision_invalid" | "model_cache.recipe_revision_missing" | "model_cache.redirect_forbidden" | "model_cache.release_asset_identity_conflict" | "model_cache.release_metadata_invalid" | "model_cache.removal_child_failed" | "model_cache.removal_child_invalid" | "model_cache.removal_child_mismatch" | "model_cache.removal_child_missing" | "model_cache.removal_child_pending" | "model_cache.removal_invalid" | "model_cache.removal_path_unsafe" | "model_cache.removal_plan_invalid" | "model_cache.removal_referenced" | "model_cache.removal_scope_changed" | "model_cache.removal_scope_invalid" | "model_cache.removal_scope_unavailable" | "model_cache.removal_wait" | "model_cache.request_key_invalid" | "model_cache.request_key_reused" | "model_cache.review_invalid" | "model_cache.review_unavailable" | "model_cache.revision_invalid" | "model_cache.revision_missing" | "model_cache.schema_unsupported" | "model_cache.selector_ambiguous" | "model_cache.selector_invalid" | "model_cache.selector_missing" | "model_cache.source_gone" | "model_cache.source_invalid" | "model_cache.source_size_mismatch" | "model_cache.source_truncated" | "model_cache.source_unavailable" | "model_cache.source_unsupported" | "model_cache.source_untrusted" | "model_cache.stale_plan" | "model_cache.unavailable" | "model_cache.upstream_check_budget_exhausted" | "model_cache.upstream_check_failed" | "model_cache.upstream_revision_invalid";
        /** ModelCacheDownloadPayload */
        ModelCacheDownloadPayload: {
            /** @default null */
            access_recheck?: components["schemas"]["ModelCacheAccessRecheck"] | null;
            /** Artifact Set Sha256 */
            artifact_set_sha256: string;
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            /** @default null */
            cancellation?: components["schemas"]["ModelCacheCancellation"] | null;
            /** @default null */
            claim?: components["schemas"]["ModelCacheClaim"] | null;
            /** @default null */
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /**
             * Force Refresh
             * @default false
             */
            force_refresh?: boolean;
            manifest: components["schemas"]["CacheManifest"];
            /**
             * Operator Action
             * @default null
             */
            operator_action?: string | null;
            /** Plan Digest */
            plan_digest: string;
            /**
             * Removal Fence
             * @default null
             */
            removal_fence?: string | null;
            /** @default null */
            result?: components["schemas"]["ModelCacheDownloadResult"] | null;
            /**
             * Resume Of
             * @default null
             */
            resume_of?: string | null;
            retry: components["schemas"]["ModelCacheRetry"];
            /**
             * Retry Of
             * @default null
             */
            retry_of?: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Selector
             * @default null
             */
            selector?: string | null;
            /**
             * Source Policy
             * @constant
             */
            source_policy: "nas-first";
            transfer: components["schemas"]["ModelCacheTransfer"];
            /**
             * With Model
             * @default null
             */
            with_model?: boolean | null;
        };
        /** ModelCacheDownloadResult */
        ModelCacheDownloadResult: {
            /** Artifact Set Sha256 */
            artifact_set_sha256: string;
            /**
             * Coverage
             * @constant
             */
            coverage: "complete";
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /** ModelCacheMissingSourceObservation */
        ModelCacheMissingSourceObservation: {
            /** Artifact Key */
            artifact_key: string;
            /** Observations */
            observations: number | ExactNumber;
            /**
             * Status
             * @enum {integer}
             */
            status: number | ExactNumber;
        };
        /** ModelCacheOperationProgress */
        ModelCacheOperationProgress: {
            /** Completed Artifacts */
            completed_artifacts: number | ExactNumber;
            /**
             * Current Artifact Key
             * @default null
             */
            current_artifact_key?: string | null;
            /** Downloaded Bytes */
            downloaded_bytes: number | ExactNumber;
            /**
             * Expected Bytes
             * @default null
             */
            expected_bytes?: (number | ExactNumber) | null;
            measurement: components["schemas"]["OperationProgress"];
            /**
             * Phase
             * @enum {string}
             */
            phase: "queued" | "downloading" | "verifying" | "reclaiming" | "cancelling" | "completed" | "failed";
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Total Artifacts */
            total_artifacts: number | ExactNumber;
            /**
             * Total Bytes Known
             * @default true
             */
            total_bytes_known?: boolean;
        };
        /**
         * ModelCacheOperatorRequest
         * @description Body shared by the singular operator model actions.
         */
        ModelCacheOperatorRequest: {
            /** Request Key */
            request_key: string;
        };
        /**
         * ModelCacheOperatorResponse
         * @description CLI-shaped result without exposing an internal plan/digest workflow.
         */
        ModelCacheOperatorResponse: {
            /**
             * Action
             * @enum {string}
             */
            action: "download" | "remove";
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            cancellation?: components["schemas"]["ModelCacheCancellation"] | null;
            /** Cancelled Operations */
            cancelled_operations?: string[];
            /** Eta Seconds */
            eta_seconds?: (number | ExactNumber) | null;
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /** Model Content Sha256 */
            model_content_sha256?: string | null;
            /** Next Actions */
            next_actions?: string[];
            /** Next Attempt At */
            next_attempt_at?: string | null;
            /** Operation Id */
            operation_id?: string | null;
            /** Phase */
            phase: string;
            /** Preserved */
            preserved?: string[];
            progress: components["schemas"]["OperationProgress"];
            /** Request Key */
            request_key: string;
            /** Result */
            result?: components["schemas"]["ModelCacheDownloadResult"] | components["schemas"]["ModelCacheRemovalResult"] | null;
            /** Selector */
            selector: string;
            /** State */
            state: "accepted" | ("queued" | "running" | "backoff" | "observing" | "succeeded" | "failed" | "cancelled");
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
            /** Transferred Bytes */
            transferred_bytes: number | ExactNumber;
        };
        /**
         * ModelCacheOperatorStatus
         * @description The operator-facing word of a model-cache operation that is not a stored state.
         *
         *     ``accepted``: the request is recorded and not yet picked up.  Every other state
         *     an operator sees is a :class:`~vonk_agent_protocol.LifecycleState` (a cancel
         *     under way is ``observing`` with its cancellation intent).
         * @enum {string}
         */
        ModelCacheOperatorStatus: "accepted";
        /**
         * ModelCacheRemovalPayload
         * @description Exact, restartable removal plan and its durable effect checkpoint.
         */
        ModelCacheRemovalPayload: {
            /** @default null */
            access_recheck?: components["schemas"]["ModelCacheAccessRecheck"] | null;
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            /** @default null */
            cancellation?: components["schemas"]["ModelCacheCancellation"] | null;
            /** @default null */
            claim?: components["schemas"]["ModelCacheClaim"] | null;
            /** Delete Objects */
            delete_objects: string[];
            /** @default null */
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /**
             * Force Refresh
             * @default false
             */
            force_refresh?: boolean;
            /**
             * Model Content Sha256
             * @default null
             */
            model_content_sha256?: string | null;
            /** Object Index */
            object_index: number | ExactNumber;
            /** Object Pending Bytes */
            object_pending_bytes: (number | ExactNumber) | null;
            /**
             * Operator Action
             * @default null
             */
            operator_action?: string | null;
            /** Reclaimed Bytes */
            reclaimed_bytes: number | ExactNumber;
            /** Removal Fence */
            removal_fence: string;
            result: components["schemas"]["ModelCacheRemovalResult"] | null;
            /**
             * Resume Of
             * @default null
             */
            resume_of?: string | null;
            retry: components["schemas"]["ModelCacheRetry"];
            /**
             * Retry Of
             * @default null
             */
            retry_of?: string | null;
            /** Review Digest */
            review_digest: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Scope From Content
             * @default false
             */
            scope_from_content?: boolean;
            /**
             * Scope Pending
             * @default false
             */
            scope_pending?: boolean;
            /** Selected */
            selected: string[];
            /** Selected Objects */
            selected_objects: string[];
            /** Selector */
            selector: string;
            /** Set Index */
            set_index: number | ExactNumber;
            /**
             * Source Policy
             * @constant
             */
            source_policy: "nas-first";
            /**
             * With Model
             * @default null
             */
            with_model?: boolean | null;
        };
        /**
         * ModelCacheRemovalRequest
         * @description Request key for removing the named model against current state.
         */
        ModelCacheRemovalRequest: {
            /** Request Key */
            request_key: string;
        };
        /** ModelCacheRemovalResult */
        ModelCacheRemovalResult: {
            /** Cancelled Operations */
            cancelled_operations?: string[];
            /** Reclaimed Bytes */
            reclaimed_bytes: number | ExactNumber;
            /** Removed Entries */
            removed_entries: string[];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /** ModelCacheRepairCheckpoint */
        ModelCacheRepairCheckpoint: {
            /** Completed Objects */
            completed_objects: string[];
            /** Transfer Id */
            transfer_id: string;
        };
        /** ModelCacheRepairPayload */
        ModelCacheRepairPayload: {
            /** @default null */
            access_recheck?: components["schemas"]["ModelCacheAccessRecheck"] | null;
            /** Artifact Set Sha256 */
            artifact_set_sha256: string;
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            /** @default null */
            cancellation?: components["schemas"]["ModelCacheCancellation"] | null;
            /** @default null */
            claim?: components["schemas"]["ModelCacheClaim"] | null;
            /** @default null */
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /**
             * Force Refresh
             * @default false
             */
            force_refresh?: boolean;
            manifest: components["schemas"]["CacheManifest"];
            /**
             * Operator Action
             * @default null
             */
            operator_action?: string | null;
            /** Plan Digest */
            plan_digest: string;
            /**
             * Removal Fence
             * @default null
             */
            removal_fence?: string | null;
            repair_checkpoint: components["schemas"]["ModelCacheRepairCheckpoint"];
            /** @default null */
            result?: components["schemas"]["ModelCacheDownloadResult"] | null;
            /**
             * Resume Of
             * @default null
             */
            resume_of?: string | null;
            retry: components["schemas"]["ModelCacheRetry"];
            /**
             * Retry Of
             * @default null
             */
            retry_of?: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Selector
             * @default null
             */
            selector?: string | null;
            /**
             * Source Policy
             * @constant
             */
            source_policy: "nas-first";
            transfer: components["schemas"]["ModelCacheTransfer"];
            /**
             * With Model
             * @default null
             */
            with_model?: boolean | null;
        };
        /** ModelCacheRetry */
        ModelCacheRetry: {
            /** Automatic Attempts */
            automatic_attempts: number | ExactNumber;
            /**
             * Credential Fingerprint
             * @default null
             */
            credential_fingerprint?: string | null;
            /** @default null */
            missing_source?: components["schemas"]["ModelCacheMissingSourceObservation"] | null;
            /**
             * Next Retry At
             * @default null
             */
            next_retry_at?: string | null;
            /** Operator Retries */
            operator_retries: number | ExactNumber;
            /**
             * Retry After Seconds
             * @default null
             */
            retry_after_seconds?: (number | ExactNumber) | null;
        };
        /** ModelCacheTransfer */
        ModelCacheTransfer: {
            /** Artifacts */
            artifacts: {
                [key: string]: components["schemas"]["ModelCacheTransferArtifact"];
            };
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Total Bytes */
            total_bytes: number | ExactNumber;
        };
        /** ModelCacheTransferArtifact */
        ModelCacheTransferArtifact: {
            /** Baseline Bytes */
            baseline_bytes: number | ExactNumber;
            /** Received Bytes */
            received_bytes: number | ExactNumber;
            /** Started At */
            started_at: string;
        };
        /**
         * ModelDefinition
         * @description One exact model version and variant, including its complete manifest.
         */
        ModelDefinition: {
            /** Capabilities */
            capabilities: string[];
            /** Dependencies */
            dependencies: components["schemas"]["ModelReference"][];
            /** Files */
            files: components["schemas"]["ModelFile"][];
            format: components["schemas"]["ModelFormat"];
            identity: components["schemas"]["ModelIdentity"];
            /**
             * Kind
             * @default model
             * @constant
             */
            kind?: "model";
            license: components["schemas"]["ModelLicense"];
            metadata: components["schemas"]["ModelMetadata"];
            /** Modalities */
            modalities: ("text" | "image" | "audio" | "video" | "3d" | "embeddings")[];
            /** Requires Token */
            requires_token: boolean;
            /** Source */
            source: components["schemas"]["ModelSource"] | components["schemas"]["GitHubReleaseSource"];
        };
        /** ModelDetailResponse */
        ModelDetailResponse: {
            /** Alignment */
            alignment?: string[];
            document: components["schemas"]["ModelDefinition"];
            /** Family */
            family: string;
            identity: components["schemas"]["LibraryModelIdentity"];
            local: components["schemas"]["LibraryLocalState"];
            /** Quantization */
            quantization: string;
            resources: components["schemas"]["LibraryResourceProjection"];
            /** Selector */
            selector: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Usage */
            usage: string[];
            /** Variant */
            variant: string;
            /** Version */
            version: string;
        };
        /** ModelFamily */
        ModelFamily: {
            /** Publisher */
            publisher: string;
            /** Slug */
            slug: string;
            /** Title */
            title: string;
        };
        /**
         * ModelFile
         * @description One entry in the complete immutable model file manifest.
         *
         *     ``sha256`` and ``size_bytes`` always describe the whole installed file.
         *     When the source publishes the file only as split parts, ``parts`` lists
         *     them in joining order (byte concatenation yields the file). Omitted means
         *     the source publishes the file whole, so the same bytes have the same
         *     identity whether the source splits them or not.
         */
        ModelFile: {
            /** Id */
            id: string;
            /** Parts */
            parts?: components["schemas"]["ModelFilePart"][] | null;
            /** Path */
            path: string;
            /** Roles */
            roles: string[];
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number | ExactNumber;
        };
        /**
         * ModelFilePart
         * @description One published piece of a file the source can only host split.
         *
         *     A part is a transport detail: it exists only at the source (for example a
         *     Hugging Face repository that caps files at 50 GB publishes
         *     ``model.safetensors.part00``). It is never installed.
         */
        ModelFilePart: {
            /** Path */
            path: string;
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number | ExactNumber;
        };
        /**
         * ModelFileState
         * @description The condition of one model file a node holds.
         * @enum {string}
         */
        ModelFileState: "partial" | "verified" | "missing" | "corrupt";
        /** ModelFormat */
        ModelFormat: {
            /** Precision */
            precision: string;
            /** Quantization */
            quantization: string;
        };
        /**
         * ModelIdentity
         * @description The family, logical model, exact version, and selected variant.
         */
        ModelIdentity: {
            family: components["schemas"]["ModelFamily"];
            model: components["schemas"]["ModelRecord"];
            /** Publisher */
            publisher: string;
            /** Slug */
            slug: string;
            /** Variant */
            variant: string;
            /** Version */
            version: string;
        };
        /** ModelLibraryResponse */
        ModelLibraryResponse: {
            facets: components["schemas"]["LibraryFacetValues"];
            filters?: components["schemas"]["LibraryFilterValues"];
            freshness_policy: components["schemas"]["FreshnessPolicy"];
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            library?: components["schemas"]["LibraryRelease"] | null;
            /** Models */
            models: components["schemas"]["LibraryModelProjection"][];
            /** Next Cursor */
            next_cursor: string | null;
        };
        /** ModelLicense */
        ModelLicense: {
            /** Attribution */
            attribution: string[];
            /** Spdx */
            spdx: string;
            territorial_restrictions?: components["schemas"]["ModelTerritorialRestrictions"] | null;
            /** Url */
            url: string;
        };
        /** ModelMetadata */
        ModelMetadata: {
            /** Description */
            description: string;
            /** Tags */
            tags: string[];
        };
        /** ModelRecord */
        ModelRecord: {
            /** Publisher */
            publisher: string;
            /** Slug */
            slug: string;
            /** Title */
            title: string;
        };
        /** ModelReference */
        ModelReference: {
            /** Content Sha256 */
            content_sha256: string;
            /**
             * Kind
             * @default model
             * @constant
             */
            kind?: "model";
            /** Publisher */
            publisher: string;
            /** Slug */
            slug: string;
        };
        /** ModelRevisionProjection */
        ModelRevisionProjection: {
            /** Artifact Count */
            artifact_count: number | ExactNumber;
            /** Download Bytes */
            download_bytes: number | ExactNumber;
            /**
             * Failure Reason
             * @default null
             */
            failure_reason?: string | null;
            identity: components["schemas"]["ModelIdentity"];
            /** Installed Bytes */
            installed_bytes: number | ExactNumber;
            /** Modalities */
            modalities: ("text" | "image" | "audio" | "video" | "3d" | "embeddings")[];
            /** @default null */
            package_handle?: components["schemas"]["RecipePackageHandleProjection"] | null;
            /**
             * Package Sha256
             * @default null
             */
            package_sha256?: string | null;
            /**
             * Publication Commit
             * @default null
             */
            publication_commit?: string | null;
            /**
             * Release Released At
             * @default null
             */
            release_released_at?: string | null;
            /**
             * Release Version
             * @default null
             */
            release_version?: string | null;
            /**
             * Source Bundle Sha256
             * @default null
             */
            source_bundle_sha256?: string | null;
            /**
             * Source Path
             * @default null
             */
            source_path?: string | null;
        };
        /** ModelSource */
        ModelSource: {
            /** Repository */
            repository: string;
            /** Revision */
            revision: string;
        };
        /** ModelTerritorialRestrictions */
        ModelTerritorialRestrictions: {
            /** Denied Jurisdictions */
            denied_jurisdictions: string[];
            /** Notice */
            notice: string;
        };
        /** NativeProgressWitness */
        NativeProgressWitness: {
            /** Attempt Id */
            attempt_id: string;
            /** Certificate Serial */
            certificate_serial: string;
            /** Fence */
            fence: string;
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            /** Payload Digest */
            payload_digest: string;
            /** @default null */
            sample?: components["schemas"]["OperationProgress"] | null;
            /**
             * Sample Digest
             * @default null
             */
            sample_digest?: string | null;
        };
        /**
         * NetworkInterface
         * @description One NIC as the agent reads it from sysfs.
         */
        NetworkInterface: {
            /** Carrier */
            carrier: boolean;
            /**
             * Kind
             * @enum {string}
             */
            kind: "wired" | "wifi" | "fabric" | "tunnel" | "other";
            /** Link Speed Mbps */
            link_speed_mbps?: number | null;
            /** Name */
            name: string;
        };
        /** NodeConnection */
        NodeConnection: {
            /**
             * Agent State
             * @enum {string}
             */
            agent_state: "unregistered" | "pending" | "active" | "retired" | "revoked";
            certificate_state: components["schemas"]["CertificateState"];
            /** Last Seen Age Seconds */
            last_seen_age_seconds: (number | ExactNumber) | null;
            /** Last Seen At */
            last_seen_at: string | null;
            offline_reason: components["schemas"]["NodeOfflineReason"] | null;
            /**
             * Online State
             * @enum {string}
             */
            online_state: "online" | "offline" | "unregistered";
        };
        /**
         * NodeDistributionAssignment
         * @description A distribution grant scoped to one node, plan and model set.
         *
         *     Only the object set travels to the agent (``wire``); the scope fields stay
         *     with the Controller, which authorizes every download against them.
         */
        NodeDistributionAssignment: {
            /**
             * Assignment Id
             * Format: uuid
             */
            assignment_id: string;
            /**
             * Expires At
             * Format: date-time
             */
            expires_at: string;
            /** Generation */
            generation: number;
            /** Model Artifact Set Sha256 */
            model_artifact_set_sha256: string;
            /** Node Id */
            node_id: string;
            /** Objects */
            objects: components["schemas"]["DistributionObject"][];
            /** Oci Archive Sha256 */
            oci_archive_sha256: string;
            /** Oci Image Config Digest */
            oci_image_config_digest: string;
            /** Oci Image Digest */
            oci_image_digest: string;
            /** Plan Digest */
            plan_digest: string;
        };
        /**
         * NodeIdentityState
         * @enum {string}
         */
        NodeIdentityState: "active" | "retired";
        /**
         * NodeOfflineReason
         * @description Why a node is shown offline in the fleet projection.
         * @enum {string}
         */
        NodeOfflineReason: "unregistered" | "agent-inactive" | "agent-revoked" | "never-seen" | "last-seen-in-future" | "stale" | "certificate-missing" | "certificate-not-yet-valid" | "certificate-expired" | "certificate-revoked" | "certificate-inactive";
        /** NodeProfileChange */
        NodeProfileChange: {
            /** Entity Id */
            entity_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            entity_kind: "node-profile";
            fields: components["schemas"]["NodeProfilePayload"];
            /** Node Id */
            node_id: string;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /** NodeProfilePayload */
        NodeProfilePayload: {
            /** Display Name Changed */
            display_name_changed?: boolean | null;
            /** Node Id */
            node_id: string;
            /** Profile Changed */
            profile_changed?: boolean | null;
        };
        /** NodeTelemetryPayload */
        NodeTelemetryPayload: {
            /** Node Id */
            node_id: string;
            /** Sample Id */
            sample_id: string;
        };
        /**
         * ObservationCause
         * @description Why an attempt is being observed rather than settled.
         *
         *     An attempt that ended without a definite answer is ``observing``; this says
         *     what left it unanswered.  ``reported-unknown``: the executor said it could not
         *     confirm the effect.  ``lease-lapsed``: the executor stopped reporting.  The old
         *     spellings carried this in the state word itself (``waiting-for-operator`` and
         *     ``expired``), which is why an adopted attempt also yields its cause.
         * @enum {string}
         */
        ObservationCause: "reported-unknown" | "lease-lapsed";
        /** ObservationTransferChunk */
        ObservationTransferChunk: {
            /** Data */
            data: string;
            /** Ordinal */
            ordinal: number | ExactNumber;
            /** Transfer Id */
            transfer_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "chunk";
        };
        /** ObservationTransferComplete */
        ObservationTransferComplete: {
            /** Bytes */
            bytes: number | ExactNumber;
            /** Chunks */
            chunks: number | ExactNumber;
            /** Sha256 */
            sha256: string;
            /** Transfer Id */
            transfer_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "complete";
        };
        /** ObservationTransferError */
        ObservationTransferError: {
            /** Detail */
            detail: string;
            /**
             * Reason Code
             * @constant
             */
            reason_code: "observation.transfer_unavailable";
            /** Transfer Id */
            transfer_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "error";
        };
        /**
         * ObservationTransferRecord
         * @description One NDJSON record; EOF after a verified complete record is success.
         */
        ObservationTransferRecord: components["schemas"]["ObservationTransferStart"] | components["schemas"]["ObservationTransferChunk"] | components["schemas"]["ObservationTransferComplete"] | components["schemas"]["ObservationTransferError"];
        /** ObservationTransferStart */
        ObservationTransferStart: {
            /**
             * Encoding
             * @constant
             */
            encoding: "base64-canonical-json-utf8-v1";
            /**
             * Resource
             * @enum {string}
             */
            resource: "fleet" | "platform";
            /** Transfer Id */
            transfer_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "start";
        };
        /**
         * ObservedAssignmentState
         * @description Where a fleet-profile assignment stands on the Sparks, as observed.
         * @enum {string}
         */
        ObservedAssignmentState: "not-placed" | "placed" | "installing" | "installed" | "running" | "degraded";
        /**
         * OciFailureCategory
         * @description Secret-free OCI failure context preserved across the helper boundary.
         * @enum {string}
         */
        OciFailureCategory: "storage-permission-denied" | "storage-not-found" | "process" | "workload" | "runtime" | "image-digest" | "artifact" | "storage" | "metadata" | "capacity" | "reconciliation-busy";
        /**
         * OfflineStopIntent
         * @description Exact Stop orders retained for reconciliation after node contact returns.
         */
        OfflineStopIntent: {
            /**
             * Code
             * @default node.offline
             * @constant
             */
            code?: "node.offline";
            /** Node Ids */
            node_ids: string[];
        };
        /**
         * OperationBlocker
         * @description One reason an operation is waiting or blocked, with the Sparks it concerns.
         */
        OperationBlocker: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Node Ids */
            node_ids?: string[];
            /**
             * Severity
             * @enum {string}
             */
            severity: "info" | "warning" | "error";
        };
        /**
         * OperationCheckpoint
         * @description Restart-safe cursor identifying a durable operation unit.
         */
        OperationCheckpoint: {
            /** Cursor */
            cursor?: string | null;
            /** Digest */
            digest?: string | null;
            /** Key */
            key: string;
            /** Sequence */
            sequence: number | ExactNumber;
        };
        /** OperationDetailResponse */
        OperationDetailResponse: {
            /** Attempt */
            attempt: number;
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            cancellation?: components["schemas"]["FleetProfileApplicationCancellationView"] | null;
            /** Created At */
            created_at?: string | null;
            evidence_download?: components["schemas"]["OperationEvidenceDownload"] | null;
            /** Failure */
            failure?: components["schemas"]["AgentFailureResult"] | components["schemas"]["AvailabilityOperationFailure"] | components["schemas"]["OperationFailureEvidence"] | null;
            /** Id */
            id: string;
            /** Kind */
            kind: string;
            model_cache_cancellation?: components["schemas"]["ModelCacheCancellation"] | null;
            /** Next Attempt At */
            next_attempt_at?: string | null;
            /** Node Ids */
            node_ids: string[];
            /**
             * Observation Unavailable
             * @default false
             */
            observation_unavailable?: boolean;
            owner?: components["schemas"]["OperationOwnerReference"] | null;
            /** Parent Id */
            parent_id?: string | null;
            progress?: components["schemas"]["OperationProgress"] | null;
            /** Projection Issues */
            projection_issues?: components["schemas"]["OperationProjectionIssue"][] | null;
            recovery?: components["schemas"]["OperationRecovery"] | null;
            /** State */
            state: string;
            /** Status Reason */
            status_reason?: string | null;
            /** Updated At */
            updated_at?: string | null;
        };
        /**
         * OperationEvidenceDownload
         * @description Where this failed attempt's diagnostics render on request.
         */
        OperationEvidenceDownload: {
            /** Href */
            href: string;
        };
        /**
         * OperationFailureCode
         * @description Error codes of a stored operation failure evidence record.
         * @enum {string}
         */
        OperationFailureCode: "fleet_profile_application_failed" | "artifact_process_failed" | "stored_operation_result_unreadable";
        /**
         * OperationFailureEvidence
         * @description Small, sanitized operator evidence safe to expose in status responses.
         */
        OperationFailureEvidence: {
            /** Detail */
            detail?: string | null;
            /** Error Code */
            error_code: string;
            /**
             * Retryable
             * @default false
             */
            retryable?: boolean;
            /** Summary */
            summary: string;
            /**
             * Uncertain
             * @default false
             */
            uncertain?: boolean;
        };
        /**
         * OperationMemberProgress
         * @description Progress for one node, rank, shard, or other operation member.
         */
        OperationMemberProgress: {
            /** Activity */
            activity?: ("active" | "waiting" | "possibly_stalled") | null;
            /** Bytes Per Second */
            bytes_per_second?: number | null;
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Completed Items */
            completed_items?: (number | ExactNumber) | null;
            /** Elapsed Seconds */
            elapsed_seconds?: (number | ExactNumber) | null;
            /** Eta Seconds */
            eta_seconds?: number | null;
            /** Kind */
            kind?: string | null;
            /** Last Progress At */
            last_progress_at?: string | null;
            /** Member Id */
            member_id: string;
            /** Object Sha256 */
            object_sha256?: string | null;
            /** Observed At */
            observed_at?: string | null;
            /** Phase */
            phase: string;
            /** Smoothed Bytes Per Second */
            smoothed_bytes_per_second?: number | null;
            /**
             * State
             * @default running
             */
            state?: string;
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
            /** Total Items */
            total_items?: (number | ExactNumber) | null;
        };
        /**
         * OperationOwnerReference
         * @description Exact durable owner and original request identity for Activity.
         */
        OperationOwnerReference: {
            /** Id */
            id: string;
            /** Kind */
            kind: string;
            /** Request Id */
            request_id?: string | null;
        };
        /**
         * OperationProgress
         * @description Canonical durable progress payload shared by Controller and agents.
         */
        OperationProgress: {
            /** Activity */
            activity?: ("active" | "waiting" | "possibly_stalled") | null;
            /** Bytes Per Second */
            bytes_per_second?: number | null;
            checkpoint?: components["schemas"]["OperationCheckpoint"] | null;
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Completed Items */
            completed_items?: (number | ExactNumber) | null;
            /** Elapsed Seconds */
            elapsed_seconds?: (number | ExactNumber) | null;
            /** Eta Seconds */
            eta_seconds?: number | null;
            /** Kind */
            kind?: string | null;
            /** Last Progress At */
            last_progress_at?: string | null;
            /** Members */
            members?: components["schemas"]["OperationMemberProgress"][];
            /** Object Sha256 */
            object_sha256?: string | null;
            /** Observed At */
            observed_at?: string | null;
            /** Phase */
            phase: string;
            /** Smoothed Bytes Per Second */
            smoothed_bytes_per_second?: number | null;
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
            /**
             * Total Bytes Known
             * @default false
             */
            total_bytes_known?: boolean;
            /** Total Items */
            total_items?: (number | ExactNumber) | null;
        };
        /**
         * OperationProjectionIssue
         * @description One optional fact unavailable within this response's reader allocation.
         */
        OperationProjectionIssue: {
            /** Budget Bytes */
            budget_bytes: number | ExactNumber;
            /**
             * Field
             * @enum {string}
             */
            field: "progress" | "cancellation";
            /** Observed Bytes */
            observed_bytes: number | ExactNumber;
            /**
             * Reason
             * @default response-budget-exceeded
             * @constant
             */
            reason?: "response-budget-exceeded";
        };
        /** OperationRecovery */
        OperationRecovery: {
            /** Actions */
            actions?: components["schemas"]["OperationRecoveryAction"][];
            /** Explanation */
            explanation?: string | null;
            /**
             * Uncertain
             * @default false
             */
            uncertain?: boolean;
        };
        /**
         * OperationRecoveryAction
         * @enum {string}
         */
        OperationRecoveryAction: "retry" | "resume" | "cancel" | "inspect";
        /** OperationsResponse */
        OperationsResponse: {
            /**
             * Continuation Unavailable
             * @default false
             */
            continuation_unavailable?: boolean;
            /** Next Cursor */
            next_cursor?: string | null;
            /** Operations */
            operations: components["schemas"]["OperationDetailResponse"][] | null;
            /** Projection Issue */
            projection_issue?: string | null;
            /** Total */
            total: (number | ExactNumber) | null;
        };
        /**
         * OperatorActionName
         * @description The operator actions a row can advertise and the core accepts.
         * @enum {string}
         */
        OperatorActionName: "resume" | "retire" | "retry" | "stop";
        /**
         * OperatorSurface
         * @description The real surfaces behind an advertised action in the blocker allowlist.
         * @enum {string}
         */
        OperatorSurface: "resume" | "retire" | "retry" | "stop" | "automatic";
        /**
         * OutcomeDone
         * @description The effect is established; ``result`` is the operation's success body.
         */
        OutcomeDone: {
            /**
             * Kind
             * @constant
             */
            kind: "done";
            /** Result */
            result: components["schemas"]["RuntimePreflightResult"] | components["schemas"]["AgentInstallResult"] | components["schemas"]["RecipeStartResult"] | components["schemas"]["RecipeStopResult"] | components["schemas"]["RecipeReconcileResult"] | components["schemas"]["RecipeUninstallResult"] | components["schemas"]["RecipeBuildEvidence"] | components["schemas"]["RecipeBuildCleanupEvidence"] | components["schemas"]["RecipeJobRunResult"] | components["schemas"]["ArtifactDistributionResult"] | components["schemas"]["AgentUpgradeResult"];
        };
        /**
         * OutcomeEvidence
         * @description Typed facts an executor attaches to a failed or unknown outcome.
         *
         *     Every field is bounded and already sanitized by the agent; the Controller
         *     sanitizes again at ingress.  ``diagnostics`` holds the bounded diagnostic
         *     logs, ``helper_error_code``/``helper_exit_code`` the privileged helper's own
         *     verdict, ``stage``/``diagnostic`` the phase and a one-line cause.
         */
        OutcomeEvidence: {
            /**
             * Diagnostic
             * @default null
             */
            diagnostic?: string | null;
            /** @default null */
            diagnostics?: components["schemas"]["FailureDiagnostics"] | null;
            /**
             * Helper Error Code
             * @default null
             */
            helper_error_code?: string | null;
            /**
             * Helper Exit Code
             * @default null
             */
            helper_exit_code?: number | null;
            /** @default null */
            package_activation?: components["schemas"]["PackageActivationReceipt"] | null;
            /**
             * Stage
             * @default null
             */
            stage?: string | null;
        };
        /**
         * OutcomeFailed
         * @description A definite failure.
         *
         *     ``receipt`` is the process receipt of a one-shot recipe job whose process
         *     ran and exited nonzero; every other failure reports ``code`` and ``reason``.
         */
        OutcomeFailed: {
            code: components["schemas"]["FailureCode"];
            /** @default null */
            evidence?: components["schemas"]["OutcomeEvidence"] | null;
            /** @default null */
            failure_kind?: components["schemas"]["AgentFailureKind"] | null;
            /**
             * Kind
             * @constant
             */
            kind: "failed";
            /** Reason */
            reason: string;
            /** @default null */
            receipt?: components["schemas"]["RecipeJobRunResult"] | null;
            /**
             * Retry After Seconds
             * @default null
             */
            retry_after_seconds?: number | null;
        };
        /**
         * OutcomeKind
         * @description What an executor reported, as the lifecycle core sees it.
         *
         *     ``done``, ``cancelled`` and a ``failed`` that is not retryable are
         *     *definite*: the executor says what happened, and the row ends there.
         *     ``unknown`` (and a retryable failure) says the effect may or may not have
         *     happened, which the core resolves by observing it.  On the agent wire a
         *     cancellation is a definite ``failed`` outcome with the
         *     ``operation_cancelled`` code.
         * @enum {string}
         */
        OutcomeKind: "done" | "failed" | "cancelled" | "unknown";
        /**
         * OutcomeUnknown
         * @description The executor could not establish the effect.
         */
        OutcomeUnknown: {
            /** @default null */
            evidence?: components["schemas"]["OutcomeEvidence"] | null;
            /**
             * Kind
             * @constant
             */
            kind: "unknown";
            /** Reason */
            reason: string;
            /** @default null */
            receipt?: components["schemas"]["RecipeJobRunResult"] | null;
            /**
             * Retry After Seconds
             * @default null
             */
            retry_after_seconds?: number | null;
            wait_reason: components["schemas"]["WaitReason"];
        };
        /** OutputLimits */
        OutputLimits: {
            /** Allowed Media Types */
            allowed_media_types: string[];
            /** Max File Bytes */
            max_file_bytes: number;
            /** Max Files */
            max_files: number;
            /** Max Total Bytes */
            max_total_bytes: number;
        };
        /**
         * PackageActivationOutcome
         * @enum {string}
         */
        PackageActivationOutcome: "awaiting_controller_activation" | "candidate_install_failed" | "controller_confirmed_activation" | "restoring_captured_source" | "source_restored_and_restarted" | "source_restore_failed";
        /**
         * PackageActivationPhase
         * @description Where a package activation transaction stands.
         * @enum {string}
         */
        PackageActivationPhase: "armed" | "activation_failed" | "acknowledged" | "rolling_back" | "rolled_back" | "rollback_failed";
        /** PackageActivationReceipt */
        PackageActivationReceipt: {
            /** Attempt Nonce */
            attempt_nonce: string;
            /** Candidate Binary Sha256 */
            candidate_binary_sha256: string;
            /** Candidate Package Sha256 */
            candidate_package_sha256: string;
            /** Candidate Version */
            candidate_version: string;
            /**
             * Created At
             * Format: int64
             */
            created_at: number | ExactNumber;
            /** Node Id */
            node_id: string;
            outcome: components["schemas"]["PackageActivationOutcome"];
            phase: components["schemas"]["PackageActivationPhase"];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Source Binary Sha256 */
            source_binary_sha256: string;
            /** Source Package Sha256 */
            source_package_sha256: string;
            /** Source Version */
            source_version: string;
            /**
             * Updated At
             * Format: int64
             */
            updated_at: number | ExactNumber;
        };
        /** PackageRollbackAuthority */
        PackageRollbackAuthority: {
            /**
             * Activation Deadline
             * Format: int64
             */
            activation_deadline: number | ExactNumber;
            /** Attempt Nonce */
            attempt_nonce: string;
            source: components["schemas"]["PackageRollbackSource"];
        };
        /** PackageRollbackSource */
        PackageRollbackSource: {
            /** Binary Sha256 */
            binary_sha256: string;
            /** Helper Sha256 */
            helper_sha256: string;
            /** Package Sha256 */
            package_sha256: string;
            /** Package Signature */
            package_signature: string;
            /** Package Version */
            package_version: string;
        };
        ParameterDefinition: components["schemas"]["StringParameter"] | components["schemas"]["IntegerParameter"] | components["schemas"]["FloatParameter"] | components["schemas"]["BooleanParameter"] | components["schemas"]["EnumParameter"];
        ParameterScalar: boolean | (number | ExactNumber) | (number | ExactNumber) | string;
        /**
         * PlacementInstallState
         * @description How much of a placement's installation is already on its Sparks.
         * @enum {string}
         */
        PlacementInstallState: "complete" | "partial" | "not_present" | "unknown";
        /**
         * PlacementLoadState
         * @description Whether a placement's recipe is loaded on its Sparks.
         * @enum {string}
         */
        PlacementLoadState: "loaded" | "not_loaded" | "unknown";
        /** PlatformObservation */
        PlatformObservation: {
            api: components["schemas"]["ApiRuntimeObservation"];
            /** Capabilities */
            capabilities?: components["schemas"]["CapabilityStatus"][];
            /**
             * Observed At
             * Format: date-time
             */
            observed_at: string;
            /** Worker Issue */
            worker_issue: ("worker-observation-unavailable" | "worker-provenance-unavailable") | null;
            /** Workers */
            workers: components["schemas"]["WorkerRuntimeObservation"][] | null;
        };
        /**
         * PrebuiltImage
         * @description One catalog-pinned prebuilt runtime image for a recipe revision.
         */
        PrebuiltImage: {
            /** Build Key */
            build_key: string;
            /** Reference */
            reference: string;
        };
        /**
         * PrebuiltImageCode
         * @description Why a prebuilt runtime image is, or is not, used.
         * @enum {string}
         */
        PrebuiltImageCode: "prebuilt.build_key_mismatch" | "prebuilt.not_pinned" | "prebuilt.pull_failed_recently" | "prebuilt.used";
        /** PreparationReason */
        PreparationReason: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Node Ids */
            node_ids?: string[];
            /**
             * Severity
             * @enum {string}
             */
            severity: "blocker" | "warning" | "info";
        };
        /**
         * ProfileAction
         * @description Fleet profile Action contract words.
         * @enum {string}
         */
        ProfileAction: "switch" | "keep" | "adopt";
        /**
         * ProfileCancellationCause
         * @description Fleet profile CancellationCause contract words.
         * @enum {string}
         */
        ProfileCancellationCause: "operator" | "superseded";
        /**
         * ProfileChildJobKind
         * @description Fleet profile ChildJobKind contract words.
         * @enum {string}
         */
        ProfileChildJobKind: "recipe.run-switch.v2" | "recipe.stop.v2" | "recipe.cleanup.v2";
        /**
         * ProfileChildPhase
         * @description Fleet profile ChildPhase contract words.
         * @enum {string}
         */
        ProfileChildPhase: "model-download" | "container-download" | "container-build" | "target-copy" | "runtime-install" | "start" | "final-verify" | "transfer" | "verify" | "prepare" | "cleanup" | "stop" | "uninstall";
        /**
         * ProfileChildSource
         * @description Fleet profile ChildSource contract words.
         * @enum {string}
         */
        ProfileChildSource: "switch-adapter";
        /**
         * ProfileDocumentState
         * @description Profile views and catalogue documents used by profile resolution.
         * @enum {string}
         */
        ProfileDocumentState: "active" | "draft" | "ready" | "loaded" | "not-created";
        /**
         * ProfileEffectState
         * @description Fleet profile EffectState contract words.
         * @enum {string}
         */
        ProfileEffectState: "not-issued" | "pending" | "succeeded" | "failed" | "cancelled" | "unknown";
        /**
         * ProfileInstallationPolicy
         * @description Fleet profile InstallationPolicy contract words.
         * @enum {string}
         */
        ProfileInstallationPolicy: "keep-cached" | "exact";
        /**
         * ProfileJobRunStopAuthorization
         * @description Current accepted profile Stop owns this immutable JobRun scope.
         */
        ProfileJobRunStopAuthorization: {
            /** Installation Id */
            installation_id: string;
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Missing Node Ids */
            missing_node_ids?: string[];
            /** Plan Digest */
            plan_digest: string;
            /** Profile Application Id */
            profile_application_id: string;
            /** Profile Digest */
            profile_digest: string;
            /** Profile Operation Id */
            profile_operation_id: string;
            /** Profile Plan Digest */
            profile_plan_digest: string;
            /** Profile Step */
            profile_step: number | ExactNumber;
            /** Reachable Node Ids */
            reachable_node_ids: string[];
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Run Generation */
            run_generation: number | ExactNumber;
            /** Run Id */
            run_id: string;
            /** Run Node Ids */
            run_node_ids: string[];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Stop Plan Digest */
            stop_plan_digest: string;
            /** Targets */
            targets?: components["schemas"]["ProfileJobRunStopTarget"][];
            /** Unissued Artifact Job Ids */
            unissued_artifact_job_ids?: string[];
            /** Workload Intent Ordinal */
            workload_intent_ordinal: number;
        };
        /**
         * ProfileJobRunStopJob
         * @description The one-shot Stop parent persisted for a current profile owner.
         */
        ProfileJobRunStopJob: {
            /**
             * Execution Mode
             * @constant
             */
            execution_mode: "profile-jobrun-stop";
            /** @default null */
            offline_stop_intent?: components["schemas"]["OfflineStopIntent"] | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @constant
             */
            owner_kind: "run";
            /** Phases */
            phases: components["schemas"]["ProfileJobRunStopPhaseItem"][][];
            /** Plan Digest */
            plan_digest: string;
            /** Profile Application Id */
            profile_application_id: string;
            /** Profile Operation Id */
            profile_operation_id: string;
            profile_stop_authorization: components["schemas"]["ProfileJobRunStopAuthorization"];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Workload Intent Ordinal */
            workload_intent_ordinal: number;
        };
        /** ProfileJobRunStopPhaseItem */
        ProfileJobRunStopPhaseItem: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeStopPayload"];
        };
        /**
         * ProfileJobRunStopTarget
         * @description One exact transient runtime and its durable JobRun source.
         */
        ProfileJobRunStopTarget: {
            /** Artifact Job Id */
            artifact_job_id: string;
            /** Node Id */
            node_id: string;
            /** Source Job Id */
            source_job_id: string;
            /** Source Operation Id */
            source_operation_id: string;
            /** Stop Payload Sha256 */
            stop_payload_sha256: string;
        };
        /**
         * ProfileOperationKind
         * @description Fleet profile OperationKind contract words.
         * @enum {string}
         */
        ProfileOperationKind: "fleet-profile.apply";
        /**
         * ProfilePartialStop
         * @description The reachable ranks a profile Stop reaches and the ranks it cannot.
         */
        ProfilePartialStop: {
            /** Missing Node Ids */
            missing_node_ids: string[];
            /** Target Node Ids */
            target_node_ids: string[];
        };
        /**
         * ProfileProjectionKind
         * @description Typed identities in profile effects and operation projections.
         * @enum {string}
         */
        ProfileProjectionKind: "job" | "profile-application" | "profile-step" | "agent-operation" | "fleet-profile-application";
        /**
         * ProfileReasonCode
         * @description Reasons a fleet profile cannot be applied or is waiting.
         * @enum {string}
         */
        ProfileReasonCode: "profile.admission_busy" | "profile.admission_effect_busy" | "profile.application_intent.invalid" | "profile.choices_unreadable" | "profile.definition_unavailable" | "profile.cleanup_delegated" | "profile.distributed_cross_scope" | "profile.failure_repeated" | "profile.incomplete_multi_spark_model" | "profile.interruption_expected" | "profile.pending_cross_scope" | "profile.preparation_not_started" | "profile.preparation_scope_mismatch" | "profile.preparation_unavailable" | "profile.recipe_unavailable" | "profile.recovery_assignments_changed" | "profile.recovery_cache_pending" | "profile.recovery_scope_changed" | "profile.recovery_waiting" | "profile.resource_recheck_unavailable" | "profile.retry_conflict" | "profile.retry_executor_unavailable" | "profile.retry_intent_unavailable" | "profile.retry_review_unavailable" | "profile.review_stale" | "profile.runtime_image_rebuild_pending" | "profile.shared_installation_scope" | "profile.spark_removed" | "profile.spark_unavailable" | "profile.stale_plan" | "profile.switch_authority_unavailable" | "profile.switch_scope_unresolved" | "profile.topology_incomplete" | "profile.recovery_artifact_changed" | "profile.runtime-image-changed" | "profile.selection_lost" | "profile.asset_reservation_unavailable";
        /**
         * ProfileReasonSeverity
         * @description A profile reason and its upstream assessment severity.
         * @enum {string}
         */
        ProfileReasonSeverity: "info" | "warning" | "error" | "blocker";
        /**
         * ProfileReportedPhase
         * @description Run-switch phase projected into a profile child checkpoint.
         * @enum {string}
         */
        ProfileReportedPhase: "final_verify";
        /**
         * ProfileRetryDisposition
         * @description An accepted profile intent either retries or ends as superseded.
         * @enum {string}
         */
        ProfileRetryDisposition: "wait" | "supersede";
        /**
         * ProfileStopOwnerBinding
         * @description The canonical accepted profile operation that owns this exact Stop.
         */
        ProfileStopOwnerBinding: {
            /** Installation Id */
            installation_id: string;
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Missing Node Ids */
            missing_node_ids?: string[];
            /** Plan Digest */
            plan_digest: string;
            /** Profile Application Id */
            profile_application_id: string;
            /** Profile Digest */
            profile_digest: string;
            /** Profile Operation Id */
            profile_operation_id: string;
            /** Profile Plan Digest */
            profile_plan_digest: string;
            /** Profile Step */
            profile_step: number | ExactNumber;
            /** Reachable Node Ids */
            reachable_node_ids: string[];
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Run Generation */
            run_generation: number | ExactNumber;
            /** Run Id */
            run_id: string;
            /** Run Node Ids */
            run_node_ids: string[];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Stop Plan Digest */
            stop_plan_digest: string;
            /** Workload Intent Ordinal */
            workload_intent_ordinal: number;
        };
        /**
         * ProfileSwitchChildKind
         * @description Fleet profile SwitchChildKind contract words.
         * @enum {string}
         */
        ProfileSwitchChildKind: "install" | "run" | "stop" | "cleanup";
        /**
         * ProgressPhase
         * @description What an operation is doing, as the measured progress names it.
         * @enum {string}
         */
        ProgressPhase: "queued" | "pending" | "waiting" | "preparing" | "downloading" | "model-download" | "verifying" | "finalizing" | "building" | "pulling" | "transfer" | "copying" | "uploading" | "installing" | "reconciling-installation" | "starting" | "stopping" | "uninstalling" | "reclaiming" | "updating" | "executing" | "completed" | "failed";
        /**
         * ProjectionCode
         * @description Warnings and attention items of the fleet and library projections.
         * @enum {string}
         */
        ProjectionCode: "cpu.low-clock" | "fleet.frame_budget_exceeded" | "fleet.frame_encoding_unavailable" | "fleet.stored_event_payload_unavailable" | "observation.transfer_unavailable" | "install.partial" | "inventory.missing" | "inventory.stale" | "network.nas-route-wifi-no-wired-port" | "network.nas-route-wifi-wired-port-down" | "network.nas-route-wifi-wired-port-unused" | "node.offline" | "profile.retrying" | "recipe.update_available" | "run.degraded" | "telemetry.delayed" | "telemetry.missing" | "telemetry.stale";
        /** ProjectionReason */
        ProjectionReason: {
            code: components["schemas"]["ProjectionCode"];
            /** Detail */
            detail: string;
            install_partial?: components["schemas"]["InstallPartialEvidence"] | null;
            /** Recommendation */
            recommendation?: string | null;
            /**
             * Severity
             * @enum {string}
             */
            severity: "info" | "warning" | "error";
        };
        /**
         * ReasonCodeVocabulary
         * @description Carrier that publishes every reason-code enum into the wire schema.
         *
         *     The model is never sent: it exists so the schema exporter, the Rust
         *     generator and the OpenAPI/TypeScript generators emit each closed word set
         *     from this one module.
         */
        ReasonCodeVocabulary: {
            admission_code: components["schemas"]["AdmissionCode"];
            agent_evidence_code: components["schemas"]["AgentEvidenceCode"];
            artifact_lifecycle_code: components["schemas"]["ArtifactLifecycleCode"];
            cache_reference_reason: components["schemas"]["CacheReferenceReason"];
            catalog_code: components["schemas"]["CatalogCode"];
            catalog_sync_code: components["schemas"]["CatalogSyncCode"];
            certificate_code: components["schemas"]["CertificateCode"];
            cluster_mapping_code: components["schemas"]["ClusterMappingCode"];
            controller_error_code: components["schemas"]["ControllerErrorCode"];
            distribution_code: components["schemas"]["DistributionCode"];
            helper_error_code: components["schemas"]["HelperErrorCode"];
            helper_operation_code: components["schemas"]["HelperOperationCode"];
            image_store_code: components["schemas"]["ImageStoreCode"];
            install_admission_code: components["schemas"]["InstallAdmissionCode"];
            install_degraded_reason: components["schemas"]["InstallDegradedReason"];
            library_assessment_code: components["schemas"]["LibraryAssessmentCode"];
            library_projection_code: components["schemas"]["LibraryProjectionCode"];
            model_cache_blocker_code: components["schemas"]["ModelCacheBlockerCode"];
            model_cache_code: components["schemas"]["ModelCacheCode"];
            node_offline_reason: components["schemas"]["NodeOfflineReason"];
            operation_failure_code: components["schemas"]["OperationFailureCode"];
            prebuilt_image_code: components["schemas"]["PrebuiltImageCode"];
            profile_reason_code: components["schemas"]["ProfileReasonCode"];
            projection_code: components["schemas"]["ProjectionCode"];
            recipe_build_code: components["schemas"]["RecipeBuildCode"];
            recipe_image_code: components["schemas"]["RecipeImageCode"];
            recipe_operation_code: components["schemas"]["RecipeOperationCode"];
            recipe_package_code: components["schemas"]["RecipePackageCode"];
            recipe_update_code: components["schemas"]["RecipeUpdateCode"];
            reconcile_code: components["schemas"]["ReconcileCode"];
            resource_planning_code: components["schemas"]["ResourcePlanningCode"];
            resource_term: components["schemas"]["ResourceTerm"];
            resource_term_problem: components["schemas"]["ResourceTermProblem"];
            run_degraded_reason: components["schemas"]["RunDegradedReason"];
            run_switch_code: components["schemas"]["RunSwitchCode"];
            runtime_image_code: components["schemas"]["RuntimeImageCode"];
            runtime_preflight_code: components["schemas"]["RuntimePreflightCode"];
            runtime_preflight_finding_code: components["schemas"]["RuntimePreflightFindingCode"];
            source_bundle_code: components["schemas"]["SourceBundleCode"];
            source_policy_code: components["schemas"]["SourcePolicyCode"];
            stop_plan_code: components["schemas"]["StopPlanCode"];
            storage_demand_code: components["schemas"]["StorageDemandCode"];
            supersede_code: components["schemas"]["SupersedeCode"];
            topology_code: components["schemas"]["TopologyCode"];
            uninstall_plan_code: components["schemas"]["UninstallPlanCode"];
        };
        /**
         * RecipeAlternative
         * @description One other recipe serving the same model, for a one-line comparison.
         */
        RecipeAlternative: {
            /**
             * Cache
             * @enum {string}
             */
            cache: "cached" | "preparing" | "not_cached" | "failed" | "unknown";
            /** Creator */
            creator?: string | null;
            /** Engine */
            engine: string;
            /**
             * Fits Fleet
             * @enum {string}
             */
            fits_fleet: "ready" | "blocked" | "unavailable";
            /** Node Count */
            node_count: number | ExactNumber;
            /** Selector */
            selector: string;
            /** Title */
            title: string;
            /** Version */
            version: string;
        };
        /**
         * RecipeBuildAdapter
         * @description The adaptation stage applied after the recipe image is built.
         *
         *     ``adapter_sha256`` is the canonical digest of ``definition``.  The agent
         *     re-derives it from the received definition and refuses to adapt when they
         *     disagree, so a Controller/agent drift cannot silently install a different
         *     adaptation than the one the prepared-image identity recorded.
         */
        RecipeBuildAdapter: {
            /** Adapter Sha256 */
            adapter_sha256: string;
            definition: components["schemas"]["RecipeBuildAdapterDefinition"];
        };
        /**
         * RecipeBuildAdapterDefinition
         * @description One reviewed, digest-identified platform adaptation of a built image.
         *
         *     The recipe Dockerfile owns the pinned upstream runtime, compilation, model
         *     patches and engine arguments.  The platform owns the final Vonk contract
         *     layered on top of that built image: the ``ai.vonkforge.runtime-interface``
         *     label, the canonical ``/opt/vonk/bin/<engine>`` launcher and the runtime
         *     user/ownership.  Hosting that in one reviewed adapter instead of recipe
         *     boilerplate requires an identity that changes when the adaptation changes,
         *     so this definition -- not the compatible ``v1`` label -- is what the digest
         *     covers.  ``containerfile`` is the rendered, ordered adaptation stage and is
         *     the only content the builder executes.
         */
        RecipeBuildAdapterDefinition: {
            /** Adapter Id */
            adapter_id: string;
            /** Containerfile */
            containerfile: string;
            /** Engine */
            engine: string;
            /** Image User */
            image_user: string;
        };
        /** RecipeBuildAdditionalContext */
        RecipeBuildAdditionalContext: {
            /** Name */
            name: string;
            /** Path */
            path: string;
        };
        /** RecipeBuildBaseImage */
        RecipeBuildBaseImage: {
            /** Manifest Digest */
            manifest_digest: string;
            /** Reference */
            reference: string;
        };
        /**
         * RecipeBuildCleanupEvidence
         * @description The build service is stopped; the fenced attempt is the whole answer.
         */
        RecipeBuildCleanupEvidence: Record<string, never>;
        /** RecipeBuildCleanupParent */
        RecipeBuildCleanupParent: {
            /** @default null */
            build_cancellation?: components["schemas"]["RecipeOperationCancellationResult"] | null;
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["BuildCleanupPhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * RecipeBuildCleanupRequest
         * @description Stop only the transient service belonging to one retained build attempt.
         */
        RecipeBuildCleanupRequest: {
            /** Build Id */
            build_id: string;
            /** Operation Id */
            operation_id: string;
        };
        /**
         * RecipeBuildCode
         * @description Recipe image build planning and recording problems.
         * @enum {string}
         */
        RecipeBuildCode: "build.adapter_unavailable" | "build.cancellation_pending" | "build.capability_missing" | "build.capacity_busy" | "build.capacity_contract_invalid" | "build.consumer_busy" | "build.consumer_invalid" | "build.contract_invalid" | "build.dependencies_stale" | "build.evidence_invalid" | "build.image_size_invalid" | "build.input_mismatch" | "build.insufficient_disk" | "build.insufficient_memory" | "build.inventory_missing" | "build.inventory_stale" | "build.network_capability_missing" | "build.node_incompatible" | "build.node_unknown" | "build.plan_invalid" | "build.producer_invalid" | "build.recipe_unresolved" | "build.resolution_stale" | "build.resources_invalid" | "build.result_conflict" | "build.runtime_changed" | "build.security_invalid" | "build.source_invalid" | "build.source_unavailable" | "build.state" | "build.shared_consumers";
        /** RecipeBuildDefinition */
        RecipeBuildDefinition: {
            base_image: components["schemas"]["RecipeImage"];
            context: components["schemas"]["BuildContext"];
            /** Dockerfile */
            dockerfile: string;
            network: components["schemas"]["BuildNetwork"];
            /** Patches */
            patches: components["schemas"]["BuildPatch"][];
        };
        /**
         * RecipeBuildDependency
         * @description A request persisted before dispatch, then its exact lifecycle child.
         */
        RecipeBuildDependency: {
            /**
             * Operation Id
             * @default null
             */
            operation_id?: string | null;
            /**
             * Request Key
             * Format: uuid
             */
            request_key: string;
        };
        /** RecipeBuildEnvironmentArgument */
        RecipeBuildEnvironmentArgument: {
            /** Name */
            name: string;
            /** Value */
            value: string | (number | ExactNumber) | boolean;
        };
        /** RecipeBuildEvidence */
        RecipeBuildEvidence: {
            /** Image Bytes */
            image_bytes: number;
            /** Image Digest */
            image_digest: string;
            /** Oci Layout Sha256 */
            oci_layout_sha256: string;
        };
        /**
         * RecipeBuildIntent
         * @description The accepted producer's intent, independent of its current consumers.
         */
        RecipeBuildIntent: {
            /**
             * Kind
             * @enum {string}
             */
            kind: "independent" | "dependency";
        };
        /**
         * RecipeBuildLimits
         * @description Resource limits; builds never get a GPU, privileges, host mounts or a
         *     container socket.
         */
        RecipeBuildLimits: {
            /** Cpu Cores */
            cpu_cores: number;
            /** Memory Bytes */
            memory_bytes: number;
            /** Output Bytes */
            output_bytes: number;
            /** Processes */
            processes: number;
            /** Temporary Bytes */
            temporary_bytes: number;
            /** Timeout Seconds */
            timeout_seconds: number;
        };
        /** RecipeBuildMetadata */
        RecipeBuildMetadata: {
            /** Name */
            name: string;
            /** Value */
            value: string;
        };
        /**
         * RecipeBuildNetwork
         * @description Public hosts the build may reach; an empty list builds offline.
         */
        RecipeBuildNetwork: {
            /** Hosts */
            hosts: string[];
        };
        /** RecipeBuildOptions */
        RecipeBuildOptions: {
            /** Additional Contexts */
            additional_contexts: components["schemas"]["RecipeBuildAdditionalContext"][];
            /** Annotations */
            annotations: components["schemas"]["RecipeBuildMetadata"][];
            /** Environment */
            environment: components["schemas"]["RecipeBuildEnvironmentArgument"][];
            /**
             * Format
             * @enum {string}
             */
            format: "oci" | "docker";
            /** Identity Label */
            identity_label: boolean;
            /**
             * Ignorefile
             * @default null
             */
            ignorefile?: string | null;
            /** Jobs */
            jobs: number;
            /** Labels */
            labels: components["schemas"]["RecipeBuildMetadata"][];
            /**
             * Layer Compression
             * @enum {string}
             */
            layer_compression: "disabled" | "gzip";
            /** Layer Labels */
            layer_labels: components["schemas"]["RecipeBuildMetadata"][];
            /** Layers */
            layers: boolean;
            /** No Hostname */
            no_hostname: boolean;
            /** No Hosts */
            no_hosts: boolean;
            /** Omit History */
            omit_history: boolean;
            /** Os Features */
            os_features: string[];
            /**
             * Os Version
             * @default null
             */
            os_version?: string | null;
            /** Shm Bytes */
            shm_bytes: number;
            /** Skip Unused Stages */
            skip_unused_stages: boolean;
            /**
             * Squash
             * @enum {string}
             */
            squash: "none" | "new" | "all";
            /**
             * Timestamp
             * @default null
             */
            timestamp?: number | null;
            /** Unset Environment */
            unset_environment: string[];
            /** Unset Labels */
            unset_labels: string[];
        };
        /** RecipeBuildParent */
        RecipeBuildParent: {
            /** @default null */
            build_intent?: components["schemas"]["RecipeBuildIntent"] | null;
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /**
             * Force Rebuild
             * @default null
             */
            force_rebuild?: boolean | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["BuildPhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /**
             * Prebuilt Claim Owner
             * @default null
             */
            prebuilt_claim_owner?: string | null;
            /**
             * Prebuilt Claim Until
             * @default null
             */
            prebuilt_claim_until?: string | null;
            /**
             * Prebuilt Image
             * @default null
             */
            prebuilt_image?: string | null;
            /**
             * Prebuilt Node Id
             * @default null
             */
            prebuilt_node_id?: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * RecipeBuildRequest
         * @description Build one recipe image for linux/arm64.
         */
        RecipeBuildRequest: {
            adapter: components["schemas"]["RecipeBuildAdapter"];
            /** Base Image Storage Bytes */
            base_image_storage_bytes: number;
            /** Base Images */
            base_images: components["schemas"]["RecipeBuildBaseImage"][];
            /** Build Id */
            build_id: string;
            /** Build Input Sha256 */
            build_input_sha256: string;
            /** Capabilities */
            capabilities: string[];
            /** Dockerfile */
            dockerfile: string;
            limits: components["schemas"]["RecipeBuildLimits"];
            network: components["schemas"]["RecipeBuildNetwork"];
            options: components["schemas"]["RecipeBuildOptions"];
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Source Bundle Bytes */
            source_bundle_bytes: number;
            /** Source Bundle Sha256 */
            source_bundle_sha256: string;
        };
        /**
         * RecipeCacheRemovalCheckpoint
         * @description Durable resumable progress for a recipe image and model-cache removal.
         */
        RecipeCacheRemovalCheckpoint: {
            failure: components["schemas"]["AvailabilityOperationFailure"] | null;
            /** Image Index */
            image_index: number | ExactNumber;
            /** Image Pending Bytes */
            image_pending_bytes: (number | ExactNumber) | null;
            /** Image Reclaimed Bytes */
            image_reclaimed_bytes: number | ExactNumber;
            /** Model Index */
            model_index: number | ExactNumber;
            /** Model Reclaimed Bytes */
            model_reclaimed_bytes: number | ExactNumber;
            /** Retry Attempts */
            retry_attempts: number | ExactNumber;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Scope Pending
             * @default false
             */
            scope_pending?: boolean;
        };
        /**
         * RecipeCacheRemovalIntent
         * @description Exact accepted removal request stored on its existing Job owner.
         */
        RecipeCacheRemovalIntent: {
            /**
             * Action
             * @constant
             */
            action: "remove";
            /** Actor */
            actor: string;
            /**
             * Kind
             * @constant
             */
            kind: "recipe.cache.remove.v2";
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Removal Fence */
            removal_fence: string;
            /** Request Key */
            request_key: string;
            /** Review Digest */
            review_digest: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Selector */
            selector: string;
            /** With Model */
            with_model: boolean;
        };
        /**
         * RecipeCacheRemovalModelChild
         * @description One disjoint model-set removal child accepted with the recipe intent.
         */
        RecipeCacheRemovalModelChild: {
            /** Operation Id */
            operation_id: string;
            /** Plan Digest */
            plan_digest: string;
            /** Request Key */
            request_key: string;
            /** Selected Sets */
            selected_sets: string[];
        };
        /**
         * RecipeCacheRemovalOwner
         * @description One accepted exact plan plus its canonical durable effect checkpoint.
         */
        RecipeCacheRemovalOwner: {
            checkpoint: components["schemas"]["RecipeCacheRemovalCheckpoint"];
            plan: components["schemas"]["RecipeCacheRemovalPlan"];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /**
         * RecipeCacheRemovalPlan
         * @description Immutable exact targets bound to the current request owner.
         */
        RecipeCacheRemovalPlan: {
            /** Image Archives */
            image_archives: string[];
            intent: components["schemas"]["RecipeCacheRemovalIntent"];
            /** Model Children */
            model_children: components["schemas"]["RecipeCacheRemovalModelChild"][];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /**
         * RecipeCacheRemovalResult
         * @description Stored terminal summary, bound to the Job's immutable intent.
         */
        RecipeCacheRemovalResult: {
            /**
             * Action
             * @constant
             */
            action: "remove";
            /** Cancelled Builds */
            cancelled_builds: string[];
            /** Cancelled Operations */
            cancelled_operations: string[];
            /** Model Removals */
            model_removals: string[];
            /** Next Actions */
            next_actions: string[];
            /** Operation Id */
            operation_id: string;
            /** Preserved */
            preserved: string[];
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Reclaimed Bytes */
            reclaimed_bytes: number | ExactNumber;
            /** Request Key */
            request_key: string;
            /** Review Digest */
            review_digest: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Selector */
            selector: string;
            /**
             * State
             * @constant
             */
            state: "succeeded";
            /** With Model */
            with_model: boolean;
        };
        /** RecipeCancellationRequest */
        RecipeCancellationRequest: {
            /** Reason */
            reason: string;
            /** Request Key */
            request_key: string;
        };
        /**
         * RecipeDefinition
         * @description The sole public recipe authoring contract.
         */
        RecipeDefinition: {
            execution: components["schemas"]["RecipeExecution"];
            identity: components["schemas"]["RecipeIdentity"];
            /** Interfaces */
            interfaces: (components["schemas"]["RecipeOpenAIInterface"] | components["schemas"]["RecipeJobInterface"])[];
            /**
             * Kind
             * @default recipe
             * @constant
             */
            kind?: "recipe";
            metadata: components["schemas"]["RecipeMetadata"];
            /** Models */
            models: components["schemas"]["RecipeModelSelection"][];
            /** Options */
            options?: components["schemas"]["RecipeOption"][];
            provenance: components["schemas"]["RecipeProvenance"];
            release: components["schemas"]["RecipeRelease"];
            runtime: components["schemas"]["RecipeRuntime"];
            /** Settings */
            settings: components["schemas"]["RecipeGenerationSettings"] | components["schemas"]["RecipeEmbeddingSettings"] | components["schemas"]["RecipeJobSettings"];
            topology: components["schemas"]["RecipeTopology"];
            validation: components["schemas"]["RecipeValidation"];
        };
        /** RecipeDetailResponse */
        RecipeDetailResponse: {
            /** Alignment */
            alignment?: string | null;
            /** Alternatives */
            alternatives?: components["schemas"]["RecipeAlternative"][];
            assessment?: components["schemas"]["RecipeReadiness"] | null;
            /** Creator */
            creator?: string | null;
            document: components["schemas"]["RecipeDefinition"];
            /** Engine */
            engine: string;
            identity: components["schemas"]["LibraryRecipeIdentity"];
            local: components["schemas"]["LibraryLocalState"];
            /** Model Documents */
            model_documents: components["schemas"]["LibraryRecipeModel"][];
            /** Model Selectors */
            model_selectors: string[];
            /** Node Count */
            node_count: number | ExactNumber;
            resources: components["schemas"]["LibraryResourceProjection"];
            /** Selector */
            selector: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Usage */
            usage: string[];
        };
        /** RecipeDiskResources */
        RecipeDiskResources: {
            /** Artifact Bytes */
            artifact_bytes: number | ExactNumber;
            /** Image Bytes */
            image_bytes: number | ExactNumber;
            /** Safety Margin Bytes */
            safety_margin_bytes: number | ExactNumber;
            /** Working Bytes */
            working_bytes: number | ExactNumber;
        };
        /** RecipeDownloadRequest */
        RecipeDownloadRequest: {
            /** Request Key */
            request_key: string;
        };
        /** RecipeEmbeddingSettings */
        RecipeEmbeddingSettings: {
            concurrency?: components["schemas"]["RecipeIntegerSetting"] | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "embedding";
            /** Knobs */
            knobs?: {
                [key: string]: components["schemas"]["RecipeSetting"];
            };
            max_batch_tokens?: components["schemas"]["RecipeIntegerSetting"] | null;
        };
        /**
         * RecipeExecution
         * @description The platform builds every recipe image from its pinned base and context.
         */
        RecipeExecution: {
            build: components["schemas"]["RecipeBuildDefinition"];
        };
        /** RecipeGenerationSettings */
        RecipeGenerationSettings: {
            concurrency?: components["schemas"]["RecipeIntegerSetting"] | null;
            context_tokens: components["schemas"]["RecipeIntegerSetting"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "generation";
            /** Knobs */
            knobs?: {
                [key: string]: components["schemas"]["RecipeSetting"];
            };
            max_batch_tokens?: components["schemas"]["RecipeIntegerSetting"] | null;
        };
        /** RecipeHttpServingRequest */
        RecipeHttpServingRequest: {
            /** Body */
            body?: {
                [key: string]: components["schemas"]["vonk_forge_contracts__recipe__JsonValue"];
            } | null;
            /**
             * Method
             * @enum {string}
             */
            method: "GET" | "POST";
            /** Path */
            path: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            transport: "http";
        };
        /** RecipeIdentity */
        RecipeIdentity: {
            /** Publisher */
            publisher: string;
            /** Slug */
            slug: string;
        };
        /** RecipeImage */
        RecipeImage: {
            /** Digest */
            digest: string;
            /** Repository */
            repository: string;
        };
        /** RecipeImageAvailabilityAction */
        RecipeImageAvailabilityAction: {
            key: components["schemas"]["AvailabilityRecoveryAction"];
        };
        /** RecipeImageAvailabilityArtifact */
        RecipeImageAvailabilityArtifact: {
            /** Download Bytes */
            download_bytes: number | ExactNumber;
            /** Id */
            id: string;
            /** Key */
            key: string;
            /** Kind */
            kind: string;
            /** Model Content Sha256 */
            model_content_sha256?: string | null;
            /** Path */
            path: string;
            /** Repository */
            repository?: string | null;
            /** Revision */
            revision?: string | null;
            /** Roles */
            roles: string[];
            /** Sha256 */
            sha256: string;
            /** Source */
            source: string;
        };
        /** RecipeImageAvailabilityChild */
        RecipeImageAvailabilityChild: {
            /** Artifact Set Sha256 */
            artifact_set_sha256?: string | null;
            /** Artifacts */
            artifacts?: components["schemas"]["RecipeImageAvailabilityArtifact"][];
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "model-cache" | "runtime-image";
            /** Model Content Digests */
            model_content_digests: string[];
            /** Plan Digest */
            plan_digest?: string | null;
            progress: components["schemas"]["OperationProgress"];
            /** Request Key */
            request_key?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "backoff" | "observing" | "succeeded" | "failed" | "cancelled";
        };
        /** RecipeImageAvailabilityResponse */
        RecipeImageAvailabilityResponse: {
            /** Actions */
            actions?: components["schemas"]["RecipeImageAvailabilityAction"][];
            /** Attempt */
            attempt: number;
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            cancellation?: components["schemas"]["RecipeOperationCancellationResult"] | null;
            /** Children */
            children?: components["schemas"]["RecipeImageAvailabilityChild"][];
            /** Created At */
            created_at: string;
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /** Id */
            id: string;
            /**
             * Kind
             * @constant
             */
            kind: "recipe.image.availability.v2";
            /** Next Attempt At */
            next_attempt_at?: string | null;
            progress: components["schemas"]["OperationProgress"] | null;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string | null;
            /** Recipe Revision Id */
            recipe_revision_id: string | null;
            /** Request */
            request: (components["schemas"]["RecipeSelectorIntent"] | components["schemas"]["RecipeRevisionIntent"] | components["schemas"]["RecipeRetryIntent"]) | null;
            /** Request Id */
            request_id: string;
            residue?: components["schemas"]["Residue"] | null;
            result?: components["schemas"]["RecipeImageAvailabilityResult"] | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "backoff" | "observing" | "succeeded" | "failed" | "cancelled";
            /** Updated At */
            updated_at: string;
        };
        /** RecipeImageAvailabilityResult */
        RecipeImageAvailabilityResult: {
            /** Artifact Set Sha256 */
            artifact_set_sha256?: string | null;
            /** Build Id */
            build_id?: string | null;
            /** Build Input Sha256 */
            build_input_sha256?: string | null;
            /** Image Bytes */
            image_bytes: number | ExactNumber;
            /** Image Digest */
            image_digest: string;
            /** Local Image Config Id */
            local_image_config_id?: string | null;
            /** Model Child Id */
            model_child_id?: string | null;
            /** Model Content Digests */
            model_content_digests: string[];
            /** Model Digest */
            model_digest?: string | null;
            /** Oci Archive Sha256 */
            oci_archive_sha256: string;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
        };
        /**
         * RecipeImageCode
         * @description Runtime image availability, preparation and cache-removal problems.
         * @enum {string}
         */
        RecipeImageCode: "recipe_image.action_invalid" | "recipe_image.build_cancelled" | "recipe_image.build_capacity_wait" | "recipe_image.build_failed" | "recipe_image.build_input_missing" | "recipe_image.build_invalid" | "recipe_image.build_unavailable" | "recipe_image.build_wait" | "recipe_image.builder_busy" | "recipe_image.builder_occupied" | "recipe_image.cancel_busy" | "recipe_image.cancel_request_key_reused" | "recipe_image.cancellation_invalid" | "recipe_image.claim_lost" | "recipe_image.database_busy" | "recipe_image.identity_conflict" | "recipe_image.identity_invalid" | "recipe_image.insufficient_disk" | "recipe_image.insufficient_memory" | "recipe_image.metadata_refresh_failed" | "recipe_image.metadata_refresh_unavailable" | "recipe_image.model_cache_failed" | "recipe_image.model_cache_invalid" | "recipe_image.model_cache_unavailable" | "recipe_image.model_child_cancelled" | "recipe_image.model_child_missing" | "recipe_image.no_builder" | "recipe_image.not_cancellable" | "recipe_image.not_retryable" | "recipe_image.operation_invalid" | "recipe_image.removal_evidence_unavailable" | "recipe_image.operation_missing" | "recipe_image.preparation_failed" | "recipe_image.preparation_exhausted" | "recipe_image.preparing" | "recipe_image.recipe_invalid" | "recipe_image.recipe_unavailable" | "recipe_image.removal_choice_invalid" | "recipe_image.removal_failed" | "recipe_image.removal_referenced" | "recipe_image.removal_scope_limited" | "recipe_image.request_key_reused" | "recipe_image.runtime_invalid" | "recipe_image.selector_ambiguous" | "recipe_image.selector_invalid" | "recipe_image.selector_missing" | "recipe_image.source_policy_refused" | "recipe_image.superseded_by_newer_revision" | "recipe_image.waiting_for_model" | "recipe_image.waiting_for_worker";
        /** RecipeInputSlot */
        RecipeInputSlot: {
            /** Description */
            description: string;
            /** Extensions */
            extensions: string[];
            /** Id */
            id: string;
            /** Label */
            label: string;
            /** Max File Bytes */
            max_file_bytes: number;
            /** Max Files */
            max_files: number;
            /** Max Total Bytes */
            max_total_bytes: number;
            /** Media Types */
            media_types: string[];
            /** Min Files */
            min_files: number;
        };
        /** RecipeInstallParent */
        RecipeInstallParent: {
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["InstallPhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /** RecipeInstallPayload */
        RecipeInstallPayload: {
            compiled_execution_plan: components["schemas"]["CompiledExecutionPlan"];
            /** Expected Bytes */
            expected_bytes: number;
            /** Installation Id */
            installation_id: string;
            /** Plan Digest */
            plan_digest: string;
        };
        /** RecipeInstallationChange */
        RecipeInstallationChange: {
            /** Entity Id */
            entity_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            entity_kind: "recipe-installation";
            fields: components["schemas"]["RecipeInstallationPayload"];
            /** Node Id */
            node_id?: null;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /** RecipeInstallationPayload */
        RecipeInstallationPayload: {
            /** Entity Id */
            entity_id: string;
            /**
             * Entity Kind
             * @constant
             */
            entity_kind: "recipe-installation";
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** State */
            state: string;
        };
        /** RecipeIntegerSetting */
        RecipeIntegerSetting: {
            /**
             * Change Effect
             * @enum {string}
             */
            change_effect: "none" | "restart" | "reprepare" | "rebuild";
            /** Value */
            value: number | ExactNumber;
        };
        /** RecipeJobActivateParent */
        RecipeJobActivateParent: {
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /** Plan Digest */
            plan_digest: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /** RecipeJobEvidence */
        RecipeJobEvidence: {
            /** Elapsed Milliseconds */
            elapsed_milliseconds: number;
            /** Peak Memory Bytes */
            peak_memory_bytes: number | null;
        };
        /** RecipeJobFile */
        RecipeJobFile: {
            /** Media Type */
            media_type: string;
            /** Name */
            name: string;
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number;
        };
        /** RecipeJobInput */
        RecipeJobInput: {
            /** Max Bytes */
            max_bytes: number;
            /** Media Types */
            media_types: string[];
            /** Required */
            required: boolean;
            /** Slots */
            slots?: components["schemas"]["RecipeInputSlot"][] | null;
        };
        /** RecipeJobInputFile */
        RecipeJobInputFile: {
            /** Media Type */
            media_type: string;
            /** Name */
            name: string;
            /** Sha256 */
            sha256: string;
            /** Size Bytes */
            size_bytes: number;
            /** Slot */
            slot: string;
        };
        /**
         * RecipeJobInputManifest
         * @description Declared user files; manifest.json itself is platform metadata.
         */
        RecipeJobInputManifest: {
            /** Files */
            files: components["schemas"]["RecipeJobInputFile"][];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Total Bytes */
            total_bytes: number;
        };
        /** RecipeJobInterface */
        RecipeJobInterface: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            adapter: "artifact-job" | "audio-job" | "image-job" | "mesh-job" | "video-job";
            input?: components["schemas"]["RecipeJobInput"] | null;
            output: components["schemas"]["RecipeJobOutput"];
        };
        /** RecipeJobOutput */
        RecipeJobOutput: {
            /** Max Total Bytes */
            max_total_bytes: number;
            /** Slots */
            slots: components["schemas"]["RecipeOutputSlot"][];
        };
        /** RecipeJobOutputLimits */
        RecipeJobOutputLimits: {
            /** Allowed Media Types */
            allowed_media_types: string[];
            /** Max File Bytes */
            max_file_bytes: number;
            /** Max Files */
            max_files: number;
            /** Max Total Bytes */
            max_total_bytes: number;
        };
        /** RecipeJobOutputManifest */
        RecipeJobOutputManifest: {
            /** Files */
            files: components["schemas"]["RecipeJobFile"][];
            /** Manifest Sha256 */
            manifest_sha256: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Total Bytes */
            total_bytes: number;
        };
        /** RecipeJobOutputMapping */
        RecipeJobOutputMapping: {
            /** Extensions */
            extensions: string[];
            /** Media Type */
            media_type: string;
            /** Slot */
            slot: string;
        };
        /** RecipeJobRunParent */
        RecipeJobRunParent: {
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["JobRunPhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * RecipeJobRunRequest
         * @description One job run; interface, image, placement and timeout come from the plan.
         */
        RecipeJobRunRequest: {
            compiled_execution_plan: components["schemas"]["CompiledExecutionPlan"];
            /** Input Manifest Sha256 */
            input_manifest_sha256: string;
            /** Input Total Bytes */
            input_total_bytes: number;
            /** Inputs */
            inputs: components["schemas"]["RecipeJobInputFile"][];
            /** Installation Id */
            installation_id: string;
            /** Job Id */
            job_id: string;
            /** Mapping Id */
            mapping_id: string;
            output_limits: components["schemas"]["RecipeJobOutputLimits"];
            /** Output Mappings */
            output_mappings: components["schemas"]["RecipeJobOutputMapping"][];
            /** Plan Digest */
            plan_digest: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Run Generation */
            run_generation: number | ExactNumber;
            /** Run Id */
            run_id: string;
        };
        /** RecipeJobRunResult */
        RecipeJobRunResult: {
            /** @default null */
            diagnostics?: components["schemas"]["FailureDiagnostics"] | null;
            evidence: components["schemas"]["RecipeJobEvidence"];
            /** Exit Code */
            exit_code: number;
            /** Job Id */
            job_id: string;
            output_manifest: components["schemas"]["RecipeJobOutputManifest"];
            /**
             * Reason
             * @default null
             */
            reason?: string | null;
            /** Run Id */
            run_id: string;
        };
        /**
         * RecipeJobServingRequest
         * @description A job check stages its fixture as the input when the interface has one.
         */
        RecipeJobServingRequest: {
            /** Fixture */
            fixture: string;
            /** Input Slots */
            input_slots?: {
                [key: string]: string;
            };
            /** Output Slot */
            output_slot: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            transport: "job";
        };
        /** RecipeJobSettings */
        RecipeJobSettings: {
            concurrency?: components["schemas"]["RecipeIntegerSetting"] | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "job";
            /** Knobs */
            knobs?: {
                [key: string]: components["schemas"]["RecipeSetting"];
            };
        };
        /** RecipeLibraryResponse */
        RecipeLibraryResponse: {
            facets: components["schemas"]["LibraryFacetValues"];
            filters?: components["schemas"]["LibraryFilterValues"];
            freshness_policy: components["schemas"]["FreshnessPolicy"];
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            library?: components["schemas"]["LibraryRelease"] | null;
            /** Next Cursor */
            next_cursor: string | null;
            /** Recipes */
            recipes: components["schemas"]["LibraryRecipeProjection"][];
        };
        /** RecipeLifecycle */
        RecipeLifecycle: {
            /** Stop Timeout Seconds */
            stop_timeout_seconds: number;
        };
        /**
         * RecipeMemoryResources
         * @description Unified (DGX Spark) memory one role needs.
         */
        RecipeMemoryResources: {
            /** Peak Bytes */
            peak_bytes: number | ExactNumber;
            /** Reserve Bytes */
            reserve_bytes: number | ExactNumber;
        };
        /** RecipeMetadata */
        RecipeMetadata: {
            /** Alignment */
            alignment?: ("standard" | "abliterated" | "derisked" | "other-modified" | "unspecified") | null;
            /** Description */
            description: string;
            /** Tags */
            tags: string[];
            /** Title */
            title: string;
        };
        /** RecipeModelFile */
        RecipeModelFile: {
            /** File Id */
            file_id: string;
            /** Id */
            id: string;
            mount: components["schemas"]["RecipeMount"];
            /** Roles */
            roles: string[];
        };
        /** RecipeModelSelection */
        RecipeModelSelection: {
            /** Files */
            files: components["schemas"]["RecipeModelFile"][];
            /** Id */
            id: string;
            model: components["schemas"]["ModelReference"];
        };
        /** RecipeMount */
        RecipeMount: {
            /** Target */
            target: string;
        };
        /** RecipeOpenAIInterface */
        RecipeOpenAIInterface: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            adapter: "openai";
            /** Health Path */
            health_path: string;
            /** Model Aliases */
            model_aliases: string[];
            /** Port */
            port: number;
        };
        /** RecipeOperationActivatedResult */
        RecipeOperationActivatedResult: {
            /**
             * Activated
             * @constant
             */
            activated: true;
        };
        /**
         * RecipeOperationCancellationResult
         * @description Cancellation metadata merged into a pending lifecycle result.
         */
        RecipeOperationCancellationResult: {
            /** Cancel Actor */
            cancel_actor: string;
            /** Cancel Request Id */
            cancel_request_id: string;
            /**
             * Cancel Requested
             * @constant
             */
            cancel_requested: true;
            /** Cancel Requested At */
            cancel_requested_at?: string | null;
            /** Cancelled */
            cancelled?: true | null;
            /** Launch Evidence */
            launch_evidence?: {
                [key: string]: components["schemas"]["AgentInstallResult"] | components["schemas"]["RecipeBuildEvidence"] | components["schemas"]["RecipeBuildCleanupEvidence"] | components["schemas"]["RecipeStartResult"] | components["schemas"]["RecipeStopResult"] | components["schemas"]["RecipeUninstallResult"] | components["schemas"]["RecipeReconcileResult"] | components["schemas"]["RemovedRecipeNodeResult"] | components["schemas"]["AgentFailureResult"] | components["schemas"]["LifecycleCodeFailureResult"];
            } | null;
            /** Node Evidence */
            node_evidence?: {
                [key: string]: components["schemas"]["AgentInstallResult"] | components["schemas"]["RecipeBuildEvidence"] | components["schemas"]["RecipeBuildCleanupEvidence"] | components["schemas"]["RecipeStartResult"] | components["schemas"]["RecipeStopResult"] | components["schemas"]["RecipeUninstallResult"] | components["schemas"]["RecipeReconcileResult"] | components["schemas"]["RemovedRecipeNodeResult"] | components["schemas"]["AgentFailureResult"] | components["schemas"]["LifecycleCodeFailureResult"];
            } | null;
            /** Reason */
            reason: string;
            /** Recovery */
            recovery?: "retry creates a new operation" | null;
        };
        /**
         * RecipeOperationCode
         * @description Recipe operation conflicts.
         * @enum {string}
         */
        RecipeOperationCode: "recipe.operation_conflict" | "recipe.evidence_unproven";
        /**
         * RecipeOperationProgressResult
         * @description Partial evidence retained while a multi-node operation is running.
         */
        RecipeOperationProgressResult: {
            /**
             * Launch Evidence
             * @default null
             */
            launch_evidence?: {
                [key: string]: components["schemas"]["AgentInstallResult"] | components["schemas"]["RecipeBuildEvidence"] | components["schemas"]["RecipeBuildCleanupEvidence"] | components["schemas"]["RecipeStartResult"] | components["schemas"]["RecipeStopResult"] | components["schemas"]["RecipeUninstallResult"] | components["schemas"]["RecipeReconcileResult"] | components["schemas"]["RemovedRecipeNodeResult"] | components["schemas"]["AgentFailureResult"] | components["schemas"]["LifecycleCodeFailureResult"];
            } | null;
            /**
             * Node Evidence
             * @default null
             */
            node_evidence?: {
                [key: string]: components["schemas"]["AgentInstallResult"] | components["schemas"]["RecipeBuildEvidence"] | components["schemas"]["RecipeBuildCleanupEvidence"] | components["schemas"]["RecipeStartResult"] | components["schemas"]["RecipeStopResult"] | components["schemas"]["RecipeUninstallResult"] | components["schemas"]["RecipeReconcileResult"] | components["schemas"]["RemovedRecipeNodeResult"] | components["schemas"]["AgentFailureResult"] | components["schemas"]["LifecycleCodeFailureResult"];
            } | null;
        };
        /**
         * RecipeOperationResult
         * @description Terminal aggregate emitted by the durable lifecycle job projector.
         */
        RecipeOperationResult: {
            /** Failed Nodes */
            failed_nodes: string[];
            /**
             * Launch Evidence
             * @default null
             */
            launch_evidence?: {
                [key: string]: components["schemas"]["AgentInstallResult"] | components["schemas"]["RecipeBuildEvidence"] | components["schemas"]["RecipeBuildCleanupEvidence"] | components["schemas"]["RecipeStartResult"] | components["schemas"]["RecipeStopResult"] | components["schemas"]["RecipeUninstallResult"] | components["schemas"]["RecipeReconcileResult"] | components["schemas"]["RemovedRecipeNodeResult"] | components["schemas"]["AgentFailureResult"] | components["schemas"]["LifecycleCodeFailureResult"];
            } | null;
            /** Node Evidence */
            node_evidence: {
                [key: string]: components["schemas"]["AgentInstallResult"] | components["schemas"]["RecipeBuildEvidence"] | components["schemas"]["RecipeBuildCleanupEvidence"] | components["schemas"]["RecipeStartResult"] | components["schemas"]["RecipeStopResult"] | components["schemas"]["RecipeUninstallResult"] | components["schemas"]["RecipeReconcileResult"] | components["schemas"]["RemovedRecipeNodeResult"] | components["schemas"]["AgentFailureResult"] | components["schemas"]["LifecycleCodeFailureResult"];
            };
            /**
             * Recovery Error
             * @default null
             */
            recovery_error?: string | null;
            /**
             * Recovery Route Published
             * @default null
             */
            recovery_route_published?: true | null;
            /** Successful Nodes */
            successful_nodes: string[];
        };
        /** RecipeOperationStoppedResult */
        RecipeOperationStoppedResult: {
            /**
             * Stopped
             * @constant
             */
            stopped: true;
        };
        /** RecipeOperatorRequest */
        RecipeOperatorRequest: {
            /** Request Key */
            request_key: string;
            /** With Model */
            with_model: boolean;
        };
        /** RecipeOperatorResponse */
        RecipeOperatorResponse: {
            /**
             * Action
             * @constant
             */
            action: "remove";
            /** Cancelled Builds */
            cancelled_builds?: string[];
            /** Cancelled Operations */
            cancelled_operations?: string[];
            failure?: components["schemas"]["AvailabilityOperationFailure"] | null;
            /** Model Removals */
            model_removals?: string[];
            /** Next Actions */
            next_actions?: string[];
            /** Operation Id */
            operation_id: string;
            /** Preserved */
            preserved?: string[];
            progress: components["schemas"]["OperationProgress"];
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Reclaimed Bytes */
            reclaimed_bytes: number | ExactNumber;
            /** Request Key */
            request_key: string;
            /** Selector */
            selector: string;
            /**
             * State
             * @enum {string}
             */
            state: "accepted" | "queued" | "running" | "backoff" | "succeeded" | "failed" | "cancelled";
            /** With Model */
            with_model: boolean;
        };
        /**
         * RecipeOption
         * @description A recipe-declared setting with a fixed set of named values.
         *
         *     Users pick one of the enumerated choices; there are no free-form values.
         *     Exactly one choice is the default and applies whenever nothing is chosen.
         */
        RecipeOption: {
            /** Choices */
            choices: components["schemas"]["RecipeOptionChoice"][];
            /** Help */
            help: string;
            /** Label */
            label: string;
            /** Name */
            name: string;
        };
        /**
         * RecipeOptionChoice
         * @description One named value of an option, with the runtime changes it selects.
         *
         *     ``args`` replace a base runtime argument of the same name in place, or are
         *     appended after the base arguments; ``env`` does the same for environment
         *     variables. Both go through the checks of ``runtime.arguments`` and
         *     ``runtime.environment``, and the platform applies its own security, mount
         *     and port rules to the merged result.
         */
        RecipeOptionChoice: {
            /** Args */
            args?: components["schemas"]["RecipeRuntimeArgument"][];
            /**
             * Default
             * @default false
             */
            default?: boolean;
            /** Env */
            env?: {
                [key: string]: string | (number | ExactNumber) | boolean | (number | ExactNumber);
            };
            /** Help */
            help: string;
            /** Label */
            label: string;
            /** Value */
            value: string;
        };
        /** RecipeOutputSlot */
        RecipeOutputSlot: {
            /** Description */
            description: string;
            /** Extensions */
            extensions: string[];
            /** Id */
            id: string;
            /** Label */
            label: string;
            /** Max File Bytes */
            max_file_bytes: number;
            /** Max Files */
            max_files: number;
            /** Max Total Bytes */
            max_total_bytes: number;
            /** Media Types */
            media_types: string[];
            /** Min Files */
            min_files: number;
        };
        /**
         * RecipePackageCode
         * @description Recipe package download and verification problems.
         * @enum {string}
         */
        RecipePackageCode: "recipe_package.cache_unavailable" | "recipe_package.digest_mismatch" | "recipe_package.document_incompatible" | "recipe_package.extract_invalid" | "recipe_package.not_found" | "recipe_package.package_invalid" | "recipe_package.release_incomplete" | "recipe_package.release_invalid" | "recipe_package.response_invalid" | "recipe_package.schema_incompatible" | "recipe_package.snapshot_changed" | "recipe_package.unavailable" | "recipe_package.uri_invalid" | "recipe_package.url_insecure" | "recipe_package.url_invalid";
        /** RecipePackageHandleProjection */
        RecipePackageHandleProjection: {
            /** Archive Path */
            archive_path: string;
            /** Closure Path */
            closure_path: string;
            /** Package Path */
            package_path: string;
            /** Package Sha256 */
            package_sha256: string;
            /** Package Size */
            package_size: number | ExactNumber;
            /** Publication Commit */
            publication_commit: string;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /** Source Commit */
            source_commit: string;
        };
        /** RecipeParallelism */
        RecipeParallelism: {
            /** Backend */
            backend: string;
            /** Data */
            data: number | ExactNumber;
            /** Pipeline */
            pipeline: number | ExactNumber;
            /** Tensor */
            tensor: number | ExactNumber;
        };
        /** RecipePresence */
        RecipePresence: {
            /** Affected Ranks */
            affected_ranks?: number[];
            /** Complete */
            complete: boolean | null;
            degraded_reason?: components["schemas"]["InstallDegradedReason"] | null;
            /** Expected Rank Count */
            expected_rank_count: number;
            group_state: components["schemas"]["InstallationState"];
            /** Installation Id */
            installation_id: string;
            /** Installed Bytes */
            installed_bytes?: (number | ExactNumber) | null;
            /** Member Node Ids */
            member_node_ids: string[];
            /** Present Ranks */
            present_ranks: number[];
            /** Projection Issue */
            projection_issue?: string | null;
            /** Rank */
            rank: number;
            rank_state: components["schemas"]["InstallationState"];
            /** Recipe Id */
            recipe_id: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Required Bytes */
            required_bytes?: (number | ExactNumber) | null;
            /** Role */
            role: string;
            /** Title */
            title: string;
            /** Topology Name */
            topology_name: string;
        };
        /** RecipeProvenance */
        RecipeProvenance: {
            /** Attribution */
            attribution: string[];
            /** Source Reference */
            source_reference?: string | null;
        };
        /** RecipeReadiness */
        RecipeReadiness: {
            cache: components["schemas"]["RecipeReadinessCheck"];
            fit?: components["schemas"]["SparkFit"] | null;
            fleet_fit: components["schemas"]["RecipeReadinessCheck"];
            group?: components["schemas"]["SparkGroup"] | null;
            /**
             * Observed At
             * Format: date-time
             */
            observed_at: string;
            readiness: components["schemas"]["RecipeReadinessCheck"];
        };
        /** RecipeReadinessCheck */
        RecipeReadinessCheck: {
            /** Reasons */
            reasons?: components["schemas"]["RunSwitchReason"][];
            /**
             * State
             * @enum {string}
             */
            state: "ready" | "blocked" | "unavailable";
        };
        /** RecipeReconcileParent */
        RecipeReconcileParent: {
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["ReconcilePhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /** @default null */
            reconciliation_authority?: components["schemas"]["RunSwitchReconciliationAuthority"] | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * RecipeReconcilePayload
         * @description Authority to remove one managed install with an invalid launch contract.
         */
        RecipeReconcilePayload: {
            /** Installation Id */
            installation_id: string;
            /** Plan Digest */
            plan_digest: string;
        };
        /**
         * RecipeReconcileResult
         * @description A reconciliation succeeds with an empty result.
         */
        RecipeReconcileResult: Record<string, never>;
        /**
         * RecipeRelease
         * @description The version this recipe runs.
         *
         *     When the upstream project publishes versions, this is the upstream version
         *     and its release date (for example ``1.6`` released 2026-09-17). A recipe
         *     whose upstream has no versions carries its own semantic version instead.
         *     The recipe library's own version follows the contract, not recipe content.
         */
        RecipeRelease: {
            /** Released At */
            released_at: string;
            /** Version */
            version: string;
        };
        /** RecipeRemovalProjectionIssue */
        RecipeRemovalProjectionIssue: {
            /**
             * Code
             * @default recipe_image.removal_evidence_unavailable
             * @constant
             */
            code?: "recipe_image.removal_evidence_unavailable";
            /** Detail */
            detail: string;
            /** Next Action */
            next_action: string;
        };
        /**
         * RecipeRemovalUnavailableView
         * @description Known Job identity with no claim about unreadable removal effects.
         */
        RecipeRemovalUnavailableView: {
            /**
             * Action
             * @default remove
             * @constant
             */
            action?: "remove";
            /** Failure */
            failure?: null;
            /**
             * Kind
             * @default recipe.cache.remove.v2
             * @constant
             */
            kind?: "recipe.cache.remove.v2";
            /** Observed At */
            observed_at: string;
            /** Operation Id */
            operation_id: string;
            /** Progress */
            progress?: null;
            projection_issue: components["schemas"]["RecipeRemovalProjectionIssue"];
            /** Recipe Revision Id */
            recipe_revision_id: string | null;
            /** Request Key */
            request_key: string | null;
            /**
             * State
             * @default unknown
             * @constant
             */
            state?: "unknown";
        };
        /** RecipeRetryIntent */
        RecipeRetryIntent: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "retry";
            /** Operation Id */
            operation_id: string;
        };
        /** RecipeRevisionIntent */
        RecipeRevisionIntent: {
            /** Build Input Sha256 */
            build_input_sha256?: string | null;
            /** Effective Execution Key */
            effective_execution_key?: string | null;
            /**
             * Force
             * @default false
             */
            force?: boolean;
            /**
             * Force Rebuild
             * @default false
             */
            force_rebuild?: boolean;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "revision";
            /** Model Digest */
            model_digest?: string | null;
            /** Recipe Revision Id */
            recipe_revision_id: string;
        };
        /** RecipeRevisionProjection */
        RecipeRevisionProjection: {
            /**
             * Artifact Inputs
             * @default null
             */
            artifact_inputs?: components["schemas"]["ArtifactInputProjection"][] | null;
            /**
             * Build Model Artifacts
             * @default null
             */
            build_model_artifacts?: components["schemas"]["BuildModelArtifactProjection"][] | null;
            /** @default null */
            build_options?: components["schemas"]["RecipeBuildOptions"] | null;
            /** @default null */
            build_resources?: components["schemas"]["BuildResourcesProjection"] | null;
            /** @default null */
            build_security?: components["schemas"]["BuildSecurityProjection"] | null;
            /** Description */
            description: string;
            /**
             * Failure Reason
             * @default null
             */
            failure_reason?: string | null;
            /** @default null */
            package_handle?: components["schemas"]["RecipePackageHandleProjection"] | null;
            /**
             * Package Sha256
             * @default null
             */
            package_sha256?: string | null;
            /** @default null */
            prebuilt_image?: components["schemas"]["PrebuiltImage"] | null;
            /**
             * Publication Commit
             * @default null
             */
            publication_commit?: string | null;
            /**
             * Release Released At
             * @default null
             */
            release_released_at?: string | null;
            /**
             * Release Version
             * @default null
             */
            release_version?: string | null;
            /** Runtime Engine */
            runtime_engine: string;
            /**
             * Source Bundle Sha256
             * @default null
             */
            source_bundle_sha256?: string | null;
            /**
             * Source Path
             * @default null
             */
            source_path?: string | null;
            /** Tags */
            tags: string[];
            /** Title */
            title: string;
            topology: components["schemas"]["RecipeTopology"];
        };
        /** RecipeRoleResources */
        RecipeRoleResources: {
            disk: components["schemas"]["RecipeDiskResources"];
            memory: components["schemas"]["RecipeMemoryResources"];
        };
        /** RecipeRunChange */
        RecipeRunChange: {
            /** Entity Id */
            entity_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            entity_kind: "recipe-run";
            fields: components["schemas"]["RecipeRunPayload"];
            /** Node Id */
            node_id?: null;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /**
         * RecipeRunDispositionValue
         * @description The header verdict for a run the Controller never owned.
         * @enum {string}
         */
        RecipeRunDispositionValue: "unowned";
        /** RecipeRunPayload */
        RecipeRunPayload: {
            /** Alias */
            alias: string;
            /** Entity Id */
            entity_id: string;
            /**
             * Entity Kind
             * @constant
             */
            entity_kind: "recipe-run";
            /** Installation Id */
            installation_id: string;
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Route State */
            route_state: string;
            /** State */
            state: string;
        };
        /** RecipeRuntime */
        RecipeRuntime: {
            /** Arguments */
            arguments: components["schemas"]["RecipeRuntimeArgument"][];
            /** Engine */
            engine: string;
            /** Entrypoint */
            entrypoint: string[];
            /** Environment */
            environment: components["schemas"]["RecipeRuntimeEnvironment"][];
            lifecycle: components["schemas"]["RecipeLifecycle"];
        };
        /** RecipeRuntimeArgument */
        RecipeRuntimeArgument: {
            /** Name */
            name: string;
            /** Setting */
            setting?: string | null;
            /** @description A literal process value; null is reserved for the setting-bound placeholder. */
            value?: components["schemas"]["RuntimeArgumentValue"] | null;
        };
        /** RecipeRuntimeEnvironment */
        RecipeRuntimeEnvironment: {
            /** Name */
            name: string;
            /** Value */
            value: string | (number | ExactNumber) | boolean | (number | ExactNumber);
        };
        /** RecipeSelectorIntent */
        RecipeSelectorIntent: {
            /**
             * Force
             * @default false
             */
            force?: boolean;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "selector";
            /** Selector */
            selector: string;
        };
        /** RecipeServingValidation */
        RecipeServingValidation: {
            /** Checks */
            checks: components["schemas"]["RecipeValidationCheck"][];
            /**
             * Interface
             * @enum {string}
             */
            interface: "openai" | "image-job" | "audio-job" | "video-job" | "mesh-job" | "artifact-job";
        };
        /** RecipeSetting */
        RecipeSetting: {
            /**
             * Change Effect
             * @enum {string}
             */
            change_effect: "none" | "restart" | "reprepare" | "rebuild";
            /** Value */
            value: string | (number | ExactNumber) | boolean | (number | ExactNumber);
        };
        /** RecipeStartParent */
        RecipeStartParent: {
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["StartPhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /** @default null */
            recovery?: components["schemas"]["DistributedRecoveryMarker"] | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Start Anchored At
             * @default null
             */
            start_anchored_at?: string | null;
            /**
             * Start Deadline
             * @default null
             */
            start_deadline?: string | null;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * RecipeStartPayload
         * @description Start one rank; placement, image and addresses come from the plan.
         */
        RecipeStartPayload: {
            compiled_execution_plan: components["schemas"]["CompiledExecutionPlan"];
            /** Installation Id */
            installation_id: string;
            /** Mapping Id */
            mapping_id: string;
            /**
             * Phase
             * @default null
             */
            phase?: ("rank-launch" | "collective-readiness") | null;
            /** Plan Digest */
            plan_digest: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Run Generation */
            run_generation: number | ExactNumber;
            /** Run Id */
            run_id: string;
            /**
             * Start Deadline
             * Format: date-time
             * @default null
             */
            start_deadline?: string | null;
        };
        /**
         * RecipeStartResult
         * @description The serving rank reports its endpoint; every other rank reports ``{}``.
         */
        RecipeStartResult: {
            /** Endpoint */
            endpoint?: string | null;
            preload_diagnostics?: components["schemas"]["FailureDiagnostics"] | null;
        };
        /** RecipeStopParent */
        RecipeStopParent: {
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** @default null */
            job_run_stop_authorization?: components["schemas"]["JobRunStopScope"] | null;
            /** @default null */
            offline_stop_intent?: components["schemas"]["OfflineStopIntent"] | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["StopPhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /** @default null */
            profile_partial_stop?: components["schemas"]["ProfilePartialStop"] | null;
            /** @default null */
            recovery?: components["schemas"]["DistributedRecoveryMarker"] | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** @default null */
            service_stop_review?: components["schemas"]["ServiceRunStopReview"] | null;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * RecipeStopPayload
         * @description Exact cleanup authority, independent of historical launch-plan readability.
         *
         *     The Controller binds these identities and timeout into the signed helper
         *     grant; the helper reconciles only the matching runtime generation.
         */
        RecipeStopPayload: {
            /**
             * Cancel Pending Start
             * @default false
             */
            cancel_pending_start?: boolean;
            /** Installation Id */
            installation_id: string;
            /** Mapping Id */
            mapping_id: string;
            /** Plan Digest */
            plan_digest: string;
            /** Rank */
            rank: number | ExactNumber;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Role */
            role: string;
            /** Run Generation */
            run_generation: number | ExactNumber;
            /** Run Id */
            run_id: string;
            /** Stop Timeout Seconds */
            stop_timeout_seconds: number;
            /** Target Runtime Id */
            target_runtime_id: string;
        };
        /**
         * RecipeStopResult
         * @description A stop succeeds with an empty result.
         */
        RecipeStopResult: Record<string, never>;
        /**
         * RecipeTopology
         * @description Roles and their start order; everything else follows from node_count.
         *
         *     One node runs alone. More nodes share one connected fabric: losing a rank
         *     withdraws the endpoint, recovery restarts the workers and then the
         *     entrypoint, and stopping always starts with the endpoint owner.
         */
        RecipeTopology: {
            /** Name */
            name: string;
            /** Node Count */
            node_count: number | ExactNumber;
            parallelism: components["schemas"]["RecipeParallelism"];
            /** Roles */
            roles: components["schemas"]["RecipeTopologyRole"][];
            /** Start Order */
            start_order: string[];
        };
        /** RecipeTopologyRole */
        RecipeTopologyRole: {
            /** Count */
            count: number | ExactNumber;
            /** Endpoint Owner */
            endpoint_owner: boolean;
            /** Name */
            name: string;
            resources: components["schemas"]["RecipeRoleResources"];
        };
        /** RecipeUninstallParent */
        RecipeUninstallParent: {
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Owner Id */
            owner_id: string;
            /**
             * Owner Kind
             * @enum {string}
             */
            owner_kind: "installation" | "run" | "recipe-build" | "artifact-job";
            /**
             * Phases
             * @default null
             */
            phases?: components["schemas"]["UninstallPhaseOperation"][][] | null;
            /** Plan Digest */
            plan_digest: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /** RecipeUninstallPayload */
        RecipeUninstallPayload: {
            /** Cleanup Model Content Sha256 */
            cleanup_model_content_sha256: string | null;
            /** Installation Id */
            installation_id: string;
            /** Plan Digest */
            plan_digest: string;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
        };
        /**
         * RecipeUninstallResult
         * @description An uninstall succeeds with an empty result.
         */
        RecipeUninstallResult: Record<string, never>;
        /**
         * RecipeUpdateChild
         * @description Frozen identity plus a rebuildable observation; child jobs own execution.
         */
        RecipeUpdateChild: {
            /** Effective Execution Key */
            effective_execution_key: string;
            failure?: components["schemas"]["RecipeUpdateFailure"] | null;
            /** Observed At */
            observed_at?: string | null;
            /** Operation Id */
            operation_id?: string | null;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /** Recipe Name */
            recipe_name: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Request Key */
            request_key: string;
            /** Retry At */
            retry_at?: string | null;
            /**
             * State
             * @default pending
             * @enum {string}
             */
            state?: "pending" | "queued" | "running" | "backoff" | "observing" | "succeeded" | "failed" | "cancelled";
        };
        /**
         * RecipeUpdateCode
         * @description Recipe update batch problems.
         * @enum {string}
         */
        RecipeUpdateCode: "recipe_update.claim_lost" | "recipe_update.observation_invalid" | "recipe_update.operation_invalid" | "recipe_update.request_key_reused" | "recipe_update.scope_invalid" | "recipe-update.cancel-effect-unknown";
        /** RecipeUpdateDocument */
        RecipeUpdateDocument: {
            /** @default null */
            cancellation?: components["schemas"]["RecipeOperationCancellationResult"] | null;
            /** Children */
            children: components["schemas"]["RecipeUpdateChild"][];
            /**
             * Claim Owner
             * @default null
             */
            claim_owner?: string | null;
            /**
             * Claim Until
             * @default null
             */
            claim_until?: string | null;
            /**
             * Kind
             * @default recipe.cache.update.v2
             * @constant
             */
            kind?: "recipe.cache.update.v2";
            /**
             * Next Attempt At
             * @default null
             */
            next_attempt_at?: string | null;
            /**
             * Next Child
             * @default 0
             */
            next_child?: number | ExactNumber;
            request: components["schemas"]["RecipeUpdateScope"];
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
        };
        /**
         * RecipeUpdateFailure
         * @description Bounded admission failure or observation of the referenced child's failure.
         */
        RecipeUpdateFailure: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Retryable */
            retryable: boolean;
        };
        /**
         * RecipeUpdateNotice
         * @description A running workload uses an older revision than the newest active one.
         */
        RecipeUpdateNotice: {
            /**
             * Code
             * @default recipe.update_available
             */
            code?: string;
            /** Detail */
            detail: string;
            /** Newest Released At */
            newest_released_at?: string | null;
            /** Newest Revision Id */
            newest_revision_id: string;
            /** Newest Version */
            newest_version?: string | null;
            /** Running Released At */
            running_released_at?: string | null;
            /** Running Revision Id */
            running_revision_id: string;
            /** Running Version */
            running_version?: string | null;
            /**
             * Severity
             * @default info
             */
            severity?: string;
        };
        /** RecipeUpdateRequest */
        RecipeUpdateRequest: {
            /**
             * All
             * @default false
             */
            all?: boolean;
            /** Request Key */
            request_key: string;
            /** Selectors */
            selectors?: string[];
        };
        /** RecipeUpdateResponse */
        RecipeUpdateResponse: {
            /**
             * Action
             * @default update
             * @constant
             */
            action?: "update";
            /** Attempt */
            attempt: number;
            cancellation?: components["schemas"]["RecipeOperationCancellationResult"] | null;
            /** Children */
            children: components["schemas"]["RecipeUpdateChild"][];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Id */
            id: string;
            /**
             * Kind
             * @default recipe.cache.update.v2
             * @constant
             */
            kind?: "recipe.cache.update.v2";
            /** Next Attempt At */
            next_attempt_at?: string | null;
            /**
             * Partial
             * @default false
             */
            partial?: boolean;
            progress: components["schemas"]["OperationProgress"];
            request: components["schemas"]["RecipeUpdateScope"];
            /** Request Id */
            request_id: string;
            /** Resume Condition */
            resume_condition?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "backoff" | "observing" | "succeeded" | "failed" | "cancelled";
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Wait Owner */
            wait_owner?: "recipe-image-availability" | null;
            /** Waiting On */
            waiting_on?: string | null;
        };
        /** RecipeUpdateScope */
        RecipeUpdateScope: {
            /**
             * All
             * @default false
             */
            all?: boolean;
            /** Selectors */
            selectors?: string[];
        };
        /** RecipeValidation */
        RecipeValidation: {
            serving: components["schemas"]["RecipeServingValidation"];
        };
        /** RecipeValidationCheck */
        RecipeValidationCheck: {
            /** Assertions */
            assertions: ("endpoint.healthy" | "chat.nonempty" | "chat.output-cap" | "tools.called" | "completion.nonempty" | "completion.output-cap" | "embedding.nonempty" | "inference.completed" | "artifact.output")[];
            /**
             * Kind
             * @enum {string}
             */
            kind: "openai.health" | "openai.chat" | "openai.vision" | "openai.tools" | "openai.completion" | "openai.embedding" | "image-job.output" | "audio-job.output" | "video-job.output" | "mesh-job.output" | "artifact-job.output";
            /** Name */
            name: string;
            /** Request */
            request: components["schemas"]["RecipeHttpServingRequest"] | components["schemas"]["RecipeJobServingRequest"];
        };
        /**
         * ReconcileCode
         * @description Why an installation reconcile is blocked.
         * @enum {string}
         */
        ReconcileCode: "reconcile.active_effect_unknown" | "reconcile.agent_unavailable" | "reconcile.capacity_busy" | "reconcile.install_provenance_mismatch" | "reconcile.install_provenance_unavailable" | "reconcile.installation_effect_unknown" | "reconcile.installation_identity_mismatch" | "reconcile.installation_identity_unavailable" | "reconcile.membership_changed" | "reconcile.operation_active" | "reconcile.rank_membership_changed" | "reconcile.recipe_revision_unavailable" | "reconcile.spec_identity_mismatch";
        /** ReconcilePhaseOperation */
        ReconcilePhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeReconcilePayload"];
        };
        /** RecoveryStartItem */
        RecoveryStartItem: {
            /** Node Id */
            node_id: string;
            payload: components["schemas"]["RecipeStartPayload"];
        };
        /**
         * RemovedRecipeNodeResult
         * @description Result emitted by the Controller's logical uninstall projection.
         */
        RemovedRecipeNodeResult: {
            /**
             * Removed
             * @constant
             */
            removed: true;
        };
        /**
         * RequestValidationIssue
         * @description A structural input error without the submitted input or validator context.
         */
        RequestValidationIssue: {
            /** Loc */
            loc: (string | (number | ExactNumber))[];
            /** Msg */
            msg: string;
            /** Type */
            type: string;
        };
        /** RequestValidationProblem */
        RequestValidationProblem: {
            /** Candidates */
            candidates?: string[] | null;
            context?: components["schemas"]["ErrorContextResponse"] | null;
            /** Detail */
            detail: string;
            /** Issues */
            issues: components["schemas"]["RequestValidationIssue"][];
        };
        /**
         * ReservationState
         * @description The standing of a resource reservation.
         * @enum {string}
         */
        ReservationState: "active" | "promised" | "released" | "expired";
        /**
         * Residue
         * @description A typed ``unknown``: what was damaged, why, and what the caller does next.
         *
         *     It is a value, not an exception: the caller retires the damaged row or skips
         *     the element and carries on.
         */
        Residue: {
            /** Kind */
            kind: string;
            /**
             * Note
             * @default
             */
            note?: string;
            reason: components["schemas"]["BookkeepingReason"];
            /** Subject */
            subject: string;
        };
        /**
         * ResourceBlockerCode
         * @description The capacity-fit codes the resource planner gives a node that cannot fit.
         *
         *     ``insufficient`` is the family prefix a run admission maps onto
         *     ``run.insufficient_memory``; the planner itself names the exact
         *     ``insufficient_capacity*`` member.
         * @enum {string}
         */
        ResourceBlockerCode: "resource.capacity_unknown" | "resource.insufficient" | "resource.insufficient_capacity" | "resource.insufficient_capacity_after_stop" | "resource.insufficient_reservation_budget" | "resource.resident_usage_unknown";
        /**
         * ResourceDemandEvidence
         * @description The evidence terms used for one selected rank's memory fit.
         */
        ResourceDemandEvidence: {
            /** Batch Bytes */
            batch_bytes?: (number | ExactNumber) | null;
            /** Concurrency Bytes */
            concurrency_bytes?: (number | ExactNumber) | null;
            /** Context Bytes */
            context_bytes?: (number | ExactNumber) | null;
            /** Evidence Digest */
            evidence_digest?: string | null;
            /**
             * Evidence State
             * @enum {string}
             */
            evidence_state: "declared" | "measured" | "fresh" | "stale" | "unknown";
            /** Runtime Overhead Bytes */
            runtime_overhead_bytes?: (number | ExactNumber) | null;
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
            /** Weights Bytes */
            weights_bytes?: (number | ExactNumber) | null;
        };
        /**
         * ResourcePlanningCode
         * @description Resource planning (memory, disk, parallelism) refusals and unknowns.
         * @enum {string}
         */
        ResourcePlanningCode: "resource.envelope_exceeds_capacity" | "resource.envelope_unverified" | "resource.estimate_uncertain" | "resource.evidence_invalid" | "resource.evidence_unknown" | "resource.knobs_invalid" | "resource.parallelism_duplicate" | "resource.parallelism_inconsistent" | "resource.parallelism_type" | "resource.parallelism_unknown" | "resource.settings_kind_unknown" | "resource.settings_type" | "resource.settings_unknown" | "resource.stop_release_unknown" | "resource.context_unknown" | "resource.context_evidence_invalid" | "resource.context_unsupported" | "resource.context_evidence_unknown" | "resource.concurrency_unknown" | "resource.concurrency_evidence_invalid" | "resource.concurrency_unsupported" | "resource.concurrency_evidence_unknown" | "resource.batch_unknown" | "resource.batch_evidence_invalid" | "resource.batch_unsupported" | "resource.batch_evidence_unknown";
        /**
         * ResourceTerm
         * @description The effective settings whose capacity cost the resource planner derives.
         * @enum {string}
         */
        ResourceTerm: "context" | "concurrency" | "batch";
        /**
         * ResourceTermProblem
         * @description What is wrong with the evidence for one resource term.
         * @enum {string}
         */
        ResourceTermProblem: "unknown" | "evidence_invalid" | "unsupported" | "evidence_unknown";
        /**
         * RolloutPreparation
         * @description Normalized preparation identity shared by profiles, Run, web and CLI.
         */
        RolloutPreparation: {
            /** Controller Ready */
            controller_ready: boolean;
            /** Exceptions */
            exceptions?: components["schemas"]["CompatibilityPreparation"][];
            model: components["schemas"]["ModelArtifactPreparation"];
            /** Ready */
            ready: boolean;
            /** Reasons */
            reasons?: components["schemas"]["PreparationReason"][];
            runtime_image: components["schemas"]["RuntimeImagePreparation"];
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Target Node Ids */
            target_node_ids: string[];
            /** Targets Ready */
            targets_ready: boolean;
        };
        /**
         * RouteClaimMarker
         * @description The marker of the one route publication claim row.
         *
         *     ``route_publications.activation_marker`` holds an activation marker for a
         *     published or maintenance generation, and this ordinal for the claim row that
         *     orders concurrent publication attempts.
         */
        RouteClaimMarker: {
            /** Claim Ordinal */
            claim_ordinal: number | ExactNumber;
        };
        /**
         * RoutePublicationState
         * @description The phases of one atomic route publication.
         * @enum {string}
         */
        RoutePublicationState: "withdrawal-pending" | "routes-withdrawn" | "publication-pending" | "completed" | "failed";
        /**
         * RouteState
         * @description Whether a run's inference route is published to the gateway.
         * @enum {string}
         */
        RouteState: "withdrawn" | "pending" | "published" | "failed";
        /**
         * RunAdmissionCode
         * @description The typed codes a run admission names for a refusal, blocker or wait.
         *
         *     ``capacity_busy`` is lock contention only.  Every other reason an admission
         *     must wait or is refused carries its own member, so a waiting operation shows
         *     the real cause.  The retryable blockers (a plan that may become admissible
         *     by itself) are a subset the Controller derives from these members.
         * @enum {string}
         */
        RunAdmissionCode: "run.plan_invalid" | "run.plan_stale" | "run.dependencies_stale" | "run.capacity_busy" | "run.target_membership_changed" | "run.mapping_not_ready" | "run.inventory_missing" | "run.stale_inventory" | "run.insufficient_memory" | "run.port_occupied" | "run.rendezvous_port_occupied" | "run.unreconciled_lost_rank" | "run.not_installed" | "run.fabric_address_missing" | "run.fabric_address_duplicate";
        /**
         * RunDegradedReason
         * @description Why a run is shown degraded in the fleet projection.
         * @enum {string}
         */
        RunDegradedReason: "external-member" | "mapping-incomplete" | "missing-ranks" | "unexpected-ranks" | "rank-membership-mismatch" | "run-not-running" | "rank-not-running" | "rank-stale" | "route-not-published";
        /**
         * RunMemoryResidualRange
         * @description Possible remaining bytes for one exact active run reservation.
         */
        RunMemoryResidualRange: {
            /** Maximum Bytes */
            maximum_bytes: number | ExactNumber;
            /**
             * Minimum Bytes
             * @default 0
             * @constant
             */
            minimum_bytes?: number | ExactNumber;
            /**
             * Reservation Kind
             * @enum {string}
             */
            reservation_kind: "host-memory" | "gpu-memory" | "unified-memory";
            /** Run Generation */
            run_generation: number | ExactNumber;
            /** Run Id */
            run_id: string;
        };
        /** RunNodeChange */
        RunNodeChange: {
            /** Entity Id */
            entity_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            entity_kind: "run-node";
            fields: components["schemas"]["RunNodePayload"];
            /** Node Id */
            node_id: string;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /** RunNodePayload */
        RunNodePayload: {
            /** Entity Id */
            entity_id: string;
            /**
             * Entity Kind
             * @constant
             */
            entity_kind: "run-node";
            failure_diagnostics?: components["schemas"]["FailureDiagnostics"] | null;
            /** Node Id */
            node_id: string;
            /** Observed Memory Bytes */
            observed_memory_bytes?: (number | ExactNumber) | null;
            /** Rank */
            rank: number;
            /** Reserved Memory Bytes */
            reserved_memory_bytes: number | ExactNumber;
            /** Role */
            role: string;
            /** Run Id */
            run_id: string;
            /** State */
            state: string;
        };
        /** RunPresence */
        RunPresence: {
            /** Alias */
            alias: string;
            degraded_reason?: components["schemas"]["RunDegradedReason"] | null;
            /** Expected Rank Count */
            expected_rank_count: number;
            /**
             * Group State
             * @enum {string}
             */
            group_state: "healthy" | "degraded" | "unavailable";
            /** Healthy */
            healthy: boolean | null;
            /** Installation Id */
            installation_id: string;
            /** Member Node Ids */
            member_node_ids: string[];
            /** Option Choices */
            option_choices?: {
                [key: string]: string;
            };
            /** Present Ranks */
            present_ranks: number[];
            /** Projection Issue */
            projection_issue?: string | null;
            /** Rank */
            rank: number;
            /** Rank Age Seconds */
            rank_age_seconds: number | ExactNumber;
            /** Rank Fresh */
            rank_fresh: boolean;
            rank_state: components["schemas"]["RunState"];
            /** Recipe Id */
            recipe_id: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            recipe_update?: components["schemas"]["RecipeUpdateNotice"] | null;
            /** Role */
            role: string;
            /** Route Reason */
            route_reason?: string | null;
            route_state: components["schemas"]["RouteState"];
            /** Run Id */
            run_id: string;
            run_state: components["schemas"]["RunState"];
            /** Title */
            title: string;
        };
        /**
         * RunState
         * @description The condition of a recipe run (and of each of its ranks).
         * @enum {string}
         */
        RunState: "planned" | "starting" | "running" | "stopping" | "stopped" | "failed" | "lost";
        /** RunSwitchApplyRequest */
        RunSwitchApplyRequest: {
            /**
             * Action
             * @default run
             * @enum {string}
             */
            action?: "install" | "run" | "switch";
            /** Alias */
            alias: string;
            invocation?: components["schemas"]["InvocationMetadata"];
            /** Model Content Sha256 */
            model_content_sha256: string;
            /** Option Choices */
            option_choices?: {
                [key: string]: string;
            };
            /**
             * Plan Digest
             * @default null
             */
            plan_digest?: string | null;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /**
             * Request Key
             * @default null
             */
            request_key?: string | null;
            /**
             * Retention
             * @default retain-cached
             * @enum {string}
             */
            retention?: "retain-cached" | "reclaim-unreferenced";
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            spark_group: components["schemas"]["SparkGroup"];
        };
        /**
         * RunSwitchAssessment
         * @description Planner-owned admission and observations shared by operator reviews.
         */
        RunSwitchAssessment: {
            /** Alias */
            alias: string | null;
            /** Allowed */
            allowed: boolean;
            /** Blockers */
            blockers: components["schemas"]["RunSwitchReason"][];
            effective_settings?: components["schemas"]["EffectiveSettingsSelection"] | null;
            fit_after_stop: components["schemas"]["SparkFit"] | null;
            fit_current: components["schemas"]["SparkFit"];
            /** Freshness */
            freshness?: components["schemas"]["FreshnessEvidence"][];
            post_stop_memory_check?: components["schemas"]["ConditionalPostStopMemoryCheck"] | null;
            preparation?: components["schemas"]["RolloutPreparation"] | null;
            /**
             * Stop Before Prepare
             * @default false
             */
            stop_before_prepare?: boolean;
            /**
             * Stop Before Transfer
             * @default false
             */
            stop_before_transfer?: boolean;
            /** Stops */
            stops: components["schemas"]["StopImpact"][];
            /** Warnings */
            warnings: components["schemas"]["RunSwitchReason"][];
        };
        /** RunSwitchBuildEvidence */
        RunSwitchBuildEvidence: {
            /** Build Id */
            build_id: string | null;
            /** Build Input Sha256 */
            build_input_sha256?: string | null;
            /** Builder Node Id */
            builder_node_id?: string | null;
            compatibility: components["schemas"]["BuildCompatibilityEvidence"];
            /** Detail */
            detail?: string | null;
            /** Image Bytes */
            image_bytes?: (number | ExactNumber) | null;
            /** Image Digest */
            image_digest: string | null;
            /** Oci Layout Sha256 */
            oci_layout_sha256?: string | null;
            runtime: components["schemas"]["RuntimeImageStorageImpact"];
            source: components["schemas"]["BuildSourceEvidence"];
            /**
             * State
             * @enum {string}
             */
            state: "available" | "planned" | "building" | "failed" | "missing" | "incompatible" | "unknown";
        };
        /** RunSwitchCachedTransferResult */
        RunSwitchCachedTransferResult: {
            /** Cached Nodes */
            cached_nodes: string[];
            /** Cached Target Totals */
            cached_target_totals: {
                [key: string]: number | ExactNumber;
            };
            /**
             * Phase
             * @constant
             */
            phase: "transfer";
            /**
             * Skipped
             * @constant
             */
            skipped: true;
            /**
             * Subphase
             * @constant
             */
            subphase: "target-copy";
            /**
             * Verified
             * @constant
             */
            verified: false;
            /** Verified Build Id */
            verified_build_id: string | null;
            /** Verified Digests */
            verified_digests: string[];
            /** Verified Image Digest */
            verified_image_digest: string;
            /** Verified Oci Layout Sha256 */
            verified_oci_layout_sha256: string;
        };
        /** RunSwitchCancellation */
        RunSwitchCancellation: {
            /** Actor */
            actor: string;
            /** Reason */
            reason: string;
            /** Request Key */
            request_key: string;
            /**
             * Requested At
             * Format: date-time
             */
            requested_at: string;
        };
        /**
         * RunSwitchChildProgress
         * @description Progress nested in a durable child receipt.
         */
        RunSwitchChildProgress: {
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Members */
            members?: components["schemas"]["RunSwitchMemberReceipt"][];
            operation?: components["schemas"]["OperationProgress"] | null;
            /** Phase */
            phase?: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "final_verify" | "container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
            /**
             * Total Bytes Known
             * @default false
             */
            total_bytes_known?: boolean;
        };
        /** RunSwitchCleanupIntent */
        RunSwitchCleanupIntent: {
            /**
             * Cleanup Mode
             * @default uninstall
             * @enum {string}
             */
            cleanup_mode?: "uninstall" | "reconcile";
            /** Installation Id */
            installation_id: string;
            /**
             * Plan Digest
             * @default null
             */
            plan_digest?: string | null;
            /**
             * Request Key
             * @default null
             */
            request_key?: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "cleanup";
        };
        /** RunSwitchCleanupResult */
        RunSwitchCleanupResult: {
            /** Nas Evicted */
            nas_evicted: boolean;
            /**
             * Phase
             * @constant
             */
            phase: "cleanup";
            /** Protected Digests */
            protected_digests?: string[];
            /**
             * Protected Referenced Bytes
             * @default 0
             */
            protected_referenced_bytes?: number | ExactNumber;
            /** Reclaimed Bytes */
            reclaimed_bytes: number | ExactNumber;
            /** Reclaimed Digests */
            reclaimed_digests?: string[];
            /**
             * Scope
             * @constant
             */
            scope: "spark-local";
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
        };
        /**
         * RunSwitchCleanupVerifyResult
         * @description Observed removal of the installation, derived from durable state.
         */
        RunSwitchCleanupVerifyResult: {
            /**
             * Active Runs
             * @default 0
             */
            active_runs?: number | ExactNumber;
            /**
             * Cleanup Mode
             * @default uninstall
             * @enum {string}
             */
            cleanup_mode?: "uninstall" | "reconcile";
            /** Final Verified */
            final_verified: boolean;
            /** Installation Id */
            installation_id: string;
            /** Installation State */
            installation_state?: string | null;
            /**
             * Phase
             * @constant
             */
            phase: "final_verify";
            /** Reconciliation Request Id */
            reconciliation_request_id?: string | null;
            /** Removed */
            removed: boolean;
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
        };
        /**
         * RunSwitchCode
         * @description Run/Switch phase blockers, waits and failure codes.
         * @enum {string}
         */
        RunSwitchCode: "run-switch.active-run-conflict" | "run-switch.advance-failed" | "run-switch.agent-upgrade-required" | "run-switch.artifact-identity-unknown" | "run-switch.artifact-inspection-unavailable" | "run-switch.artifact-manifest-unknown" | "run-switch.artifact-phase-executor-unavailable" | "run-switch.artifact-verification-result-invalid" | "run-switch.cleanup-reclaim-evidence-invalid" | "run-switch.cleanup-reclaimed-bytes-exceed-plan" | "run-switch.cleanup-reference-protection-evidence-invalid" | "run-switch.cleanup-reference-protection-overlap" | "run-switch.cleanup-scope-invalid" | "run-switch.container-build-evidence-invalid" | "run-switch.container-build-executor-unavailable" | "run-switch.container-build-identity-unavailable" | "run-switch.container-build-parent-changed" | "run-switch.container-build-parent-invalid" | "run-switch.container-build-plan-invalid" | "run-switch.container-build-receipt-unavailable" | "run-switch.container-build-required" | "run-switch.container-build-state-invalid" | "run-switch.container-build-unavailable" | "run-switch.cross-group_conflict" | "run-switch.disk-envelope-invalid" | "run-switch.disk-eviction-planned" | "run-switch.effect-uncertain" | "run-switch.final-verification" | "run-switch.final-verification-clock-invalid" | "run-switch.final-verification-failed" | "run-switch.final-verification-timeout" | "run-switch.final-verification-unavailable" | "run-switch.install-executor-unavailable" | "run-switch.install-preparation-unavailable" | "run-switch.installation-handoff-unavailable" | "run-switch.installation-identity-changed" | "run-switch.installation-identity-unavailable" | "run-switch.installation-membership-changed" | "run-switch.installation-preparation-unavailable" | "run-switch.installation-verification-failed" | "run-switch.installation-verification-unavailable" | "run-switch.insufficient-disk" | "run-switch.insufficient-memory" | "run-switch.interface-invalid" | "run-switch.inventory-stale" | "run-switch.inventory-unknown" | "run-switch.mapping_group_mismatch" | "run-switch.mapping_invalid" | "run-switch.mapping_materialization_unavailable" | "run-switch.memory-envelope-invalid" | "run-switch.model-download-artifact-set-mismatch" | "run-switch.model-download-byte-evidence-mismatch" | "run-switch.model-download-coverage-incomplete" | "run-switch.model_recipe_mismatch" | "run-switch.model_revision_unavailable" | "run-switch.nas-coverage-unknown" | "run-switch.nas-download-blocked" | "run-switch.nas-download-required" | "run-switch.option_invalid" | "run-switch.phase-retry" | "run-switch.plan-refresh-unavailable" | "run-switch.plan-targets-changed" | "run-switch.post-stop-inventory-pending" | "run-switch.post-stop-memory-pool-changed" | "run-switch.preflight-recipe-changed" | "run-switch.prepare-subphase-unsupported" | "run-switch.profile.incomplete_multi_spark_model" | "run-switch.profile_stop_scope_changed" | "run-switch.receipt_invalid" | "run-switch.recipe-build-compatibility-unknown" | "run-switch.recipe-build-incompatible" | "run-switch.recipe-build-unavailable" | "run-switch.recipe_dependencies_unavailable" | "run-switch.recipe_digest_changed" | "run-switch.recipe_unresolved" | "run-switch.reconciliation-assessment-unavailable" | "run-switch.reconciliation-authority-unavailable" | "run-switch.reconciliation-prerequisite" | "run-switch.reconciliation-receipts-retained" | "run-switch.reconciliation-state-verification-failed" | "run-switch.reconciliation-verification-failed" | "run-switch.request_key_reused_differently" | "run-switch.resource-contract-invalid" | "run-switch.resource.insufficient" | "run-switch.resource.insufficient_capacity" | "run-switch.resource.insufficient_capacity_after_stop" | "run-switch.resource.insufficient_reservation_budget" | "run-switch.resource.resident_usage_unknown" | "run-switch.run-not-active" | "run-switch.run_admission_blocked" | "run-switch.run_admission_unavailable" | "run-switch.runtime-build-verification-mismatch" | "run-switch.runtime-image-authorization-mismatch" | "run-switch.runtime-image-executor-unavailable" | "run-switch.runtime-image-owner-changed" | "run-switch.runtime-image-preparation-layout-mismatch" | "run-switch.runtime-image-preparation-receipt-invalid" | "run-switch.runtime-image-preparing" | "run-switch.runtime-image-reference-identity-mismatch" | "run-switch.runtime-image-waiting-without-child" | "run-switch.spark-unavailable" | "run-switch.start-observation" | "run-switch.start-observation-expired" | "run-switch.start_installation_unavailable" | "run-switch.stop-plan-unavailable" | "run-switch.stop-still-unresolved-after-cancellation" | "run-switch.stop-target-disappeared" | "run-switch.stopped-run-identity-changed" | "run-switch.stopped-run-membership-changed" | "run-switch.target-not-active" | "run-switch.transfer-byte-evidence-invalid" | "run-switch.uninstall-assessment-unavailable" | "run-switch.uninstall-blocked" | "run-switch.uninstall-issued-prerequisite" | "run-switch.uninstall_target_unavailable" | "run-switch.waiting" | "run-switch.reason-unclassified" | "run-switch.cancel-effect-unknown" | "run-switch.container-build-start-unavailable" | "run-switch.distributed-recovery-active" | "run-switch.final-owner-state-unknown" | "run-switch.final-verification-expired" | "run-switch.install-plan-unavailable" | "run-switch.install-preflight-expired" | "run-switch.install-preparation-failed" | "run-switch.install-start-failed" | "run-switch.installation-handoff-inconsistent" | "run-switch.plan_blocked" | "run-switch.reconciliation-start-failed" | "run-switch.route-health-recovery-active" | "run-switch.route-owner-failed" | "run-switch.route-publication-pending" | "run-switch.route-withdrawn-owner-unknown" | "run-switch.run-owner-active" | "run-switch.run-owner-terminal" | "run-switch.stale_plan" | "run-switch.stop-verification-pending" | "run-switch.superseded" | "run-switch.uninstall-abandon-failed" | "run-switch.uninstall-start-failed" | "run-switch.recipe.stop-issued-pending" | "run-switch.recipe.install-issued-pending" | "run-switch.recipe.uninstall-issued-pending" | "run-switch.recipe.reconcile-issued-pending" | "run-switch.artifact-job-cancellation-issued-pending" | "run-switch.transfer-executor-unavailable" | "run-switch.transfer-waiting-without-child" | "run-switch.transfer-returned-no-evidence" | "run-switch.verify-executor-unavailable" | "run-switch.verify-waiting-without-child" | "run-switch.verify-returned-no-evidence" | "run-switch.cleanup-executor-unavailable" | "run-switch.cleanup-waiting-without-child" | "run-switch.cleanup-returned-no-evidence" | "run-switch.resource.capacity_unknown" | "run-switch.resource.envelope_exceeds_capacity" | "run-switch.resource.envelope_unverified" | "run-switch.resource.estimate_uncertain" | "run-switch.resource.evidence_invalid" | "run-switch.resource.evidence_unknown" | "run-switch.resource.knobs_invalid" | "run-switch.resource.parallelism_duplicate" | "run-switch.resource.parallelism_inconsistent" | "run-switch.resource.parallelism_type" | "run-switch.resource.parallelism_unknown" | "run-switch.resource.settings_kind_unknown" | "run-switch.resource.settings_type" | "run-switch.resource.settings_unknown" | "run-switch.resource.stop_release_unknown" | "run-switch.reconcile.active_effect_unknown" | "run-switch.reconcile.agent_unavailable" | "run-switch.reconcile.capacity_busy" | "run-switch.reconcile.install_provenance_mismatch" | "run-switch.reconcile.install_provenance_unavailable" | "run-switch.reconcile.installation_effect_unknown" | "run-switch.reconcile.installation_identity_mismatch" | "run-switch.reconcile.installation_identity_unavailable" | "run-switch.reconcile.membership_changed" | "run-switch.reconcile.operation_active" | "run-switch.reconcile.rank_membership_changed" | "run-switch.reconcile.recipe_revision_unavailable" | "run-switch.reconcile.spec_identity_mismatch" | "run-switch.stop.capacity_release_deferred" | "run-switch.stop.rank_membership_changed" | "run-switch.stop.reservation_membership_changed" | "run-switch.stop.run_not_stoppable" | "run-switch.stop.target_scope_changed" | "run-switch.uninstall.abandon-never-installed" | "run-switch.uninstall.active_run" | "run-switch.uninstall.active_runs_truncated" | "run-switch.uninstall.bytes_unknown" | "run-switch.uninstall.installation_not_uninstallable" | "run-switch.uninstall.operation_active" | "run-switch.uninstall.rank_membership_changed";
        /** RunSwitchContainerBuildResult */
        RunSwitchContainerBuildResult: {
            /** Build Id */
            build_id: string;
            /** Build Input Sha256 */
            build_input_sha256: string;
            /** Image Bytes */
            image_bytes?: (number | ExactNumber) | null;
            /** Image Digest */
            image_digest?: string | null;
            /** Oci Layout Sha256 */
            oci_layout_sha256?: string | null;
            /**
             * Phase
             * @constant
             */
            phase: "prepare";
            /**
             * State
             * @enum {string}
             */
            state: "planned" | "building" | "succeeded" | "failed";
            /**
             * Subphase
             * @constant
             */
            subphase: "container-build";
        };
        /**
         * RunSwitchDistributionChildResult
         * @description Durable projection of one target-copy child operation.
         *
         *     A child Job has a different persisted shape from a parent phase receipt:
         *     it owns member progress and per-node handoff evidence.  Keeping that
         *     projection separate prevents a progress snapshot from being accepted as
         *     a completed phase result.
         */
        RunSwitchDistributionChildResult: {
            /**
             * Error Code
             * @default null
             */
            error_code?: string | null;
            /** Evidence */
            evidence: components["schemas"]["ArtifactVerificationEvidence"][];
            /**
             * Failure Kind
             * @default null
             */
            failure_kind?: string | null;
            /** Members */
            members: components["schemas"]["RunSwitchMemberReceipt"][];
            /**
             * Phase
             * @constant
             */
            phase: "transfer";
            progress: components["schemas"]["RunSwitchChildProgress"];
            /**
             * Reason
             * @default null
             */
            reason?: string | null;
            /**
             * Subphase
             * @constant
             */
            subphase: "target-copy";
            /**
             * Uncertain
             * @default false
             */
            uncertain?: boolean;
        };
        /**
         * RunSwitchDistributionEndedResult
         * @description A missing durable distribution child; no node effect is asserted.
         */
        RunSwitchDistributionEndedResult: {
            error_code: components["schemas"]["DistributionCode"];
            /**
             * Phase
             * @constant
             */
            phase: "transfer";
            reason: components["schemas"]["WaitReason"];
            /**
             * Subphase
             * @constant
             */
            subphase: "target-copy";
            /**
             * Uncertain
             * @default true
             */
            uncertain?: boolean;
        };
        /** RunSwitchFinalVerifyResult */
        RunSwitchFinalVerifyResult: {
            /** Final Verified */
            final_verified: boolean;
            /** Healthy */
            healthy: boolean;
            /**
             * Phase
             * @constant
             */
            phase: "final_verify";
            /** Ranks */
            ranks: components["schemas"]["RunSwitchRankReceipt"][];
            /** Route State */
            route_state: string;
            /** Run Id */
            run_id: string;
            /** State */
            state: string;
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
        };
        /**
         * RunSwitchInstallationVerifyResult
         * @description Exact installed membership observed without a serving workload.
         */
        RunSwitchInstallationVerifyResult: {
            /** Active Runs */
            active_runs: number | ExactNumber;
            /** Final Verified */
            final_verified: boolean;
            /** Installation Id */
            installation_id: string;
            /** Installation State */
            installation_state: string;
            /**
             * Phase
             * @constant
             */
            phase: "final_verify";
            /** Ranks */
            ranks: components["schemas"]["RunSwitchRankReceipt"][];
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
            /** Unwithdrawn Routes */
            unwithdrawn_routes: number | ExactNumber;
        };
        /**
         * RunSwitchJobPayload
         * @description The reviewed plan, the request that asked for it and the live progress.
         */
        RunSwitchJobPayload: {
            /**
             * Action
             * @enum {string}
             */
            action: "install" | "run" | "switch" | "stop" | "cleanup";
            /**
             * Intent
             * @default null
             */
            intent?: (components["schemas"]["RunSwitchRunIntent"] | components["schemas"]["RunSwitchStopIntent"] | components["schemas"]["RunSwitchCleanupIntent"] | components["schemas"]["RunSwitchProfileStopIntent"]) | null;
            /**
             * Operation Kind
             * @enum {string}
             */
            operation_kind: "recipe.run-switch.v2" | "recipe.stop.v2" | "recipe.cleanup.v2";
            plan: components["schemas"]["RunSwitchPlan"];
            /** Plan Digest */
            plan_digest: string;
            progress: components["schemas"]["RunSwitchOperationResult"];
            /**
             * Retry Of
             * @default null
             */
            retry_of?: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Workload Intent Ordinal
             * @default null
             */
            workload_intent_ordinal?: number | null;
        };
        /**
         * RunSwitchJournalRepairEndEvidence
         * @description An ended observation retains the original journal and cancel request.
         */
        RunSwitchJournalRepairEndEvidence: {
            /** @default null */
            cancellation?: components["schemas"]["RunSwitchCancellation"] | null;
            /**
             * Code
             * @constant
             */
            code: "run-switch.journal-repair-exhausted";
            /** Operation Id */
            operation_id: string;
            /** Original Digest */
            original_digest: string;
            /** Original Document */
            original_document: string;
            /**
             * Recorded At
             * Format: date-time
             */
            recorded_at: string;
            /** Request Key */
            request_key: string;
        };
        /**
         * RunSwitchJournalRepairEvidence
         * @description Historical evidence only: no runnable state, clock, or alternate intent.
         */
        RunSwitchJournalRepairEvidence: {
            /**
             * Algorithm
             * @constant
             */
            algorithm: "zero-transfer-native-install-v1";
            /** Corrected Digest */
            corrected_digest: string;
            /** Native Samples */
            native_samples: components["schemas"]["NativeProgressWitness"][];
            /** Operation Id */
            operation_id: string;
            /** Original Digest */
            original_digest: string;
            /** Original Document */
            original_document: string;
            /** Payload Digest */
            payload_digest: string;
            /** Plan Digest */
            plan_digest: string;
            purpose: components["schemas"]["JournalRepairPurpose"];
            /**
             * Recorded At
             * Format: date-time
             */
            recorded_at: string;
            /** Request Key */
            request_key: string;
        };
        /**
         * RunSwitchJournalRepairPendingState
         * @description Durable observation and cancellation while Job.result remains untouched.
         */
        RunSwitchJournalRepairPendingState: {
            /**
             * Attempts
             * @default 0
             */
            attempts?: number | ExactNumber;
            /** @default null */
            cancellation?: components["schemas"]["RunSwitchCancellation"] | null;
            /**
             * Deadline At
             * Format: date-time
             */
            deadline_at: string;
            /**
             * Next Attempt At
             * Format: date-time
             */
            next_attempt_at: string;
        };
        /** RunSwitchMemberProgress */
        RunSwitchMemberProgress: {
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Error */
            error?: string | null;
            /** Node Id */
            node_id: string;
            /** Phase */
            phase?: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify") | null;
            /**
             * State
             * @enum {string}
             */
            state: "pending" | "running" | "succeeded" | "failed" | "unknown";
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
        };
        /**
         * RunSwitchMemberReceipt
         * @description Durable member projection emitted by a child distribution operation.
         */
        RunSwitchMemberReceipt: {
            /**
             * Cached
             * @default false
             */
            cached?: boolean;
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Diagnostic */
            diagnostic?: string | null;
            /** Error */
            error?: string | null;
            /** Error Code */
            error_code?: string | null;
            /** Failure Kind */
            failure_kind?: string | null;
            /** Node Id */
            node_id: string;
            /** Phase */
            phase?: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify") | null;
            /**
             * State
             * @enum {string}
             */
            state: "pending" | "running" | "succeeded" | "failed" | "unknown";
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
        };
        /** RunSwitchModelDownloadPendingResult */
        RunSwitchModelDownloadPendingResult: {
            /** Artifact Set Sha256 */
            artifact_set_sha256: string;
            /** Downloaded Bytes */
            downloaded_bytes: number | ExactNumber;
            /**
             * Phase
             * @constant
             */
            phase: "transfer";
            progress: components["schemas"]["RunSwitchChildProgress"];
            /** Reason */
            reason?: string | null;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /**
             * Subphase
             * @constant
             */
            subphase: "model-download";
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
        };
        /** RunSwitchModelDownloadResult */
        RunSwitchModelDownloadResult: {
            /** Artifact Set Sha256 */
            artifact_set_sha256: string;
            /**
             * Coverage
             * @constant
             */
            coverage: "complete";
            /** Downloaded Bytes */
            downloaded_bytes: number | ExactNumber;
            evidence?: components["schemas"]["ModelCacheDownloadResult"] | null;
            /**
             * Phase
             * @constant
             */
            phase: "transfer";
            progress: components["schemas"]["RunSwitchChildProgress"];
            /** Reason */
            reason?: string | null;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /**
             * Skipped
             * @default true
             * @constant
             */
            skipped?: true;
            /**
             * Subphase
             * @constant
             */
            subphase: "model-download";
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
        };
        /** RunSwitchOperation */
        RunSwitchOperation: {
            /**
             * Action
             * @enum {string}
             */
            action: "install" | "run" | "switch" | "stop" | "cleanup";
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            /** Cleanup Mode */
            cleanup_mode?: ("uninstall" | "reconcile") | null;
            /** Completed Phases */
            completed_phases: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify")[];
            /** Current Phase */
            current_phase?: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify") | null;
            /** Installation Id */
            installation_id?: string | null;
            /**
             * Kind
             * @enum {string}
             */
            kind: "recipe.run-switch.v2" | "recipe.stop.v2" | "recipe.cleanup.v2";
            /** Next Attempt At */
            next_attempt_at?: string | null;
            /** Node Ids */
            node_ids: string[];
            /** Operation Id */
            operation_id: string;
            /** Plan Digest */
            plan_digest: string;
            progress: components["schemas"]["RunSwitchProgress"];
            /** Request Key */
            request_key: string;
            result?: components["schemas"]["RunSwitchOperationResult"] | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "backoff" | "observing" | "needs-operator" | "succeeded" | "failed" | "cancelled" | "unknown";
            /** Status Reason */
            status_reason?: string | null;
        };
        /**
         * RunSwitchOperationResult
         * @description Exact durable result tree stored in ``Job.result``.
         */
        RunSwitchOperationResult: {
            /** Blockers */
            blockers?: components["schemas"]["OperationBlocker"][];
            cancellation?: components["schemas"]["RunSwitchCancellation"] | null;
            /** Child Operation Id */
            child_operation_id?: string | null;
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Completed Phases */
            completed_phases?: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify")[];
            /** Failed Phase */
            failed_phase?: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify") | null;
            /** Failure Code */
            failure_code?: string | null;
            /** Final Observation */
            final_observation?: components["schemas"]["RunSwitchDistributionEndedResult"] | components["schemas"]["RunSwitchContainerBuildResult"] | components["schemas"]["RunSwitchRuntimeImageResult"] | components["schemas"]["RunSwitchModelDownloadResult"] | components["schemas"]["RunSwitchModelDownloadPendingResult"] | components["schemas"]["RunSwitchTargetTransferResult"] | components["schemas"]["RunSwitchCachedTransferResult"] | components["schemas"]["RunSwitchTargetTransferEvidenceResult"] | components["schemas"]["RunSwitchVerifyResult"] | components["schemas"]["RunSwitchCleanupResult"] | components["schemas"]["RunSwitchRuntimePlanResult"] | components["schemas"]["RunSwitchPreparedResult"] | components["schemas"]["RunSwitchRuntimeInstallResult"] | components["schemas"]["RunSwitchStopResult"] | components["schemas"]["RunSwitchStartResult"] | components["schemas"]["RunSwitchUninstallResult"] | components["schemas"]["RunSwitchFinalVerifyResult"] | components["schemas"]["RunSwitchCleanupVerifyResult"] | components["schemas"]["RunSwitchInstallationVerifyResult"] | null;
            /** Final Verify Started At */
            final_verify_started_at?: (number | ExactNumber) | null;
            /**
             * Force Replan
             * @default false
             */
            force_replan?: boolean;
            /**
             * Item Index
             * @default 0
             */
            item_index?: number;
            /** Members */
            members?: components["schemas"]["RunSwitchMemberReceipt"][];
            /** Observation Deadline At */
            observation_deadline_at?: string | null;
            /** Observation Due At */
            observation_due_at?: string | null;
            operation?: components["schemas"]["OperationProgress"] | null;
            /** Operation Phase Index */
            operation_phase_index?: number | null;
            /** Phase */
            phase?: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify") | null;
            /**
             * Phase Index
             * @default 0
             */
            phase_index?: number;
            /** Phase Results */
            phase_results?: (components["schemas"]["RunSwitchDistributionEndedResult"] | components["schemas"]["RunSwitchContainerBuildResult"] | components["schemas"]["RunSwitchRuntimeImageResult"] | components["schemas"]["RunSwitchModelDownloadResult"] | components["schemas"]["RunSwitchModelDownloadPendingResult"] | components["schemas"]["RunSwitchTargetTransferResult"] | components["schemas"]["RunSwitchCachedTransferResult"] | components["schemas"]["RunSwitchTargetTransferEvidenceResult"] | components["schemas"]["RunSwitchVerifyResult"] | components["schemas"]["RunSwitchCleanupResult"] | components["schemas"]["RunSwitchRuntimePlanResult"] | components["schemas"]["RunSwitchPreparedResult"] | components["schemas"]["RunSwitchRuntimeInstallResult"] | components["schemas"]["RunSwitchStopResult"] | components["schemas"]["RunSwitchStartResult"] | components["schemas"]["RunSwitchUninstallResult"] | components["schemas"]["RunSwitchFinalVerifyResult"] | components["schemas"]["RunSwitchCleanupVerifyResult"] | components["schemas"]["RunSwitchInstallationVerifyResult"])[];
            /**
             * Phase Retry Generation
             * @default 0
             */
            phase_retry_generation?: number | ExactNumber;
            preflight?: components["schemas"]["LifecyclePreflightCheckpoint"] | null;
            /** Profile Application Id */
            profile_application_id?: string | null;
            /** Recovery Child Operation Id */
            recovery_child_operation_id?: string | null;
            /** Recovery Deadline At */
            recovery_deadline_at?: string | null;
            /** Retry Attempt */
            retry_attempt?: (number | ExactNumber) | null;
            /** Retry Reason */
            retry_reason?: string | null;
            /**
             * Retryable
             * @default false
             */
            retryable?: boolean;
            runtime_image_reference_intent?: components["schemas"]["RunSwitchRuntimeImageReferenceIntent"] | null;
            /** Start Deadline */
            start_deadline?: string | null;
            /** Startup Budget Seconds */
            startup_budget_seconds?: (number | ExactNumber) | null;
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
            /**
             * Total Bytes Known
             * @default false
             */
            total_bytes_known?: boolean;
            /** Workload Intent Ordinal */
            workload_intent_ordinal?: number | null;
        };
        /** RunSwitchPhase */
        RunSwitchPhase: {
            /** Detail */
            detail: string;
            /** Index */
            index: number;
            /**
             * Kind
             * @enum {string}
             */
            kind: "transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify";
            /** Node Ids */
            node_ids?: string[];
            /** Operation Digest */
            operation_digest?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "planned" | "retained" | "skipped" | "blocked";
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
        };
        /** RunSwitchPlan */
        RunSwitchPlan: {
            /**
             * Action
             * @enum {string}
             */
            action: "install" | "run" | "switch" | "stop" | "cleanup";
            /** Alias */
            alias: string | null;
            /** Allowed */
            allowed: boolean;
            /** Blockers */
            blockers: components["schemas"]["RunSwitchReason"][];
            build: components["schemas"]["RunSwitchBuildEvidence"];
            /**
             * Cleanup Disposition
             * @default uninstall
             * @enum {string}
             */
            cleanup_disposition?: "uninstall" | "abandon";
            /**
             * Cleanup Mode
             * @default uninstall
             * @enum {string}
             */
            cleanup_mode?: "uninstall" | "reconcile";
            /** Conflicts */
            conflicts: components["schemas"]["RunSwitchReason"][];
            effective_settings?: components["schemas"]["EffectiveSettingsSelection"] | null;
            fit: components["schemas"]["SparkFit"];
            fit_after_stop: components["schemas"]["SparkFit"] | null;
            fit_current: components["schemas"]["SparkFit"];
            /** Freshness */
            freshness?: components["schemas"]["FreshnessEvidence"][];
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            /** Image Digest */
            image_digest: string | null;
            /** Installation Id */
            installation_id: string | null;
            /** Installation State */
            installation_state: string | null;
            invocation: components["schemas"]["InvocationMetadata"];
            mapping: components["schemas"]["MappingSelection"] | null;
            /** Model Content Sha256 */
            model_content_sha256: string | null;
            /** Phases */
            phases: components["schemas"]["RunSwitchPhase"][];
            /** Plan Digest */
            plan_digest: string;
            post_stop_memory_check?: components["schemas"]["ConditionalPostStopMemoryCheck"] | null;
            preparation?: components["schemas"]["RolloutPreparation"] | null;
            profile_stop_scope?: components["schemas"]["RunSwitchProfileStopScope"] | null;
            /** Recipe Build Id */
            recipe_build_id: string | null;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string | null;
            /** Recipe Revision Id */
            recipe_revision_id: string | null;
            /** Reclaimed Bytes */
            reclaimed_bytes: number | ExactNumber;
            reconciliation_authority?: components["schemas"]["RunSwitchReconciliationAuthority"] | null;
            /** Run Id */
            run_id: string | null;
            runtime_storage: components["schemas"]["RuntimeImageStorageImpact"];
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            spark_group: components["schemas"]["SparkGroup"];
            /** Start Plan Digest */
            start_plan_digest: string | null;
            /**
             * Stop Before Prepare
             * @default false
             */
            stop_before_prepare?: boolean;
            /**
             * Stop Before Transfer
             * @default false
             */
            stop_before_transfer?: boolean;
            /** Stops */
            stops: components["schemas"]["StopImpact"][];
            storage: components["schemas"]["ArtifactStorageImpact"];
            /** Warnings */
            warnings: components["schemas"]["RunSwitchReason"][];
        };
        /** RunSwitchPreparedResult */
        RunSwitchPreparedResult: {
            /**
             * Phase
             * @constant
             */
            phase: "prepare";
            /**
             * Prepared
             * @constant
             */
            prepared: true;
            /**
             * Subphase
             * @constant
             */
            subphase: "runtime-plan";
        };
        /** RunSwitchProfileStopIntent */
        RunSwitchProfileStopIntent: {
            profile_stop_scope: components["schemas"]["RunSwitchProfileStopScope"];
            /** Run Id */
            run_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "profile-stop";
        };
        /**
         * RunSwitchProfileStopScope
         * @description Reviewed profile-only cleanup of the reachable ranks in a lost group.
         *
         *     The full accepted topology remains visible even though only its reachable
         *     subset is sent Stop work.  Missing ranks are explicit so a partial cleanup
         *     can never be presented as a successful full-group stop.
         */
        RunSwitchProfileStopScope: {
            /** Missing Node Ids */
            missing_node_ids: string[];
            original_group: components["schemas"]["SparkGroup"];
            /** Target Node Ids */
            target_node_ids: string[];
        };
        /** RunSwitchProgress */
        RunSwitchProgress: {
            /**
             * Completed Bytes
             * @default 0
             */
            completed_bytes?: number | ExactNumber;
            /** Members */
            members: components["schemas"]["RunSwitchMemberProgress"][];
            operation?: components["schemas"]["OperationProgress"] | null;
            /** Phase */
            phase: ("transfer" | "verify" | "prepare" | "cleanup" | "stop" | "start" | "uninstall" | "final_verify") | null;
            /** Phase Count */
            phase_count: number;
            /** Phase Index */
            phase_index: number;
            /** Start Deadline */
            start_deadline?: string | null;
            /** Startup Budget Seconds */
            startup_budget_seconds?: (number | ExactNumber) | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "backoff" | "observing" | "needs-operator" | "succeeded" | "failed" | "cancelled" | "unknown";
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
            /** Total Bytes */
            total_bytes?: (number | ExactNumber) | null;
            /** Total Bytes Known */
            total_bytes_known: boolean;
        };
        /** RunSwitchRankReceipt */
        RunSwitchRankReceipt: {
            /** Fresh */
            fresh?: boolean | null;
            /** Node Id */
            node_id: string;
            /** Rank */
            rank: number;
            /** Role */
            role: string;
            /** State */
            state: string;
        };
        /** RunSwitchReason */
        RunSwitchReason: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Node Ids */
            node_ids?: string[];
            /**
             * Scope
             * @enum {string}
             */
            scope: "model" | "recipe" | "mapping" | "group" | "node" | "artifact" | "freshness" | "conflict" | "operation";
            /**
             * Severity
             * @enum {string}
             */
            severity: "blocker" | "warning" | "info";
            /**
             * Stale
             * @default false
             */
            stale?: boolean;
        };
        /**
         * RunSwitchReconciliationAuthority
         * @description Controller-owned identity of the installation a repair removes.
         *
         *     The accepted installation plan remains opaque; it never claims that
         *     malformed launch metadata is executable.
         */
        RunSwitchReconciliationAuthority: {
            /** Image Digest */
            image_digest: string;
            /** Installation Id */
            installation_id: string;
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Model Content Sha256 */
            model_content_sha256: string | null;
            /** Original Plan Digest */
            original_plan_digest: string;
            /** Recipe Build Id */
            recipe_build_id: string | null;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Targets */
            targets: components["schemas"]["RunSwitchReconciliationTarget"][];
        };
        /**
         * RunSwitchReconciliationTarget
         * @description One exact rank and whether its cleanup already succeeded.
         */
        RunSwitchReconciliationTarget: {
            /** Installed Bytes */
            installed_bytes: number | ExactNumber;
            /** Node Id */
            node_id: string;
            /** Rank */
            rank: number;
            /** Role */
            role: string;
            /**
             * State
             * @enum {string}
             */
            state: "pending" | "reconciled";
        };
        /** RunSwitchRunIntent */
        RunSwitchRunIntent: {
            request: components["schemas"]["RunSwitchApplyRequest"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "run";
        };
        /**
         * RunSwitchRuntimeImageReferenceIntent
         * @description Exact image bytes provisionally protected by a current RunSwitch job.
         */
        RunSwitchRuntimeImageReferenceIntent: {
            /** Actor */
            actor: string;
            /** Archive Sha256 */
            archive_sha256: string;
            /** Build Id */
            build_id: string;
            /** Build Input Sha256 */
            build_input_sha256?: string | null;
            /** Execution Keys */
            execution_keys: string[];
            /** Image Bytes */
            image_bytes: number;
            /** Image Digest */
            image_digest: string;
            /** Item Index */
            item_index: number;
            /** Operation Id */
            operation_id: string;
            /**
             * Owner Kind
             * @constant
             */
            owner_kind: "run-switch-job";
            /** Phase Index */
            phase_index: number;
            /** Plan Digest */
            plan_digest: string;
            /** Profile Application Id */
            profile_application_id?: string | null;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Request Key */
            request_key: string;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /** Workload Intent Ordinal */
            workload_intent_ordinal: number;
        };
        /** RunSwitchRuntimeImageResult */
        RunSwitchRuntimeImageResult: {
            /** Build Id */
            build_id?: string | null;
            /** Effective Execution Key */
            effective_execution_key?: string | null;
            /** Image Bytes */
            image_bytes: number | ExactNumber;
            /** Image Digest */
            image_digest: string;
            /** Oci Layout Sha256 */
            oci_layout_sha256: string;
            /**
             * Phase
             * @constant
             */
            phase: "prepare";
            runtime_image: components["schemas"]["RuntimeImageReceipt"];
            /**
             * Subphase
             * @constant
             */
            subphase: "runtime-image";
        };
        /** RunSwitchRuntimeInstallResult */
        RunSwitchRuntimeInstallResult: {
            /** Installation Id */
            installation_id: string;
            /**
             * Phase
             * @constant
             */
            phase: "prepare";
            /**
             * Subphase
             * @constant
             */
            subphase: "runtime-install";
        };
        /** RunSwitchRuntimePlanResult */
        RunSwitchRuntimePlanResult: {
            /**
             * Compiled Plan Persisted
             * @constant
             */
            compiled_plan_persisted: true;
            /** Install Plan Digest */
            install_plan_digest: string;
            /** Installation Id */
            installation_id: string;
            /** Mapping Id */
            mapping_id: string;
            /** Model Artifact Set Bytes */
            model_artifact_set_bytes?: (number | ExactNumber) | null;
            /** Model Artifact Set Sha256 */
            model_artifact_set_sha256?: string | null;
            /**
             * Phase
             * @constant
             */
            phase: "prepare";
            /**
             * Subphase
             * @constant
             */
            subphase: "runtime-plan";
        };
        /** RunSwitchStartResult */
        RunSwitchStartResult: {
            /**
             * Phase
             * @constant
             */
            phase: "start";
            /** Run Id */
            run_id: string;
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
        };
        /** RunSwitchStopIntent */
        RunSwitchStopIntent: {
            invocation?: components["schemas"]["InvocationMetadata"];
            /**
             * Plan Digest
             * @default null
             */
            plan_digest?: string | null;
            /**
             * Request Key
             * @default null
             */
            request_key?: string | null;
            /** Run Id */
            run_id: string;
            /**
             * Schema Version
             * @default 2
             * @constant
             */
            schema_version?: number | ExactNumber;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "stop";
        };
        /** RunSwitchStopResult */
        RunSwitchStopResult: {
            /**
             * Phase
             * @constant
             */
            phase: "stop";
            /** Run Id */
            run_id: string;
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
        };
        /** RunSwitchTargetTransferEvidenceResult */
        RunSwitchTargetTransferEvidenceResult: {
            /** Copied Bytes */
            copied_bytes?: (number | ExactNumber) | null;
            /** Diagnostic */
            diagnostic?: string | null;
            /** Downloaded Bytes */
            downloaded_bytes?: (number | ExactNumber) | null;
            /** Error */
            error?: string | null;
            /** Error Code */
            error_code?: string | null;
            /** Failure Kind */
            failure_kind?: string | null;
            /** Node Id */
            node_id: string;
            /**
             * Phase
             * @constant
             */
            phase: "transfer";
            /** Reason */
            reason?: string | null;
            /**
             * Subphase
             * @constant
             */
            subphase: "target-copy";
            /**
             * Uncertain
             * @default false
             */
            uncertain?: boolean;
        };
        /** RunSwitchTargetTransferResult */
        RunSwitchTargetTransferResult: {
            /** Assignments */
            assignments: {
                [key: string]: components["schemas"]["NodeDistributionAssignment"];
            };
            /** Cached Nodes */
            cached_nodes?: string[];
            /**
             * Phase
             * @constant
             */
            phase: "transfer";
            /**
             * Subphase
             * @constant
             */
            subphase: "target-copy";
        };
        /**
         * RunSwitchUninstallResult
         * @description The removal of one installation that is no longer desired.
         */
        RunSwitchUninstallResult: {
            /**
             * Disposition
             * @default uninstalled
             * @enum {string}
             */
            disposition?: "uninstalled" | "abandoned";
            /** Installation Id */
            installation_id: string;
            /**
             * Phase
             * @constant
             */
            phase: "uninstall";
            /** Reason */
            reason?: string | null;
            /** Subphase */
            subphase?: ("container-build" | "model-download" | "runtime-image" | "runtime-plan" | "target-copy" | "runtime-install") | null;
        };
        /** RunSwitchVerifyResult */
        RunSwitchVerifyResult: {
            /** Cached Nodes */
            cached_nodes?: string[];
            /** Cached Target Totals */
            cached_target_totals?: {
                [key: string]: number | ExactNumber;
            };
            /** Evidence */
            evidence?: components["schemas"]["ArtifactVerificationEvidence"][];
            /**
             * Phase
             * @constant
             */
            phase: "verify";
            /**
             * Skipped
             * @default false
             */
            skipped?: boolean;
            /**
             * Subphase
             * @constant
             */
            subphase: "target-copy";
            /**
             * Verified
             * @constant
             */
            verified: true;
            /** Verified Build Id */
            verified_build_id: string | null;
            /** Verified Digests */
            verified_digests: string[];
            /** Verified Image Digest */
            verified_image_digest: string;
            /** Verified Oci Layout Sha256 */
            verified_oci_layout_sha256: string;
        };
        /** RuntimeArgument */
        RuntimeArgument: {
            /** Name */
            name: string;
            /**
             * Setting
             * @default null
             */
            setting?: string | null;
            /** @default null */
            value?: components["schemas"]["pydantic__types__JsonValue"] | null;
        };
        RuntimeArgumentValue: string | (number | ExactNumber) | boolean | (number | ExactNumber) | components["schemas"]["vonk_forge_contracts__recipe__JsonValue"][] | {
            [key: string]: components["schemas"]["vonk_forge_contracts__recipe__JsonValue"];
        };
        /** RuntimeEnvironmentEntry */
        RuntimeEnvironmentEntry: {
            /** Name */
            name: string;
            /** Value */
            value: string | (number | ExactNumber) | boolean | (number | ExactNumber);
        };
        /**
         * RuntimeImageCode
         * @description Runtime image preparation, receipt and registry problems.
         * @enum {string}
         */
        RuntimeImageCode: "registry.destination_forbidden" | "registry.digest_mismatch" | "registry.redirect_forbidden" | "runtime_image.architecture_mismatch" | "runtime_image.architecture_missing" | "runtime_image.archive_conflict" | "runtime_image.archive_invalid" | "runtime_image.archive_mismatch" | "runtime_image.archive_size_mismatch" | "runtime_image.archive_unavailable" | "runtime_image.build_archive_digest" | "runtime_image.build_digest" | "runtime_image.build_id" | "runtime_image.build_incomplete" | "runtime_image.cache_missing" | "runtime_image.config_missing" | "runtime_image.digest_invalid" | "runtime_image.digest_mismatch" | "runtime_image.evidence_invalid" | "runtime_image.identity_invalid" | "runtime_image.image_unpinned" | "runtime_image.inspect_invalid" | "runtime_image.insufficient_disk" | "runtime_image.interface_mismatch" | "runtime_image.interface_missing" | "runtime_image.lock_unavailable" | "runtime_image.publication_contended" | "runtime_image.receipt_contract_newer" | "runtime_image.receipt_identity_conflict" | "runtime_image.receipt_identity_invalid" | "runtime_image.receipt_invalid" | "runtime_image.receipt_persistence_failed" | "runtime_image.receipt_unavailable" | "runtime_image.receipt_write_failed" | "runtime_image.recipe_invalid" | "runtime_image.reference_intent_invalid" | "runtime_image.removal_storage_failed" | "runtime_image.runtime_invalid" | "runtime_image.source_mismatch" | "runtime_image.transfer_contended";
        /**
         * RuntimeImageIdentity
         * @description Executable OCI identity, independent of its transfer observations.
         */
        RuntimeImageIdentity: {
            /**
             * Architecture
             * @constant
             */
            architecture: "linux-arm64";
            /** Build Id */
            build_id?: string | null;
            /** Image Bytes */
            image_bytes: number | ExactNumber;
            /** Image Digest */
            image_digest: string;
            /** Oci Layout Sha256 */
            oci_layout_sha256: string;
            /** Runtime Interface */
            runtime_interface: string;
        };
        /**
         * RuntimeImagePreparation
         * @description Exact executable OCI image kept separate from model payloads.
         */
        RuntimeImagePreparation: {
            /**
             * Architecture
             * @constant
             */
            architecture: "linux-arm64";
            /** Build Id */
            build_id?: string | null;
            controller: components["schemas"]["ControllerAssetState"];
            /** Image Bytes */
            image_bytes: number | ExactNumber;
            /** Image Digest */
            image_digest: string;
            /** Oci Layout Sha256 */
            oci_layout_sha256: string;
            /** Runtime Interface */
            runtime_interface: string;
            /** Targets */
            targets: components["schemas"]["TargetAssetState"][];
        };
        /**
         * RuntimeImageReceipt
         * @description Strict schema-2 receipt persisted by the Controller image cache.
         *
         *     ``oci_archive_sha256`` is the established filesystem/SQL receipt field.
         *     The compiled launch plan uses its own ``oci_layout_sha256`` field; the
         *     execution-plan service performs that one explicit typed projection at the
         *     plan boundary.
         */
        RuntimeImageReceipt: {
            /**
             * Architecture
             * @constant
             */
            architecture: "linux-arm64";
            /** Archive Path */
            archive_path: string;
            /** Build Id */
            build_id: string;
            /** Build Input Sha256 */
            build_input_sha256?: string | null;
            /** Distribution Content Sha256 */
            distribution_content_sha256: string;
            /** Distribution Publisher */
            distribution_publisher: string;
            /** Distribution Slug */
            distribution_slug: string;
            /** Image Bytes */
            image_bytes: number;
            /** Image Digest */
            image_digest: string;
            /** Local Image Config Id */
            local_image_config_id: string;
            /** Oci Archive Sha256 */
            oci_archive_sha256: string;
            /** Recorded At */
            recorded_at: string;
            /** Runtime Adapter */
            runtime_adapter: string;
            /** Runtime Adapter Sha256 */
            runtime_adapter_sha256: string;
            /**
             * Runtime Interface
             * @constant
             */
            runtime_interface: "vonk.runtime.v1";
            /**
             * Runtime Interface Label
             * @constant
             */
            runtime_interface_label: "v1";
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /**
         * RuntimeImageReferenceIntent
         * @description Exact SQL coordination identity for an image archive publication.
         *
         *     This intent protects an archive while the existing availability owner is
         *     publishing it. It is not evidence that managed bytes or a receipt exist;
         *     those facts remain owned by ``RuntimeImageStorage``.
         */
        RuntimeImageReferenceIntent: {
            /** Attempt */
            attempt: number;
            /** Claim Owner */
            claim_owner: string;
            /** Image Bytes */
            image_bytes: number;
            /** Image Digest */
            image_digest: string;
            /** Oci Archive Sha256 */
            oci_archive_sha256: string;
            /** Operation Id */
            operation_id: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /** RuntimeImageStorageImpact */
        RuntimeImageStorageImpact: {
            /** Build Id */
            build_id: string | null;
            /**
             * Copied Bytes
             * @default 0
             */
            copied_bytes?: number | ExactNumber;
            /** Image Bytes */
            image_bytes?: (number | ExactNumber) | null;
            /** Image Digest */
            image_digest: string | null;
            /** Missing Image Distribution Bytes */
            missing_image_distribution_bytes?: (number | ExactNumber) | null;
            /** Missing Image Distribution Bytes By Node */
            missing_image_distribution_bytes_by_node?: {
                [key: string]: number | ExactNumber;
            } | null;
            /** Missing Nas Bytes */
            missing_nas_bytes?: (number | ExactNumber) | null;
            /** Missing Spark Bytes */
            missing_spark_bytes?: (number | ExactNumber) | null;
            /**
             * Nas Coverage
             * @enum {string}
             */
            nas_coverage: "complete" | "partial" | "unknown";
            /** Oci Layout Sha256 */
            oci_layout_sha256?: string | null;
            /** Preparation Required */
            preparation_required: boolean;
            /**
             * Reclaimable Bytes
             * @default 0
             */
            reclaimable_bytes?: number | ExactNumber;
            /** Reclaimable Digests */
            reclaimable_digests?: string[];
            /** Required Bytes */
            required_bytes?: (number | ExactNumber) | null;
            /**
             * Reused Bytes
             * @default 0
             */
            reused_bytes?: number | ExactNumber;
            /**
             * Running Coverage
             * @default unknown
             * @enum {string}
             */
            running_coverage?: "complete" | "partial" | "unknown";
            /**
             * Spark Coverage
             * @enum {string}
             */
            spark_coverage: "complete" | "partial" | "unknown";
        };
        /**
         * RuntimePreflightCode
         * @description Runtime preflight blockers.
         * @enum {string}
         */
        RuntimePreflightCode: "runtime_preflight.child_missing" | "runtime_preflight.execution_failed" | "runtime_preflight.host_changed" | "runtime_preflight.node_revoked" | "runtime_preflight.operation_missing" | "runtime_preflight.receipt_invalid" | "runtime_preflight.receipt_missing" | "runtime_preflight.required" | "runtime_preflight.requirement_unknown" | "runtime_preflight.requirements_changed" | "runtime_preflight.stale" | "runtime_preflight.node_missing" | "runtime_preflight.capability_failed";
        /**
         * RuntimePreflightFinding
         * @description One capability's verdict.
         *
         *     ``code`` travels as the word of a :class:`RuntimePreflightFindingCode` member.
         *     It stays a pattern-bound string on the wire so an older agent's free-text
         *     code (``available``, ``proc-mount-denied``) and a newer agent's word still
         *     read; :attr:`finding_code` is the typed reading of either.
         */
        RuntimePreflightFinding: {
            /** Capability */
            capability: string;
            /** Code */
            code: string;
            /**
             * Status
             * @enum {string}
             */
            status: "passed" | "failed" | "unknown";
        };
        /**
         * RuntimePreflightFindingCode
         * @description Why one runtime preflight capability passed, failed or stayed unknown, as the agent reports it.
         *
         *     The agent builds every finding from a member; the Controller reads a code an
         *     older agent sent as free text through :func:`adopt_preflight_finding_code`.
         * @enum {string}
         */
        RuntimePreflightFindingCode: "preflight_finding.architecture_mismatch" | "preflight_finding.available" | "preflight_finding.build_step_failed" | "preflight_finding.capabilities_not_zero" | "preflight_finding.controller_unreachable" | "preflight_finding.deadline_exceeded" | "preflight_finding.diagnostic_limit_exceeded" | "preflight_finding.directory_not_writable" | "preflight_finding.disk_reserve_insufficient" | "preflight_finding.fabric_requirement_unverified" | "preflight_finding.helper_call_join_failed" | "preflight_finding.helper_capabilities_not_zero" | "preflight_finding.helper_grant_invalid" | "preflight_finding.helper_grant_node_mismatch" | "preflight_finding.helper_grant_unauthorized" | "preflight_finding.helper_grant_unavailable" | "preflight_finding.helper_image_import_failed" | "preflight_finding.helper_inspection_outcome_invalid" | "preflight_finding.helper_installation_reconciliation_busy" | "preflight_finding.helper_installation_reconciliation_storage_unavailable" | "preflight_finding.helper_io_failed" | "preflight_finding.helper_message_framing_invalid" | "preflight_finding.helper_mount_namespace_unavailable" | "preflight_finding.helper_no_new_privileges_unavailable" | "preflight_finding.helper_operation_command_failed" | "preflight_finding.helper_operation_failed" | "preflight_finding.helper_operation_invalid" | "preflight_finding.helper_operation_invalid_artifact" | "preflight_finding.helper_operation_io" | "preflight_finding.helper_operation_stop_uncertain" | "preflight_finding.helper_operation_unsafe_path" | "preflight_finding.helper_outcome_malformed" | "preflight_finding.helper_peer_identity_invalid" | "preflight_finding.helper_probe_cleanup_failed" | "preflight_finding.helper_probe_invalid_result" | "preflight_finding.helper_proc_unavailable" | "preflight_finding.helper_protocol_invalid" | "preflight_finding.helper_rejection_malformed" | "preflight_finding.helper_request_arguments_presence_invalid" | "preflight_finding.helper_request_argument_nul_byte" | "preflight_finding.helper_request_attempt_invalid" | "preflight_finding.helper_request_bytes_invalid" | "preflight_finding.helper_request_document_invalid" | "preflight_finding.helper_request_encoding_invalid" | "preflight_finding.helper_request_installation_identity_invalid" | "preflight_finding.helper_request_invalid" | "preflight_finding.helper_request_ledger_failed" | "preflight_finding.helper_request_plan_binding_invalid" | "preflight_finding.helper_request_plan_bytes_invalid" | "preflight_finding.helper_request_replayed" | "preflight_finding.helper_request_schema_version_invalid" | "preflight_finding.helper_request_storage_invalid" | "preflight_finding.helper_response_unbound" | "preflight_finding.helper_runtime_endpoint_firewall_rejected" | "preflight_finding.helper_runtime_fabric_firewall_rejected" | "preflight_finding.helper_runtime_fabric_unavailable" | "preflight_finding.helper_runtime_image_identity_invalid" | "preflight_finding.helper_runtime_image_inspect_failed" | "preflight_finding.helper_runtime_image_load_failed" | "preflight_finding.helper_runtime_image_receipt_failed" | "preflight_finding.helper_runtime_process_exited" | "preflight_finding.helper_runtime_run_missing" | "preflight_finding.helper_sandbox_run_failed" | "preflight_finding.helper_stop_uncertain" | "preflight_finding.helper_system_clock_invalid" | "preflight_finding.helper_temporary_directory_unavailable" | "preflight_finding.memory_limit_exceeded" | "preflight_finding.mount_namespace_unavailable" | "preflight_finding.nonzero_without_output" | "preflight_finding.no_new_privileges_unavailable" | "preflight_finding.oci_runtime_unavailable" | "preflight_finding.patch_rejected" | "preflight_finding.permission_denied" | "preflight_finding.proc_mount_denied" | "preflight_finding.proc_unavailable" | "preflight_finding.runroot_exceeds_50_bytes" | "preflight_finding.signed_helper_probe_required" | "preflight_finding.storage_driver_failure" | "preflight_finding.subordinate_id_mapping_unavailable" | "preflight_finding.subprocess_unavailable" | "preflight_finding.systemd_scope_failure" | "preflight_finding.temporary_directory_unavailable" | "preflight_finding.temporary_storage_exhausted" | "preflight_finding.unclassified" | "preflight_finding.unclassified_podman_build_failure" | "preflight_finding.user_namespace_denied" | "preflight_finding.user_service_manager_unavailable";
        /**
         * RuntimePreflightRequest
         * @description Check this linux/arm64 host can run one recipe runtime.
         */
        RuntimePreflightRequest: {
            /**
             * Fabric Connectivity
             * @enum {string}
             */
            fabric_connectivity: "none" | "connected";
            /** Fabric Minimum Mbps */
            fabric_minimum_mbps: number | ExactNumber;
            /** Minimum Free Bytes */
            minimum_free_bytes: number | ExactNumber;
            /** Source Build */
            source_build: boolean;
        };
        /** RuntimePreflightResult */
        RuntimePreflightResult: {
            /** Findings */
            findings: components["schemas"]["RuntimePreflightFinding"][];
            /** Fingerprint */
            fingerprint: string;
            /** Observed At */
            observed_at: number | ExactNumber;
        };
        /** RuntimeTelemetryProjection */
        RuntimeTelemetryProjection: {
            /** Engine */
            engine: string;
            /**
             * Engine Version
             * @default null
             */
            engine_version?: string | null;
            /**
             * Metrics Format
             * @default null
             */
            metrics_format?: string | null;
            /**
             * Metrics Path
             * @default null
             */
            metrics_path?: string | null;
        };
        /**
         * SavedProfileProjectionIssue
         * @description An observation problem; it never authorizes changing saved intent.
         */
        SavedProfileProjectionIssue: {
            /**
             * Code
             * @default profile.definition_unavailable
             * @constant
             */
            code?: "profile.definition_unavailable";
            /** Detail */
            detail: string;
            /** Next Action */
            next_action: string;
        };
        /**
         * SecurityRefusal
         * @description A refused request at a security boundary; it fails closed.
         */
        SecurityRefusal: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            category: "security-refusal";
            reason: components["schemas"]["SecurityRefusalReason"];
        };
        /**
         * SecurityRefusalReason
         * @description Closed reason codes of a security refusal: a real security boundary.
         *
         *     Authentication and authorization, identity and certificate expiry, node
         *     revocation, enrollment, signed package metadata, host-helper authority,
         *     tombstone fencing, credential denial and the digest-bound destructive-effect
         *     checks of Run/Switch.  ``failure_classification`` derives its code set from
         *     this enum, so the Controller and the contract cannot disagree.
         * @enum {string}
         */
        SecurityRefusalReason: "401" | "403" | "agent.certificate.rotation.conflict" | "agent.enrollment.submit.rejected" | "agent.expired_renewal_refused" | "agent.expired_renewal_grace_exhausted" | "agent.identity_mismatch" | "agent.tombstone_fenced" | "catalog.authentication_required" | "controller.authentication_required" | "controller.fleet.enrollment_denied" | "controller.request_rejected" | "digest_verification_failed" | "distribution.revoked" | "forbidden" | "grant_invalid" | "grant_node_mismatch" | "grant_unauthorized" | "helper.authorization_invalid" | "helper_grant_invalid" | "helper_grant_node_mismatch" | "helper_grant_unauthorized" | "helper_operation_invalid_artifact" | "helper_peer_identity_invalid" | "helper_request_installation_identity_invalid" | "helper_request_plan_binding_invalid" | "helper_request_replayed" | "helper_runtime_image_identity_invalid" | "host_helper.authority_denied" | "local.identity_expired" | "local.identity_failed" | "model_cache.credentials_denied" | "model_cache.credentials_invalid" | "model_cache.source_access_denied" | "operation_invalid_artifact" | "peer_identity_invalid" | "permission_denied" | "recipe_update.authority_denied" | "request_replayed" | "run-switch.artifact-digest-verification-failed" | "run-switch.cleanup-nas-eviction-forbidden" | "run-switch.cleanup-reclaimed-digest-not-planned" | "run-switch.runtime-image-preparation-digest-mismatch" | "runtime_image.authorization_invalid" | "runtime_image.authorization_revoked" | "runtime_image_identity_invalid" | "stale_fence" | "tuf.metadata_invalid" | "tuf.signature_invalid" | "unauthorized" | "unsafe_path";
        /**
         * ServiceRunStopReview
         * @description The exact service Stop accepted before its route withdrawal claim.
         */
        ServiceRunStopReview: {
            /**
             * Exact Payloads
             * @default null
             */
            exact_payloads?: {
                [key: string]: components["schemas"]["RecipeStopPayload"];
            } | null;
            /** Missing Node Ids */
            missing_node_ids: string[];
            /**
             * Profile Application Id
             * @default null
             */
            profile_application_id?: string | null;
            /** @default null */
            profile_stop_owner?: components["schemas"]["ProfileStopOwnerBinding"] | null;
            /**
             * Profile Target Node Ids
             * @default null
             */
            profile_target_node_ids?: string[] | null;
            route_state: components["schemas"]["RouteState"];
            /** Run Generation */
            run_generation: number | ExactNumber;
            /**
             * Stage
             * @enum {string}
             */
            stage: "accepted" | "withdrawal-claimed" | "dispatched";
            /**
             * Stop Order
             * @default null
             */
            stop_order?: string[] | null;
            /** Target Node Ids */
            target_node_ids: string[];
        };
        /**
         * SourceBundleCode
         * @description Recipe source bundle validation problems.
         * @enum {string}
         */
        SourceBundleCode: "bundle.archive_too_large" | "bundle.digest_invalid" | "bundle.digest_mismatch" | "bundle.duplicate_path" | "bundle.empty" | "bundle.entry_forbidden" | "bundle.expanded_too_large" | "bundle.file_invalid" | "bundle.file_too_large" | "bundle.invalid_archive" | "bundle.manifest_invalid" | "bundle.metadata_mismatch" | "bundle.not_found" | "bundle.path_forbidden" | "bundle.path_too_long" | "bundle.read_failed" | "bundle.size_mismatch" | "bundle.storage_collision" | "bundle.storage_conflict" | "bundle.storage_unavailable" | "bundle.too_many_files" | "source.digest_mismatch";
        /** SourceBundleFile */
        SourceBundleFile: {
            /**
             * Mode
             * @enum {integer}
             */
            mode: number | ExactNumber;
            /** Path */
            path: string;
            /** Sha256 */
            sha256: string;
            /** Size */
            size: number;
        };
        /** SourceBundleManifest */
        SourceBundleManifest: {
            /** Files */
            files: components["schemas"]["SourceBundleFile"][];
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
            /** Sha256 */
            sha256: string;
            /** Total Bytes */
            total_bytes: number;
        };
        /**
         * SourcePolicyCode
         * @description Findings of the build source policy (Dockerfile and Compose rules).
         * @enum {string}
         */
        SourcePolicyCode: "compose.capabilities" | "compose.devices" | "compose.host_bind" | "compose.host_namespace" | "compose.invalid" | "compose.privileged" | "compose.service_invalid" | "compose.too_large" | "compose.unconfined" | "compose.volumes_invalid" | "dockerfile.add_forbidden" | "dockerfile.base_placeholder" | "dockerfile.base_unpinned" | "dockerfile.build_privilege" | "dockerfile.copy_base_placeholder" | "dockerfile.copy_base_unpinned" | "dockerfile.copy_invalid" | "dockerfile.copy_path" | "dockerfile.from_missing" | "dockerfile.heredoc_forbidden" | "dockerfile.invalid_utf8" | "dockerfile.missing" | "dockerfile.network_host" | "dockerfile.onbuild_forbidden" | "dockerfile.root_user" | "dockerfile.secret_mount";
        /** SparkFit */
        SparkFit: {
            /** Allowed */
            allowed: boolean;
            /** Blockers */
            blockers?: components["schemas"]["RunSwitchReason"][];
            /** Nodes */
            nodes: components["schemas"]["SparkFitNode"][];
            /** Warnings */
            warnings?: components["schemas"]["RunSwitchReason"][];
        };
        /** SparkFitNode */
        SparkFitNode: {
            /** Allowed */
            allowed: boolean;
            /** Blockers */
            blockers?: components["schemas"]["RunSwitchReason"][];
            /** Disk Free After Bytes */
            disk_free_after_bytes?: (number | ExactNumber) | null;
            /** Disk Free Bytes */
            disk_free_bytes?: (number | ExactNumber) | null;
            /** Disk Required Bytes */
            disk_required_bytes?: (number | ExactNumber) | null;
            /** Memory Available Bytes */
            memory_available_bytes?: (number | ExactNumber) | null;
            /** Memory Capacity Bytes */
            memory_capacity_bytes?: (number | ExactNumber) | null;
            /** Memory Floor Bytes */
            memory_floor_bytes?: (number | ExactNumber) | null;
            /** Memory Free After Bytes */
            memory_free_after_bytes?: (number | ExactNumber) | null;
            /** Memory Kind */
            memory_kind?: ("unified" | "host" | "accelerator") | null;
            /** Memory Pool */
            memory_pool?: ("shared" | "separate") | null;
            /** Memory Required Bytes */
            memory_required_bytes?: (number | ExactNumber) | null;
            memory_usage_uncertainty?: components["schemas"]["MemoryUsageUncertainty"] | null;
            /** Node Id */
            node_id: string;
            /** Ports Required */
            ports_required: number[];
            /** Rank */
            rank: number;
            resource_demand?: components["schemas"]["ResourceDemandEvidence"] | null;
            /** Role */
            role: string;
            /** Warnings */
            warnings?: components["schemas"]["RunSwitchReason"][];
        };
        /**
         * SparkGroup
         * @description A complete, rank-labelled Spark group selected by the operator.
         */
        SparkGroup: {
            /** Nodes */
            nodes: components["schemas"]["SparkGroupNode"][];
        };
        /** SparkGroupNode */
        SparkGroupNode: {
            /**
             * Endpoint Owner
             * @default false
             */
            endpoint_owner?: boolean;
            /** Node Id */
            node_id: string;
            /** Rank */
            rank: number;
            /** Role */
            role: string;
        };
        /** StartPhaseOperation */
        StartPhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeStartPayload"];
        };
        /**
         * StateAlias
         * @description The retired spellings of a stored lifecycle state.
         *
         *     They are accepted as input for one release (API filters, CLI arguments) and
         *     adopted when an old row is read; nothing writes them any more.  This is the
         *     only place the words may be spelled: the vocabulary ratchet allows them
         *     nowhere else.
         * @enum {string}
         */
        StateAlias: "waiting-for-operator" | "cancelling" | "waiting" | "partial" | "expired";
        /**
         * StateWriteKind
         * @description The shapes of a lifecycle state write the writers ratchet recognises.
         * @enum {string}
         */
        StateWriteKind: "attribute" | "dict-item" | "bulk-update" | "constructor" | "helper-call";
        /** StopImpact */
        StopImpact: {
            /** Alias */
            alias: string;
            /** Node Ids */
            node_ids: string[];
            /** Plan Digest */
            plan_digest: string;
            /** Reserved Bytes */
            reserved_bytes: number | ExactNumber;
            /** Run Id */
            run_id: string;
            /** Run Plan Digest */
            run_plan_digest: string;
            /** State */
            state: string;
        };
        /**
         * StopOutcome
         * @description Whether an idempotent stop confirmed that the effect is gone.
         * @enum {string}
         */
        StopOutcome: "confirmed" | "unconfirmed";
        /** StopPhaseOperation */
        StopPhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeStopPayload"];
        };
        /**
         * StopPlanCode
         * @description Why a stop plan is stale or cannot be taken.
         * @enum {string}
         */
        StopPlanCode: "stop.capacity_release_deferred" | "stop.rank_membership_changed" | "stop.reservation_membership_changed" | "stop.run_not_stoppable" | "stop.target_scope_changed";
        /**
         * StorageDemandCode
         * @description Storage demand outcomes of an admission.
         * @enum {string}
         */
        StorageDemandCode: "storage.evicting" | "storage.insufficient_after_eviction" | "storage.eviction_timed_out";
        /** StoredAdmissionReason */
        StoredAdmissionReason: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
        };
        /** StoredBuildPolicyReport */
        StoredBuildPolicyReport: {
            /** Artifact Format */
            artifact_format: string;
            /**
             * Builder Binary Digest
             * @default null
             */
            builder_binary_digest?: string | null;
            /** Dockerfile */
            dockerfile: string;
            /** Findings */
            findings: components["schemas"]["StoredPolicyFinding"][];
            /** Passed */
            passed: boolean;
            /** @default null */
            prebuilt_decision?: components["schemas"]["StoredPrebuiltDecision"] | null;
            /**
             * Prebuilt Image
             * @default null
             */
            prebuilt_image?: string | null;
            /** Source Bundle Sha256 */
            source_bundle_sha256: string;
        };
        /** StoredInstallNodePlan */
        StoredInstallNodePlan: {
            /** Active Reserved Bytes */
            active_reserved_bytes: number | ExactNumber;
            /** Allowed */
            allowed: boolean;
            /** Blockers */
            blockers: components["schemas"]["StoredAdmissionReason"][];
            /** Disk Floor Bytes */
            disk_floor_bytes: number | ExactNumber;
            /** Free After Bytes */
            free_after_bytes: (number | ExactNumber) | null;
            /** Free Bytes */
            free_bytes: (number | ExactNumber) | null;
            /** Inventory Observed At */
            inventory_observed_at: string | null;
            /** Node Id */
            node_id: string;
            /** Rank */
            rank: number;
            /** Required Bytes */
            required_bytes: number | ExactNumber;
            /** Required Download Bytes */
            required_download_bytes: number | ExactNumber;
            /**
             * Required Payload Bytes
             * @default null
             */
            required_payload_bytes?: (number | ExactNumber) | null;
            /** Reused Bytes */
            reused_bytes: number | ExactNumber;
            /** Role */
            role: string;
            /** Warnings */
            warnings: components["schemas"]["StoredAdmissionReason"][];
        };
        /** StoredInstallationPlan */
        StoredInstallationPlan: {
            /** Allowed */
            allowed: boolean;
            /** Compiled Execution Plans */
            compiled_execution_plans: {
                [key: string]: components["schemas"]["CompiledExecutionPlan"];
            };
            /** Image Digest */
            image_digest: string;
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Nodes */
            nodes: components["schemas"]["StoredInstallNodePlan"][];
            /** Plan Digest */
            plan_digest: string;
            /** Recipe Build Id */
            recipe_build_id: string | null;
            /** Recipe Content Sha256 */
            recipe_content_sha256: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /** StoredPolicyFinding */
        StoredPolicyFinding: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
            /** Line */
            line: (number | ExactNumber) | null;
            /** Path */
            path: string;
        };
        /**
         * StoredPrebuiltDecision
         * @description Why the plan did or did not use the catalog's prebuilt image.
         */
        StoredPrebuiltDecision: {
            /** Code */
            code: string;
            /** Detail */
            detail: string;
        };
        /** StoredRunEndpoint */
        StoredRunEndpoint: {
            /** Url */
            url: string;
        };
        /** StoredRunNodePlan */
        StoredRunNodePlan: {
            /** Active Reserved Bytes */
            active_reserved_bytes: number | ExactNumber;
            /** Allowed */
            allowed: boolean;
            /** Available Memory Bytes */
            available_memory_bytes: (number | ExactNumber) | null;
            /** Blockers */
            blockers: components["schemas"]["StoredAdmissionReason"][];
            /** Endpoint Owner */
            endpoint_owner: boolean;
            /** Fabric Address */
            fabric_address: string | null;
            /** Fabric Bandwidth Mbps */
            fabric_bandwidth_mbps: (number | ExactNumber) | null;
            /** Free After Bytes */
            free_after_bytes: (number | ExactNumber) | null;
            /** Inventory Observed At */
            inventory_observed_at: string | null;
            /** Memory Floor Bytes */
            memory_floor_bytes: number | ExactNumber;
            /**
             * Memory Kind
             * @enum {string}
             */
            memory_kind: "unified" | "host" | "accelerator";
            /**
             * Memory Pool
             * @enum {string}
             */
            memory_pool: "shared" | "separate";
            /** Node Id */
            node_id: string;
            /** Port */
            port: number;
            /** Rank */
            rank: number;
            /** Rendezvous Port */
            rendezvous_port: number | null;
            /** Required Memory Bytes */
            required_memory_bytes: number | ExactNumber;
            /** Role */
            role: string;
            /** Warnings */
            warnings: components["schemas"]["StoredAdmissionReason"][];
        };
        /** StoredRunPlan */
        StoredRunPlan: {
            /** Alias */
            alias: string;
            /**
             * Execution Mode
             * @default null
             */
            execution_mode?: "one-shot-jobs" | null;
            /** Installation Id */
            installation_id: string;
            /** Mapping Generation */
            mapping_generation: number;
            /** Mapping Id */
            mapping_id: string;
            /** Nodes */
            nodes: components["schemas"]["StoredRunNodePlan"][];
            /**
             * Observation Schema Version
             * @constant
             */
            observation_schema_version: number | ExactNumber;
            /** Plan Digest */
            plan_digest: string;
            /** Recipe Revision Id */
            recipe_revision_id: string;
            /** Run Generation */
            run_generation: number | ExactNumber;
            /**
             * Schema Version
             * @constant
             */
            schema_version: number | ExactNumber;
        };
        /** StringParameter */
        StringParameter: {
            /** Allowed Values */
            allowed_values?: components["schemas"]["ParameterScalar"][];
            /** Default */
            default: string;
            /**
             * Maximum
             * @default null
             */
            maximum?: null;
            /**
             * Minimum
             * @default null
             */
            minimum?: null;
            /** Name */
            name: string;
            /** Pattern */
            pattern?: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            type: "string";
        };
        /**
         * SupersedeCode
         * @description Why a fleet profile application was superseded by newer intent.
         * @enum {string}
         */
        SupersedeCode: "superseded-by-intent" | "superseded-by-retry" | "effects-changed-during-admission";
        /**
         * TargetAssetState
         * @description Staging and verification state for one immutable asset on one Spark.
         */
        TargetAssetState: {
            /** Expected Bytes */
            expected_bytes?: (number | ExactNumber) | null;
            /** Imported Image Digest */
            imported_image_digest?: string | null;
            /** Missing Bytes */
            missing_bytes?: (number | ExactNumber) | null;
            /** Node Id */
            node_id: string;
            /**
             * Present Bytes
             * @default 0
             */
            present_bytes?: number | ExactNumber;
            /** Reason */
            reason?: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "unknown" | "missing" | "preparing" | "verifying" | "ready" | "failed" | "unsupported";
            /** Verified At */
            verified_at?: string | null;
            /** Verified Sha256 */
            verified_sha256?: string | null;
        };
        /** TelemetryPoint */
        TelemetryPoint: {
            /** Boot Id */
            boot_id: string;
            /** Cpu Frequency Avg Mhz */
            cpu_frequency_avg_mhz?: number | null;
            /** Cpu Frequency Max Mhz */
            cpu_frequency_max_mhz?: number | null;
            /** Cpu Frequency Min Mhz */
            cpu_frequency_min_mhz?: number | null;
            /** Disk Free Bytes */
            disk_free_bytes?: number | null;
            /** Disk Total Bytes */
            disk_total_bytes?: number | null;
            /** Gpu Memory Free Bytes */
            gpu_memory_free_bytes?: number | null;
            /** Gpu Memory Total Bytes */
            gpu_memory_total_bytes?: number | null;
            /** Gpu Temperature C */
            gpu_temperature_c?: number | null;
            gpu_unavailable_reason?: components["schemas"]["GpuUnavailableReason"] | null;
            /** Gpu Utilization Percent */
            gpu_utilization_percent?: number | null;
            /** Id */
            id: string;
            /** Memory Available Bytes */
            memory_available_bytes?: number | null;
            /** Memory Total Bytes */
            memory_total_bytes?: number | null;
            /** Node Id */
            node_id: string;
            /**
             * Observed At
             * Format: date-time
             */
            observed_at: string;
            /**
             * Received At
             * Format: date-time
             */
            received_at: string;
        };
        /** TelemetryState */
        TelemetryState: {
            /** Age Seconds */
            age_seconds: number | ExactNumber;
            /**
             * Freshness
             * @enum {string}
             */
            freshness: "live" | "delayed" | "stale";
            sample: components["schemas"]["TelemetryPoint"];
        };
        /**
         * TopologyCode
         * @description Topology planning refusals.
         * @enum {string}
         */
        TopologyCode: "topology.fabric_insufficient" | "topology.invalid" | "topology.placement_invalid" | "topology.role_mismatch" | "topology.runtime_capability_missing";
        /**
         * UnavailableFleetProfileView
         * @description Keep an authorized saved identity visible without inventing its contents.
         */
        UnavailableFleetProfileView: {
            /** Definition */
            definition?: null;
            /** Id */
            id: string;
            /** Number */
            number: number;
            projection_issue: components["schemas"]["SavedProfileProjectionIssue"];
            /** Revision */
            revision: number;
            /**
             * Status
             * @default unavailable
             * @constant
             */
            status?: "unavailable";
        };
        /**
         * UnavailableRecipePresence
         * @description Known membership whose stored group evidence cannot be projected.
         */
        UnavailableRecipePresence: {
            /** Affected Ranks */
            affected_ranks?: null;
            /** Complete */
            complete: null;
            /** Degraded Reason */
            degraded_reason?: null;
            /** Expected Rank Count */
            expected_rank_count?: number | null;
            group_state?: components["schemas"]["InstallationState"] | null;
            /** Installation Id */
            installation_id: string;
            /** Installed Bytes */
            installed_bytes?: (number | ExactNumber) | null;
            /** Member Node Ids */
            member_node_ids?: string[] | null;
            /** Present Ranks */
            present_ranks?: number[] | null;
            /** Projection Issue */
            projection_issue: string;
            /** Rank */
            rank?: number | null;
            rank_state?: components["schemas"]["InstallationState"] | null;
            /** Recipe Id */
            recipe_id?: string | null;
            /** Recipe Revision Id */
            recipe_revision_id?: string | null;
            /** Required Bytes */
            required_bytes?: null;
            /** Role */
            role?: string | null;
            /** Title */
            title?: string | null;
            /** Topology Name */
            topology_name?: string | null;
        };
        /** UnavailableRunPresence */
        UnavailableRunPresence: {
            /** Alias */
            alias?: string | null;
            /** Degraded Reason */
            degraded_reason?: null;
            /** Expected Rank Count */
            expected_rank_count?: number | null;
            /**
             * Group State
             * @default unavailable
             * @constant
             */
            group_state?: "unavailable";
            /** Healthy */
            healthy: null;
            /** Installation Id */
            installation_id?: string | null;
            /** Member Node Ids */
            member_node_ids?: string[] | null;
            /** Option Choices */
            option_choices?: null;
            /** Present Ranks */
            present_ranks?: number[] | null;
            /** Projection Issue */
            projection_issue: string;
            /** Rank */
            rank?: number | null;
            /** Rank Age Seconds */
            rank_age_seconds?: null;
            /** Rank Fresh */
            rank_fresh?: null;
            rank_state?: components["schemas"]["RunState"] | null;
            /** Recipe Id */
            recipe_id?: string | null;
            /** Recipe Revision Id */
            recipe_revision_id?: string | null;
            /** Recipe Update */
            recipe_update?: null;
            /** Role */
            role?: string | null;
            /** Route Reason */
            route_reason?: null;
            route_state?: components["schemas"]["RouteState"] | null;
            /** Run Id */
            run_id: string;
            run_state?: components["schemas"]["RunState"] | null;
            /** Title */
            title?: string | null;
        };
        /** UninstallPhaseOperation */
        UninstallPhaseOperation: {
            /** Node Id */
            node_id: string;
            /** Operation Id */
            operation_id: string;
            payload: components["schemas"]["RecipeUninstallPayload"];
        };
        /**
         * UninstallPlanCode
         * @description Why an uninstall plan is blocked or incomplete.
         * @enum {string}
         */
        UninstallPlanCode: "uninstall.abandon-never-installed" | "uninstall.active_run" | "uninstall.active_runs_truncated" | "uninstall.bytes_unknown" | "uninstall.installation_not_uninstallable" | "uninstall.operation_active" | "uninstall.rank_membership_changed";
        /**
         * UnknownError
         * @description Anything else: observed and reconciled, never parked.
         */
        UnknownError: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            category: "unknown";
            reason: components["schemas"]["WaitReason"];
        };
        /**
         * UnprojectedRevision
         * @description The column's own default: a revision whose projection is not derived yet.
         *
         *     ``catalog_document_revisions.projected`` defaults to ``{}``.  The importer
         *     always writes the derived projection; a row that still holds the default is
         *     read as unknown by :func:`read_catalog_projection` and re-derived by the
         *     catalog's own repair path.
         */
        UnprojectedRevision: {
            /**
             * Failure Reason
             * @default null
             */
            failure_reason?: string | null;
            /** @default null */
            package_handle?: components["schemas"]["RecipePackageHandleProjection"] | null;
            /**
             * Package Sha256
             * @default null
             */
            package_sha256?: string | null;
            /**
             * Publication Commit
             * @default null
             */
            publication_commit?: string | null;
            /**
             * Release Released At
             * @default null
             */
            release_released_at?: string | null;
            /**
             * Release Version
             * @default null
             */
            release_version?: string | null;
            /**
             * Source Bundle Sha256
             * @default null
             */
            source_bundle_sha256?: string | null;
            /**
             * Source Path
             * @default null
             */
            source_path?: string | null;
        };
        /**
         * WaitReason
         * @description Typed reason codes for an effect that cannot be confirmed (the *unknown* kind).
         *
         *     Each code names the one fact the executor could not establish.  The free
         *     text of a report is for people; the Controller decides on this code.
         * @enum {string}
         */
        WaitReason: "operation-not-enabled" | "agent-upgrade-awaiting-identity" | "agent-restart-interrupted" | "stop-unconfirmed" | "cleanup-unconfirmed" | "stop-metadata-unconfirmed" | "retained-identity-mismatch" | "model-custody-unconfirmed" | "runtime-effect-unconfirmed" | "job-stop-unconfirmed" | "job-state-uncertain" | "lease-lapsed" | "report-uncertain" | "observation-unavailable" | "receipt-missing" | "stale-plan" | "scope-changed" | "legacy-unclassified";
        /**
         * WaitVerdict
         * @description The verdicts of the blocker allowlist for an operator wait.
         * @enum {string}
         */
        WaitVerdict: "KEEP" | "SELF-HEAL" | "FIX-ACTION" | "DERIVED";
        /** WorkerRuntimeObservation */
        WorkerRuntimeObservation: {
            /**
             * Completed At
             * Format: date-time
             */
            completed_at: string;
            /** Loop Sequence */
            loop_sequence: number | ExactNumber;
            /** Process Instance Id */
            process_instance_id: string;
            /** Source Sha */
            source_sha: string | null;
            /** Worker Contract Sha256 */
            worker_contract_sha256: string | null;
        };
        /** WritablePath */
        WritablePath: {
            /** Name */
            name: string;
            /** Path */
            path: string;
            /** Persistent */
            persistent: boolean;
        };
        pydantic__types__JsonValue: unknown;
        vonk_forge_contracts__recipe__JsonValue: string | (number | ExactNumber) | boolean | (number | ExactNumber) | components["schemas"]["vonk_forge_contracts__recipe__JsonValue"][] | {
            [key: string]: components["schemas"]["vonk_forge_contracts__recipe__JsonValue"];
        } | null;
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    getArtifactJobCapabilities: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobCapabilitiesResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getArtifactJobByRequestId: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                request_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getArtifactJobStatus: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    cancelArtifactJob: {
        parameters: {
            query?: never;
            header: {
                "X-Request-ID": string;
            };
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CancelRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    finalizeArtifactJob: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    uploadArtifactJobInput: {
        parameters: {
            query?: never;
            header?: {
                "X-Content-SHA256"?: string;
                "Content-Type"?: string;
                "Content-Length"?: number;
            };
            path: {
                job_id: string;
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/octet-stream": string;
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    downloadArtifactJobResult: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_id: string;
                name: string;
                sha256: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Artifact result byte stream */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "*/*": string;
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    submitArtifactJob: {
        parameters: {
            query?: never;
            header: {
                "X-Request-ID": string;
            };
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    downloadCliToken: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description A short-lived administrator bearer token download */
            200: {
                headers: {
                    /** @description When the token stops working (CliTokenDownload.expires_at) */
                    "X-Vonk-Token-Expires-At"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "text/plain": string;
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    loginBrowser: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LoginRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthSession"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Invalid login request */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LoginRequestInvalid"];
                };
            };
            /** @description Too Many Requests */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    logoutBrowser: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getBrowserSession: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthSession"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getManagedRecipeCatalogSyncStatus: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ManagedCatalogSyncResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CatalogProblem"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CatalogProblem"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CatalogProblem"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CatalogProblem"];
                };
            };
        };
    };
    getCliUpdateContract: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CliUpdateContract"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getFleetStatus: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/x-vonk-observation+ndjson": components["schemas"]["ObservationTransferRecord"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    enrollFleetNode: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FleetEnrollRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetActionResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getFleetEnrollment: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                grant_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EnrollmentGrantStatus"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    revokeFleetEnrollment: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                grant_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EnrollmentGrantStatus"] | components["schemas"]["EnrollmentObservationOutcome"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getFleetAdmissionLocks: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetLocksResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    streamFleetEvents: {
        parameters: {
            query?: never;
            header?: {
                /** @description Optional durable Fleet cursor; duplicate and numeric validity are checked from the raw header list. */
                "Last-Event-ID"?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Durable Fleet event stream. The schema describes the JSON data in each refresh notice, telemetry, or change SSE frame. A refresh notice requires a verified complete Fleet read. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": components["schemas"]["FleetStreamEvent"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    upgradeFleet: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FleetUpgradeRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetActionResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getFleetNode: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetNode"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getFleetLogInfo: {
        parameters: {
            query?: {
                since?: string | null;
                lines?: number;
                recipe?: string | null;
                source?: ("client" | "monitor" | "runtime" | "job") | null;
                follow?: boolean;
            };
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetLogResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    reenrollFleetNode: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FleetReenrollRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetActionResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    removeFleetNode: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetActionResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    renameFleetNode: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FleetRenameRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetNodeIdentity"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getJob: {
        parameters: {
            query?: {
                operation_cursor?: string | null;
                target_cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobDetailResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    resumeJob: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": components["schemas"]["JobResumeRequest"] | null;
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobResumeResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    listGatewayKeys: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GatewayKeyList"] | components["schemas"]["UnknownError"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    createGatewayKey: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["GatewayKeyCreateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GatewayKeyCreated"] | components["schemas"]["UnknownError"];
                };
            };
            /** @description Accepted */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UnknownError"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Bad Gateway */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    revokeGatewayKey: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GatewayKeyRevoked"] | components["schemas"]["UnknownError"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Bad Gateway */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    rollGatewayKey: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GatewayKeyCreated"] | components["schemas"]["UnknownError"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Bad Gateway */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    listModelLibrary: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
                usage?: string[] | null;
                family?: string[] | null;
                version?: string[] | null;
                quantization?: string[] | null;
                publisher?: string[] | null;
                alignment?: string[] | null;
                search?: string | null;
                updated_since?: string | null;
                sort?: "updated" | "name";
                cached?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelLibraryResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getModelOperation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCacheOperatorResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    cancelModelOperation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ModelCacheCancellationRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCacheOperatorResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getModelRequest: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                request_key: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCacheOperatorResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getModel: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelDetailResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    downloadModel: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ModelCacheOperatorRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCacheOperatorResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    removeModel: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ModelCacheRemovalRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCacheOperatorResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    reviewModelRemoval: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CacheRemovalReview"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    listOperations: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
                state?: string | null;
                node_id?: string | null;
                request_id?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OperationsResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getOperation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OperationDetailResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getOperationEvidence: {
        parameters: {
            query: {
                attempt: number;
            };
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FailureEvidenceBundle"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getPlatformObservation: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/x-vonk-observation+ndjson": components["schemas"]["ObservationTransferRecord"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    listProfiles: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileList"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getProfileApplication: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                application_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileApplicationView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    cancelProfileApplication: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                application_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FleetProfileApplicationCancelRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileApplicationView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getProfileApplicationCancellation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                application_id: string;
                request_key: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileApplicationView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getProfile: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                number: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileView"] | components["schemas"]["UnavailableFleetProfileView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    autosaveProfile: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                number: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FleetProfileInput"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getProfileDefinition: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                number: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileDefinitionView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getProfileEndpoints: {
        parameters: {
            query?: {
                alias?: string | null;
            };
            header?: never;
            path: {
                number: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileEndpointsView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    loadProfile: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                number: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FleetProfileLoadRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileApplicationView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    previewProfile: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                number: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfilePreview"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getProfileProgress: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                number: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileApplicationView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getProfileApplicationByRequest: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                number: number;
                request_key: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetProfileApplicationView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    applyRecipeInstallationReconciliation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                installation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InstallationReconcileRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunSwitchOperation"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    previewRecipeInstallationReconciliation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                installation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunSwitchPlan"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    listRecipeLibrary: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
                model?: string[] | null;
                cached?: boolean;
                ready?: boolean | null;
                fits_fleet?: boolean | null;
                assess?: boolean;
                usage?: string[] | null;
                publisher?: string[] | null;
                alignment?: string[] | null;
                sparks?: (number | ExactNumber)[] | null;
                engine?: string[] | null;
                creator?: string[] | null;
                search?: string | null;
                updated_since?: string | null;
                sort?: "updated" | "name";
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeLibraryResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getRecipeOperation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeImageAvailabilityResponse"] | components["schemas"]["RecipeOperatorResponse"] | components["schemas"]["RecipeUpdateResponse"] | components["schemas"]["RecipeRemovalUnavailableView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    cancelRecipeOperation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RecipeCancellationRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeImageAvailabilityResponse"] | components["schemas"]["RecipeOperatorResponse"] | components["schemas"]["RecipeUpdateResponse"] | components["schemas"]["RecipeRemovalUnavailableView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    retryRecipeOperation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RecipeDownloadRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeImageAvailabilityResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getRecipeRequest: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                request_key: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeImageAvailabilityResponse"] | components["schemas"]["RecipeOperatorResponse"] | components["schemas"]["RecipeUpdateResponse"] | components["schemas"]["RecipeRemovalUnavailableView"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    listArtifactJobsForRun: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobListResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    createArtifactJob: {
        parameters: {
            query?: never;
            header: {
                "X-Request-ID": string;
            };
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ArtifactJobCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactJobResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    updateRecipes: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RecipeUpdateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeUpdateResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getRecipe: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeDetailResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    downloadRecipe: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RecipeDownloadRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeImageAvailabilityResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    removeRecipe: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RecipeOperatorRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RecipeOperatorResponse"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    reviewRecipeRemoval: {
        parameters: {
            query: {
                with_model: boolean;
            };
            header?: never;
            path: {
                selector: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CacheRemovalReview"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
    getRunSwitchOperation: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunSwitchOperation"];
                };
            };
            /** @description Unauthorized */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"];
                };
            };
            /** @description Unprocessable Content */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RequestValidationProblem"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoundedErrorResponse"] | components["schemas"]["CapabilityUnavailableReply"];
                };
            };
        };
    };
}
