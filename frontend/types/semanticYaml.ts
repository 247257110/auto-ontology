// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Shapes for the semantic-YAML workbench (Settings → Semantic YAML).
 *
 * `ModelConsistency` mirrors `POST /api/model/consistency` exactly. The backend
 * computes it from the *exported Ossie text* plus the native document, because
 * half of what it reports (a term narrowed to one table, whole object types
 * Ossie cannot hold) only exists in the native model — the Ossie file is what
 * is left after those are dropped.
 */

/** Severity of a consistency finding. Only `error` blocks a review. */
export type ConsistencySeverity = 'error' | 'warning' | 'info';

/** Stable identifier, so the UI can group without matching on message text. */
export type ConsistencyCode =
	| 'unparseable'
	| 'relationship_dangling_dataset'
	| 'relationship_dangling_field'
	| 'duplicate_metric_name'
	| 'dataset_without_fields'
	| 'term_projected_onto_one_table'
	| 'not_expressible_in_ossie'
	| 'not_exportable';

export type ConsistencyFinding = {
	severity: ConsistencySeverity;
	code: ConsistencyCode;
	/** What the finding is about: a relationship, dataset, term or metric name. */
	subject: string;
	/** One sentence, already written in the reader's language. */
	message: string;
	/**
	 * Approximate 1-based line of the subject in the shipped Ossie document, for
	 * a "jump to it" affordance. Absent when the subject could not be located —
	 * a limitation of text scanning, not a weaker finding.
	 */
	line: number | null;
	detail: Record<string, unknown> | null;
};

/** What was checked, so an empty finding list reads as "checked 62 datasets". */
export type ConsistencyShape = {
	datasets: number;
	fields: number;
	relationships: number;
	metrics: number;
};

export type ConsistencyCounts = {
	error: number;
	warning: number;
	info: number;
};

export type ModelConsistency = {
	/** No errors. Warnings and notes are informational. */
	ok: boolean;
	counts: ConsistencyCounts;
	shape: ConsistencyShape;
	findings: ConsistencyFinding[];
};

/** One of the three documents an Ossie export ships. */
export type OssieDocumentKey = 'semantic_model' | 'ontology' | 'mapping';

export type OssieDocument = {
	key: OssieDocumentKey;
	/** File name inside the zip, shown as the tab label. */
	filename: string;
	text: string;
};

/** Line-oriented view of one document: enough for a number gutter and search. */
export type DocumentView = {
	document: OssieDocument;
	lines: string[];
};

/** One semantic label the reindex can rebuild. */
export type SemanticIndexLabel =
	| 'Term'
	| 'ColumnAttribute'
	| 'SqlAttribute'
	| 'CustomAnalysis'
	| 'PqlAnalysis';

/**
 * Receipt for `POST /api/semantic-index/reindex`.
 *
 * `dry_run` returns only the first three fields. A real run that could not
 * proceed sets `skipped` and says why — it never reports success, which is the
 * failure this field exists to make impossible to miss.
 */
export type ReindexReport = {
	dry_run: boolean;
	/** How many embedded rows a run would write, per label. */
	labels: Partial<Record<SemanticIndexLabel | string, number>>;
	total: number;
	elapsed_ms: number;
	skipped?: boolean;
	reason?: string;
	written?: number;
	deleted_node_ids?: number;
	rows_before?: number;
	rows_after?: number;
};
