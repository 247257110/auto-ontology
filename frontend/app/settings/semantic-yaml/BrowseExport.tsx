// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { datasources } from '@/api/datasources';
import { semanticYamlApi } from '@/api/semanticYaml';
import { Button } from '@/common/Button';
import { Icon, IconName } from '@/common/icons';
import { SearchInput } from '@/common/SearchInput';
import { SkeletonRows } from '@/common/Skeleton';
import { Toast } from '@/common/Toast';
import { ButtonTheme, Size } from '@/enums/button';
import { ToastVariant } from '@/enums/toast';
import type { Database } from '@/types/datasources';
import type {
	ConsistencyFinding,
	ConsistencySeverity,
	ModelConsistency,
	OssieDocument,
	OssieDocumentKey,
} from '@/types/semanticYaml';

/**
 * Browse the Apache Ossie export and, more importantly, what it does not say.
 *
 * Read-only on purpose. The Ossie conversion is one-way — editing a
 * `description` here is silently dropped on import (the native document travels
 * in `custom_extensions` and wins), so a page that let you edit would be a page
 * that lies. What it can do is show the defects a reader cannot see: a
 * relationship naming a field that is not there, two metrics sharing a name, a
 * term narrowed to one table, and whole object types Ossie has no place for.
 */

const DOCUMENT_LABEL: Record<OssieDocumentKey, string> = {
	semantic_model: 'Semantic model',
	ontology: 'Ontology',
	mapping: 'Mapping',
};

const DOCUMENT_HINT: Record<OssieDocumentKey, string> = {
	semantic_model: 'Datasets, their fields, the relationships between them, and metrics.',
	ontology: 'The glossary terms as Ossie concepts.',
	mapping: 'How the ontology concepts populate from the semantic model.',
};

const SEVERITY_LABEL: Record<ConsistencySeverity, string> = {
	error: 'Error',
	warning: 'Warning',
	info: 'Note',
};

const SEVERITY_ORDER: ConsistencySeverity[] = ['error', 'warning', 'info'];

const SEVERITY_STYLE: Record<ConsistencySeverity, string> = {
	error: 'border-red-300 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300',
	warning:
		'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300',
	info: 'border-sky-300 bg-sky-50 text-sky-800 dark:border-sky-900 dark:bg-sky-950/40 dark:text-sky-300',
};

const SEVERITY_ICON: Record<ConsistencySeverity, IconName> = {
	error: IconName.Lightning,
	warning: IconName.Certification,
	info: IconName.Filter,
};

const downloadText = (text: string, filename: string) => {
	const url = URL.createObjectURL(new Blob([text], { type: 'application/x-yaml' }));
	const link = document.createElement('a');
	link.href = url;
	link.download = filename;
	document.body.appendChild(link);
	link.click();
	link.remove();
	URL.revokeObjectURL(url);
};

/** Splits one line on the query so matches can be marked without innerHTML. */
const Marked = ({ line, query }: { line: string; query: string }) => {
	if (!query) return <>{line}</>;
	const needle = query.toLowerCase();
	const haystack = line.toLowerCase();
	const parts: { text: string; hit: boolean }[] = [];
	let cursor = 0;
	let index = haystack.indexOf(needle);
	while (index !== -1) {
		if (index > cursor) parts.push({ text: line.slice(cursor, index), hit: false });
		parts.push({ text: line.slice(index, index + query.length), hit: true });
		cursor = index + query.length;
		index = haystack.indexOf(needle, cursor);
	}
	if (cursor < line.length) parts.push({ text: line.slice(cursor), hit: false });
	return (
		<>
			{parts.map((part, position) =>
				part.hit ? (
					<mark
						key={position}
						className="rounded bg-[#76b900]/30 text-inherit dark:bg-[#76b900]/40"
					>
						{part.text}
					</mark>
				) : (
					<span key={position}>{part.text}</span>
				),
			)}
		</>
	);
};

