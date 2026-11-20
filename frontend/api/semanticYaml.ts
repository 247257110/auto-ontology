// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { unzipSync } from 'fflate';

import { requests } from './requests';
import type { ResponseWithError } from './types';
import { ModelFormat } from '@/enums/modelInterchange';
import type {
	ConsistencyShape,
	ModelConsistency,
	OssieDocument,
	OssieDocumentKey,
	ReindexReport,
} from '@/types/semanticYaml';

/**
 * The three documents `POST /model/export?include_ontology=true` puts in the zip.
 * Anything else the archive grows later is ignored rather than shown under a
 * made-up name — a wrong label on a document is worse than not listing it.
 */
const OSSIE_FILE_KEYS: Record<string, OssieDocumentKey> = {
	'semantic_model.yaml': 'semantic_model',
	'ontology.yaml': 'ontology',
	'mapping.yaml': 'mapping',
};

const DOCUMENT_ORDER: OssieDocumentKey[] = ['semantic_model', 'ontology', 'mapping'];

const textDecoder = new TextDecoder();

const readZip = async (blob: Blob): Promise<OssieDocument[]> => {
	const entries = unzipSync(new Uint8Array(await blob.arrayBuffer()));
	const decoded = new Map<OssieDocumentKey, OssieDocument>();
	for (const [path, bytes] of Object.entries(entries)) {
		// The converter may nest the files in a directory.
		const filename = path.split('/').pop() ?? path;
		const key = OSSIE_FILE_KEYS[filename];
		if (!key) continue;
		decoded.set(key, {
			key,
			filename,
			text: textDecoder.decode(bytes),
		});
	}
	return DOCUMENT_ORDER.map((key) => decoded.get(key)).filter(
		(document): document is OssieDocument => document != null,
	);
};

export type OssieBundle = {
	documents: OssieDocument[];
	/**
	 * Shape reported by the consistency check, when it was called. The document
	 * tabs show counts from the check rather than parsing YAML in the browser.
	 */
	shape?: ConsistencyShape;
};

/**
 * The semantic model plus the two documents that only exist in the zip form. The
 * single-file export cannot produce the ontology and mapping, so this always
 * asks for the archive.
 */
export const exportOssieDocuments = async (
	databaseIds: string[],
): Promise<ResponseWithError<OssieBundle>> => {
	const res = await requests.postBlob('model/export', {
		databases: databaseIds,
		format: ModelFormat.OSSIE,
		include_ontology: true,
	});
	if (res.error || !res.blob) {
		return { documents: [], error: true, message: res.message };
	}
	try {
		const documents = await readZip(res.blob);
		if (documents.length === 0) {
			return {
				documents: [],
				error: true,
				message: 'The export archive did not contain any YAML documents.',
			};
		}
		return { documents };
	} catch (err) {
		return {
			documents: [],
			error: true,
			message: err instanceof Error ? err.message : 'Could not read the export archive.',
		};
	}
};

/**
 * What the exported Ossie document would hide from a reader: dangling
 * relationship fields, duplicate metric names, terms narrowed to one table, and
 * the object types Ossie has no place for. This is the half of browsing the file
 * that reading it top to bottom cannot give you.
 */
export const checkModelConsistency = (
	databaseIds: string[],
): Promise<ResponseWithError<ModelConsistency>> =>
	requests.post<ModelConsistency>('model/consistency', {
		databases: databaseIds,
		format: ModelFormat.OSSIE,
		include_ontology: false,
	});

/**
 * Rebuild the semantic vector index, so an edit's new description actually
 * reaches retrieval.
 *
 * `dryRun` writes nothing and reports how many rows would be rewritten per
 * label; a real run returns the before/after row counts, which are the check
 * that a rebuild neither lost nor duplicated a vector. A run that could not
 * proceed comes back with `skipped` and a reason rather than a success shape.
 */
export const reindexSemanticIndex = (
	options: { databases?: string[]; dryRun: boolean; labels?: string[] } = { dryRun: true },
): Promise<ResponseWithError<ReindexReport>> =>
	requests.post<ReindexReport>('semantic-index/reindex', {
		databases: options.databases,
		labels: options.labels,
		dry_run: options.dryRun,
	});

export const semanticYamlApi = {
	exportOssieDocuments,
	checkModelConsistency,
	reindexSemanticIndex,
};