const DocumentPane = ({
	doc,
	search,
	focusLine,
}: {
	doc: OssieDocument;
	search: string;
	focusLine: number | null;
}) => {
	const scrollRef = useRef<HTMLDivElement | null>(null);
	const lines = useMemo(() => doc.text.split('\n'), [doc.text]);
	const matches = useMemo(() => {
		if (!search) return 0;
		const needle = search.toLowerCase();
		return lines.reduce(
			(total, line) => total + (line.toLowerCase().includes(needle) ? 1 : 0),
			0,
		);
	}, [lines, search]);

	// Scroll the line a finding points at into view. The gutter and the code
	// share one scroll container, so only the container needs to move.
	useEffect(() => {
		if (focusLine == null) return;
		const container = scrollRef.current;
		if (!container) return;
		const row = container.querySelector<HTMLElement>(`[data-line="${focusLine}"]`);
		row?.scrollIntoView({ block: 'center' });
	}, [focusLine]);

	return (
		<div className="flex min-h-0 flex-1 flex-col gap-2">
			<div className="flex flex-wrap items-center justify-between gap-2">
				<p className="text-xs text-secondary dark:text-zinc-400">
					{lines.length.toLocaleString()} lines
					{search ? ` · ${matches} matching line(s)` : ''}
				</p>
				<div className="flex items-center gap-2">
					<Button
						theme={ButtonTheme.Secondary}
						size={Size.SMALL}
						iconPosition="left"
						onClick={() => void navigator.clipboard?.writeText(doc.text)}
					>
						<Icon name={IconName.Copy} className="h-3.5 w-3.5" />
						Copy
					</Button>
					<Button
						theme={ButtonTheme.Secondary}
						size={Size.SMALL}
						iconPosition="left"
						onClick={() => downloadText(doc.text, doc.filename)}
					>
						<Icon name={IconName.Download} className="h-3.5 w-3.5" />
						Download
					</Button>
				</div>
			</div>
			<div
				ref={scrollRef}
				className="min-h-0 flex-1 overflow-auto rounded-lg border border-zinc-200 bg-white font-mono text-xs leading-5 dark:border-zinc-700 dark:bg-zinc-950"
			>
				{lines.map((line, index) => {
					const number = index + 1;
					const highlighted = number === focusLine;
					return (
						<div
							key={number}
							data-line={number}
							className={`flex whitespace-pre ${
								highlighted ? 'bg-[#76b900]/15' : undefined
							}`}
						>
							<span
								aria-hidden
								className="w-14 shrink-0 select-none border-r border-zinc-200 px-2 text-right text-zinc-400 dark:border-zinc-800 dark:text-zinc-600"
							>
								{number}
							</span>
							<span className="px-3 text-body dark:text-zinc-200">
								<Marked line={line} query={search} />
							</span>
						</div>
					);
				})}
			</div>
		</div>
	);
};

const FindingRow = ({
	finding,
	onJump,
}: {
	finding: ConsistencyFinding;
	onJump: (line: number) => void;
}) => (
	<li className="flex items-start gap-3 px-3 py-2.5">
		<span
			className={`mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium ${SEVERITY_STYLE[finding.severity]}`}
		>
			<Icon name={SEVERITY_ICON[finding.severity]} className="h-3 w-3" />
			{SEVERITY_LABEL[finding.severity]}
		</span>
		<div className="min-w-0 flex-1">
			<p className="text-sm text-body dark:text-zinc-200">{finding.message}</p>
			<p className="mt-0.5 font-mono text-[11px] text-secondary dark:text-zinc-500">
				{finding.code}
				{finding.subject ? ` · ${finding.subject}` : ''}
			</p>
		</div>
		{finding.line != null ? (
			<Button
				theme={ButtonTheme.Minimal}
				size={Size.SMALL}
				onClick={() => onJump(finding.line!)}
			>
				Line {finding.line}
			</Button>
		) : null}
	</li>
);

export const BrowseExport = () => {
	const [databases, setDatabases] = useState<Database[]>([]);
	const [databasesLoading, setDatabasesLoading] = useState(true);
	const [databasesError, setDatabasesError] = useState<string | null>(null);
	const [selectedDbIds, setSelectedDbIds] = useState<Set<string>>(new Set());

	const [consistency, setConsistency] = useState<ModelConsistency | null>(null);
	const [checking, setChecking] = useState(false);
	const [checkError, setCheckError] = useState<string | null>(null);

	const [documents, setDocuments] = useState<OssieDocument[]>([]);
	const [loadingDocs, setLoadingDocs] = useState(false);
	const [docsError, setDocsError] = useState<string | null>(null);
	const [activeDocument, setActiveDocument] = useState<OssieDocumentKey>('semantic_model');
	const [search, setSearch] = useState('');
	const [focusLine, setFocusLine] = useState<number | null>(null);

	useEffect(() => {
		let active = true;
		void (async () => {
			setDatabasesLoading(true);
			const res = await datasources.getDBs();
			if (!active) return;
			if (res.error) {
				setDatabasesError(res.message ?? 'Failed to load databases.');
				setDatabases([]);
			} else {
				const data = res.data ?? [];
				setDatabasesError(null);
				setDatabases(data);
				setSelectedDbIds(new Set(data.map((db) => db.id)));
			}
			setDatabasesLoading(false);
		})();
		return () => {
			active = false;
		};
	}, []);

	const selectedIds = useMemo(() => Array.from(selectedDbIds), [selectedDbIds]);

	const toggleDatabase = (id: string) => {
		setSelectedDbIds((current) => {
			const next = new Set(current);
			if (next.has(id)) next.delete(id);
			else next.add(id);
			return next;
		});
	};

	const runConsistencyCheck = useCallback(async () => {
		setChecking(true);
		setCheckError(null);
		const res = await semanticYamlApi.checkModelConsistency(selectedIds);
		setChecking(false);
		if (res.error || !res.shape) {
			setCheckError(res.message ?? 'The consistency check failed.');
			setConsistency(null);
			return;
		}
		setConsistency(res);
	}, [selectedIds]);

	const loadDocuments = useCallback(async () => {
		setLoadingDocs(true);
		setDocsError(null);
		const res = await semanticYamlApi.exportOssieDocuments(selectedIds);
		setLoadingDocs(false);
		if (res.error || res.documents.length === 0) {
			setDocsError(res.message ?? 'The export did not contain any documents.');
			setDocuments([]);
			return;
		}
		setDocuments(res.documents);
		setActiveDocument((current) =>
			res.documents.some((document) => document.key === current)
				? current
				: res.documents[0].key,
		);
	}, [selectedIds]);

	/** Both halves in one action, because neither alone answers the question. */
	const loadEverything = useCallback(async () => {
		await Promise.all([runConsistencyCheck(), loadDocuments()]);
	}, [runConsistencyCheck, loadDocuments]);

	const activeDoc = documents.find((item) => item.key === activeDocument) ?? null;

	/**
	 * A finding's line is a position in the *checked* document, which is the
	 * semantic model Ossie text. Jumping therefore selects that tab first —
	 * otherwise the highlight would land in whatever tab happened to be open.
	 */
	const jumpToLine = useCallback((line: number) => {
		setActiveDocument('semantic_model');
		setFocusLine(line);
	}, []);

	if (databasesLoading) {
		return <SkeletonRows rows={4} />;
	}

	return (
		<div className="flex flex-col gap-5">
			{databasesError ? (
				<p className="text-sm text-red-600 dark:text-red-400">{databasesError}</p>
			) : null}

			{databases.length === 0 && !databasesError ? (
				<p className="text-sm text-secondary dark:text-zinc-400">
					No databases are connected yet, so there is nothing to export.
				</p>
			) : null}

			<section className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
				<div className="flex flex-wrap items-center justify-between gap-3">
					<div className="flex flex-wrap items-center gap-3">
						<span className="text-sm font-medium text-heading dark:text-zinc-200">
							Databases
						</span>
						{databases.map((database) => (
							<label
								key={database.id}
								className="flex cursor-pointer items-center gap-1.5 text-sm text-body dark:text-zinc-300"
							>
								<input
									type="checkbox"
									className="h-4 w-4 rounded border-zinc-300 text-[#76b900] focus:ring-[#76b900]/30"
									checked={selectedDbIds.has(database.id)}
									onChange={() => toggleDatabase(database.id)}
								/>
								{database.name}
							</label>
						))}
					</div>
					<div className="flex items-center gap-2">
						<Button
							theme={ButtonTheme.Secondary}
							size={Size.REGULAR}
							loading={checking}
							disabled={checking || loadingDocs || selectedIds.length === 0}
							onClick={() => void runConsistencyCheck()}
						>
							Consistency check
						</Button>
						<Button
							theme={ButtonTheme.Primary}
							size={Size.REGULAR}
							loading={loadingDocs}
							disabled={checking || loadingDocs || selectedIds.length === 0}
							onClick={() => void loadEverything()}
						>
							Load export
						</Button>
					</div>
				</div>
				{selectedIds.length === 0 && databases.length > 0 ? (
					<p className="text-xs text-secondary dark:text-zinc-400">
						Select at least one database.
					</p>
				) : null}
			</section>

			{checkError ? (
				<Toast
					open
					variant={ToastVariant.Error}
					title="Consistency check failed"
					message={checkError}
					onClose={() => setCheckError(null)}
				/>
			) : null}
			{docsError ? (
				<Toast
					open
					variant={ToastVariant.Error}
					title="Export failed"
					message={docsError}
					onClose={() => setDocsError(null)}
				/>
			) : null}

			{consistency ? (
				<section className="flex flex-col rounded-xl border border-zinc-200 bg-white dark:border-zinc-700 dark:bg-zinc-900">
					<div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-700">
						<div className="flex items-center gap-2">
							<span className="text-sm font-medium text-heading dark:text-zinc-200">
								Consistency
							</span>
							{SEVERITY_ORDER.map((severity) => (
								<span
									key={severity}
									className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium ${SEVERITY_STYLE[severity]}`}
								>
									{consistency.counts[severity]}{' '}
									{SEVERITY_LABEL[severity].toLowerCase()}
								</span>
							))}
						</div>
						<p className="text-xs text-secondary dark:text-zinc-400">
							Checked {consistency.shape.datasets} datasets ·{' '}
							{consistency.shape.fields} fields · {consistency.shape.relationships}{' '}
							relationships · {consistency.shape.metrics} metrics
						</p>
					</div>
					{consistency.findings.length === 0 ? (
						<p className="px-4 py-4 text-sm text-secondary dark:text-zinc-400">
							Nothing to report: every relationship resolves to a field that exists,
							no metric names collide, and no term was narrowed away from a table.
						</p>
					) : (
						<>
							<p className="px-4 pt-3 text-xs text-secondary dark:text-zinc-400">
								Every item below renders as an ordinary document. Reading the file
								top to bottom does not reveal any of them.
							</p>
							<ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
								{consistency.findings.map((finding, index) => (
									<FindingRow
										key={`${finding.code}-${finding.subject}-${index}`}
										finding={finding}
										onJump={jumpToLine}
									/>
								))}
							</ul>
						</>
					)}
				</section>
			) : null}

			<section className="flex min-h-[32rem] flex-col gap-3 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
				{documents.length === 0 ? (
					<p className="text-sm text-secondary dark:text-zinc-400">
						{loadingDocs
							? 'Exporting…'
							: 'Press “Load export” to read the semantic model, ontology and mapping an Apache Ossie export produces.'}
					</p>
				) : (
					<>
						<div className="flex flex-wrap items-center justify-between gap-3">
							<div
								role="tablist"
								aria-label="Export documents"
								className="flex flex-wrap gap-1"
							>
								{documents.map((item) => (
									<button
										key={item.key}
										role="tab"
										type="button"
										aria-selected={item.key === activeDocument}
										onClick={() => setActiveDocument(item.key)}
										className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
											item.key === activeDocument
												? 'bg-[#76b900]/15 font-medium text-heading ring-1 ring-[#76b900]/30 dark:text-zinc-100'
												: 'text-secondary hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800'
										}`}
									>
										{DOCUMENT_LABEL[item.key]}
									</button>
								))}
							</div>
							<SearchInput
								value={search}
								onChange={(value) => {
									setSearch(value);
									setFocusLine(null);
								}}
								placeholder="Find in document"
								className="w-full sm:w-72"
								aria-label="Find in document"
							/>
						</div>
						{activeDoc ? (
							<>
								<p className="text-xs text-secondary dark:text-zinc-400">
									<code className="font-mono">{activeDoc.filename}</code> —{' '}
									{DOCUMENT_HINT[activeDoc.key]}
								</p>
								<DocumentPane
									doc={activeDoc}
									search={search}
									focusLine={focusLine}
								/>
							</>
						) : null}
					</>
				)}
			</section>
		</div>
	);
};
