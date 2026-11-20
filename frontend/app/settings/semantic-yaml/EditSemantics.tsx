// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { analyses } from '@/api/analyses';
import { semanticYamlApi } from '@/api/semanticYaml';
import { termsApi } from '@/api/terms';
import { Button } from '@/common/Button';
import { Icon, IconName } from '@/common/icons';
import { SearchInput } from '@/common/SearchInput';
import { SkeletonRows } from '@/common/Skeleton';
import { Spinner } from '@/common/Spinner';
import { ButtonTheme, Size } from '@/enums/button';
import type { CustomAnalysis } from '@/types/analysis';
import type { ReindexReport } from '@/types/semanticYaml';
import type { ColumnAttribute, Term } from '@/types/terms';

/**
 * Edit the semantic objects where they actually live.
 *
 * The truth is the catalog, not the Ossie file: the Ossie conversion is one-way
 * (a column description edited there is discarded on import, because the native
 * document travels in `custom_extensions` and wins), so an editing UI built on
 * the YAML would be a UI that silently loses work. Everything here writes
 * through the catalog's own PATCH routes, which re-embed the affected rows as
 * part of the write.
 *
 * The panel says what happened rather than what was intended: a save reports
 * the vector work the backend performs, and the rebuild panel reports the
 * before/after row counts, which are the only evidence that a rebuild neither
 * lost nor duplicated a vector.
 */

type Mode = 'terms' | 'analyses';

const fieldClassName =
	'w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-body transition-colors focus:border-[#76b900] focus:outline-none focus:ring-2 focus:ring-[#76b900]/30 dark:border-zinc-600 dark:bg-zinc-900 dark:text-zinc-100';

const textareaClassName = `${fieldClassName} font-mono leading-5`;

const listRowClassName = (selected: boolean) =>
	`w-full cursor-pointer rounded-lg px-3 py-2 text-left transition-colors ${
		selected
			? 'bg-[#76b900]/15 ring-1 ring-[#76b900]/30'
			: 'hover:bg-zinc-100 dark:hover:bg-zinc-800'
	}`;

/** A save's receipt, so the panel never implies more than the API confirmed. */
type SaveState = {
	kind: 'idle' | 'saving' | 'saved' | 'error';
	message?: string;
};

const SaveNote = ({ state }: { state: SaveState }) => {
	if (state.kind === 'idle') return null;
	if (state.kind === 'saving') {
		return (
			<span className="inline-flex items-center gap-1.5 text-xs text-secondary dark:text-zinc-400">
				<Spinner className="h-3.5 w-3.5" />
				Saving…
			</span>
		);
	}
	return (
		<span
			className={`text-xs ${
				state.kind === 'error'
					? 'text-red-600 dark:text-red-400'
					: 'text-emerald-700 dark:text-emerald-400'
			}`}
		>
			{state.message}
		</span>
	);
};

const TermEditor = ({ term, onUpdated }: { term: Term; onUpdated: (term: Term) => void }) => {
	const [name, setName] = useState(term.name);
	const [description, setDescription] = useState(term.description ?? '');
	const [synonyms, setSynonyms] = useState(term.synonyms.join(', '));
	const [termSave, setTermSave] = useState<SaveState>({ kind: 'idle' });

	const [attributes, setAttributes] = useState<ColumnAttribute[]>([]);
	const [attributesLoading, setAttributesLoading] = useState(true);
	const [attributeDrafts, setAttributeDrafts] = useState<Record<string, string>>({});
	const [attributeSaves, setAttributeSaves] = useState<Record<string, SaveState>>({});

	useEffect(() => {
		let active = true;
		void (async () => {
			setAttributesLoading(true);
			const res = await termsApi.getColumnAttributes(term.id, { limit: 100 });
			if (!active) return;
			const rows = res.error ? [] : (res.data ?? []);
			setAttributes(rows);
			setAttributeDrafts(
				Object.fromEntries(rows.map((row) => [row.id, row.description ?? ''])),
			);
			setAttributeSaves({});
			setAttributesLoading(false);
		})();
		return () => {
			active = false;
		};
	}, [term.id]);

	const synonymsChanged = useMemo(
		() =>
			synonyms
				.split(',')
				.map((item) => item.trim())
				.filter(Boolean)
				.join('|') !== term.synonyms.join('|'),
		[synonyms, term.synonyms],
	);

	const descriptionChanged = description !== (term.description ?? '');
	const nameChanged = name !== term.name;
	const dirty = nameChanged || descriptionChanged || synonymsChanged;

	const saveTerm = useCallback(async () => {
		setTermSave({ kind: 'saving' });
		const res = await termsApi.update(term.id, {
			...(nameChanged ? { name } : {}),
			...(descriptionChanged ? { description } : {}),
			...(synonymsChanged
				? {
						synonyms: synonyms
							.split(',')
							.map((item) => item.trim())
							.filter(Boolean),
					}
				: {}),
		});
		if (res.error || !res.data) {
			setTermSave({ kind: 'error', message: res.message ?? 'The save failed.' });
			return;
		}
		setTermSave({
			kind: 'saved',
			message: nameChanged
				? 'Saved. The term and its column attributes were re-embedded.'
				: 'Saved. The term was re-embedded.',
		});
		onUpdated({
			...term,
			name: res.data.name,
			description: res.data.description,
			name_certified: res.data.name_certified,
			description_certified: res.data.description_certified,
			certification: res.data.certification,
			synonyms: synonymsChanged
				? synonyms
						.split(',')
						.map((item) => item.trim())
						.filter(Boolean)
				: term.synonyms,
		});
	}, [
		description,
		descriptionChanged,
		name,
		nameChanged,
		onUpdated,
		synonyms,
		synonymsChanged,
		term,
	]);

	const saveAttribute = useCallback(
		async (attribute: ColumnAttribute) => {
			const next = attributeDrafts[attribute.id] ?? '';
			if (next === (attribute.description ?? '')) return;
			setAttributeSaves((current) => ({
				...current,
				[attribute.id]: { kind: 'saving' },
			}));
			const res = await termsApi.updateColumnAttribute(term.id, attribute.id, {
				description: next,
			});
			if (res.error || !res.data) {
				setAttributeSaves((current) => ({
					...current,
					[attribute.id]: {
						kind: 'error',
						message: res.message ?? 'The save failed.',
					},
				}));
				return;
			}
			setAttributes((current) =>
				current.map((row) =>
					row.id === attribute.id
						? {
								...row,
								description: res.data.description,
								certified: res.data.certified,
							}
						: row,
				),
			);
			setAttributeSaves((current) => ({
				...current,
				[attribute.id]: { kind: 'saved', message: 'Saved and re-embedded.' },
			}));
		},
		[attributeDrafts, term.id],
	);

	return (
		<div className="flex min-h-0 flex-col gap-5">
			<div className="flex flex-col gap-3">
				<label className="flex flex-col gap-1">
					<span className="text-xs font-medium text-heading dark:text-zinc-300">
						Name
					</span>
					<input
						className={fieldClassName}
						value={name}
						onChange={(event) => setName(event.target.value)}
					/>
				</label>
				<label className="flex flex-col gap-1">
					<span className="text-xs font-medium text-heading dark:text-zinc-300">
						Description
					</span>
					<textarea
						className={textareaClassName}
						rows={5}
						value={description}
						onChange={(event) => setDescription(event.target.value)}
					/>
					<span className="text-[11px] text-secondary dark:text-zinc-500">
						This text is what retrieval matches a question against — it is embedded, and
						editing it changes what the term answers to.
					</span>
				</label>
				<label className="flex flex-col gap-1">
					<span className="text-xs font-medium text-heading dark:text-zinc-300">
						Synonyms
					</span>
					<input
						className={fieldClassName}
						value={synonyms}
						onChange={(event) => setSynonyms(event.target.value)}
						placeholder="Comma separated"
					/>
				</label>
				<div className="flex items-center gap-3">
					<Button
						theme={ButtonTheme.Primary}
						size={Size.REGULAR}
						disabled={!dirty || termSave.kind === 'saving'}
						loading={termSave.kind === 'saving'}
						onClick={() => void saveTerm()}
					>
						Save term
					</Button>
					<SaveNote state={termSave} />
				</div>
			</div>

			<div className="flex flex-col gap-2 border-t border-zinc-200 pt-4 dark:border-zinc-700">
				<span className="text-xs font-medium text-heading dark:text-zinc-300">
					Column attributes ({attributes.length})
				</span>
				{attributesLoading ? (
					<SkeletonRows rows={3} />
				) : attributes.length === 0 ? (
					<p className="text-xs text-secondary dark:text-zinc-400">
						This term has no column attributes yet.
					</p>
				) : (
					<ul className="flex flex-col gap-3">
						{attributes.map((attribute) => {
							const draft = attributeDrafts[attribute.id] ?? '';
							const changed = draft !== (attribute.description ?? '');
							return (
								<li key={attribute.id} className="flex flex-col gap-1.5">
									<div className="flex flex-wrap items-center gap-2">
										<span className="font-mono text-xs text-body dark:text-zinc-300">
											{attribute.name}
										</span>
										<span className="text-[11px] text-secondary dark:text-zinc-500">
											{attribute.source_column}
											{attribute.datatype ? ` · ${attribute.datatype}` : ''}
										</span>
									</div>
									<textarea
										className={fieldClassName}
										rows={2}
										value={draft}
										onChange={(event) =>
											setAttributeDrafts((current) => ({
												...current,
												[attribute.id]: event.target.value,
											}))
										}
									/>
									<div className="flex items-center gap-3">
										<Button
											theme={ButtonTheme.Secondary}
											size={Size.SMALL}
											disabled={
												!changed ||
												attributeSaves[attribute.id]?.kind === 'saving'
											}
											loading={
												attributeSaves[attribute.id]?.kind === 'saving'
											}
											onClick={() => void saveAttribute(attribute)}
										>
											Save
										</Button>
										<SaveNote
											state={attributeSaves[attribute.id] ?? { kind: 'idle' }}
										/>
									</div>
								</li>
							);
						})}
					</ul>
				)}
			</div>
		</div>
	);
};

const AnalysisEditor = ({
	analysis,
	onUpdated,
}: {
	analysis: CustomAnalysis;
	onUpdated: (analysis: CustomAnalysis) => void;
}) => {
	const [name, setName] = useState(analysis.name);
	const [description, setDescription] = useState(analysis.description);
	const [sql, setSql] = useState(analysis.sql);
	const [save, setSave] = useState<SaveState>({ kind: 'idle' });
	const [validating, setValidating] = useState(false);

	const dirty =
		name !== analysis.name || description !== analysis.description || sql !== analysis.sql;

	const validate = useCallback(async () => {
		setValidating(true);
		const res = await analyses.validate(sql);
		setValidating(false);
		setSave(
			res.error
				? { kind: 'error', message: res.message ?? 'The SQL did not validate.' }
				: { kind: 'saved', message: 'SQL resolves against the catalog.' },
		);
	}, [sql]);

	const saveAnalysis = useCallback(async () => {
		setSave({ kind: 'saving' });
		const res = await analyses.update(analysis.id, { name, description, sql });
		if (res.error || !res.data) {
			setSave({ kind: 'error', message: res.message ?? 'The save failed.' });
			return;
		}
		setSave({ kind: 'saved', message: 'Saved and re-embedded.' });
		onUpdated(res.data);
	}, [analysis.id, description, name, onUpdated, sql]);

	return (
		<div className="flex flex-col gap-3">
			<label className="flex flex-col gap-1">
				<span className="text-xs font-medium text-heading dark:text-zinc-300">Name</span>
				<input
					className={fieldClassName}
					value={name}
					onChange={(event) => setName(event.target.value)}
				/>
			</label>
			<label className="flex flex-col gap-1">
				<span className="text-xs font-medium text-heading dark:text-zinc-300">
					Description
				</span>
				<textarea
					className={textareaClassName}
					rows={4}
					value={description}
					onChange={(event) => setDescription(event.target.value)}
				/>
				<span className="text-[11px] text-secondary dark:text-zinc-500">
					This description tells the model <em>when</em> to reach for this pattern, so
					write the question shape it answers, not a summary of the SQL.
				</span>
			</label>
			<label className="flex flex-col gap-1">
				<span className="text-xs font-medium text-heading dark:text-zinc-300">SQL</span>
				<textarea
					className={textareaClassName}
					rows={12}
					value={sql}
					onChange={(event) => setSql(event.target.value)}
				/>
			</label>
			<div className="flex flex-wrap items-center gap-3">
				<Button
					theme={ButtonTheme.Secondary}
					size={Size.REGULAR}
					loading={validating}
					disabled={validating}
					onClick={() => void validate()}
				>
					Validate SQL
				</Button>
				<Button
					theme={ButtonTheme.Primary}
					size={Size.REGULAR}
					disabled={!dirty || save.kind === 'saving'}
					loading={save.kind === 'saving'}
					onClick={() => void saveAnalysis()}
				>
					Save analysis
				</Button>
				<SaveNote state={save} />
			</div>
		</div>
	);
};

const ReindexPanel = () => {
	const [preview, setPreview] = useState<ReindexReport | null>(null);
	const [result, setResult] = useState<ReindexReport | null>(null);
	const [busy, setBusy] = useState<'preview' | 'run' | null>(null);
	const [error, setError] = useState<string | null>(null);

	const runPreview = useCallback(async () => {
		setBusy('preview');
		setError(null);
		const res = await semanticYamlApi.reindexSemanticIndex({ dryRun: true });
		setBusy(null);
		if (res.error || res.total == null) {
			setError(res.message ?? 'The preview failed.');
			setPreview(null);
			return;
		}
		setPreview(res);
	}, []);

	const runRebuild = useCallback(async () => {
		setBusy('run');
		setError(null);
		const res = await semanticYamlApi.reindexSemanticIndex({ dryRun: false });
		setBusy(null);
		if (res.error || res.total == null) {
			setError(
				res.message ??
					'The rebuild failed. On a deployment older than this panel the endpoint does not exist yet.',
			);
			setResult(null);
			return;
		}
		setResult(res);
	}, []);

	const report = result ?? preview;
	const labelEntries = Object.entries(report?.labels ?? {}).filter(
		(entry): entry is [string, number] => typeof entry[1] === 'number' && entry[1] > 0,
	);

	return (
		<section className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
			<div className="flex flex-col gap-1">
				<span className="text-sm font-medium text-heading dark:text-zinc-200">
					Rebuild the semantic index
				</span>
				<p className="max-w-3xl text-xs text-secondary dark:text-zinc-400">
					Saving an object re-embeds it, so this is for what saving cannot reach: rows
					embedded before a field existed, or a vector store that drifted. It re-embeds
					before deleting, so a failing embedding endpoint leaves the current index in
					place.
				</p>
			</div>
			<div className="flex flex-wrap items-center gap-2">
				<Button
					theme={ButtonTheme.Secondary}
					size={Size.SMALL}
					loading={busy === 'preview'}
					disabled={busy != null}
					onClick={() => void runPreview()}
				>
					Preview
				</Button>
				<Button
					theme={ButtonTheme.Primary}
					size={Size.SMALL}
					loading={busy === 'run'}
					disabled={busy != null || preview == null}
					onClick={() => void runRebuild()}
				>
					Rebuild now
				</Button>
				{preview == null ? (
					<span className="text-xs text-secondary dark:text-zinc-500">
						Preview first — the rebuild button stays disabled until it knows what it
						would write.
					</span>
				) : null}
			</div>
			{error ? <p className="text-xs text-red-600 dark:text-red-400">{error}</p> : null}
			{report ? (
				<div className="flex flex-col gap-1.5 rounded-lg bg-zinc-50 px-3 py-2 dark:bg-zinc-950/50">
					<p className="text-xs text-body dark:text-zinc-300">
						{report.total.toLocaleString()} row(s)
						{report.dry_run ? ' would be rewritten' : ' rewritten'} ·{' '}
						{report.elapsed_ms.toLocaleString()} ms
						{result && result.rows_before != null && result.rows_after != null ? (
							<>
								{' · '}
								index rows {result.rows_before.toLocaleString()} →{' '}
								{result.rows_after.toLocaleString()}
							</>
						) : null}
					</p>
					{labelEntries.length > 0 ? (
						<p className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-secondary dark:text-zinc-400">
							{labelEntries.map(([label, count]) => (
								<span key={label}>
									{label}: {count}
								</span>
							))}
						</p>
					) : null}
					{report.skipped ? (
						<p className="text-[11px] text-amber-700 dark:text-amber-400">
							Skipped — {report.reason ?? 'the run could not proceed.'} Nothing was
							written.
						</p>
					) : null}
					{result &&
					result.rows_before != null &&
					result.rows_after != null &&
					result.rows_after < result.rows_before ? (
						<p className="text-[11px] text-amber-700 dark:text-amber-400">
							The index holds fewer rows than before. That is expected when duplicate
							rows existed, but worth confirming.
						</p>
					) : null}
				</div>
			) : null}
		</section>
	);
};

export const EditSemantics = () => {
	const [mode, setMode] = useState<Mode>('terms');
	const [query, setQuery] = useState('');

	const [termItems, setTermItems] = useState<Term[]>([]);
	const [termsLoading, setTermsLoading] = useState(true);
	const [termsError, setTermsError] = useState<string | null>(null);
	const [selectedTermId, setSelectedTermId] = useState<string | null>(null);

	const [analysisItems, setAnalysisItems] = useState<CustomAnalysis[]>([]);
	const [analysesLoading, setAnalysesLoading] = useState(true);
	const [analysesError, setAnalysesError] = useState<string | null>(null);
	const [selectedAnalysisId, setSelectedAnalysisId] = useState<string | null>(null);

	useEffect(() => {
		let active = true;
		void (async () => {
			setTermsLoading(true);
			const res = await termsApi.list({ query: query || undefined, limit: 100 });
			if (!active) return;
			if (res.error) {
				setTermsError(res.message ?? 'Failed to load terms.');
				setTermItems([]);
			} else {
				setTermsError(null);
				setTermItems(res.terms ?? []);
			}
			setTermsLoading(false);
		})();
		return () => {
			active = false;
		};
	}, [query]);

	useEffect(() => {
		let active = true;
		void (async () => {
			setAnalysesLoading(true);
			const res = await analyses.list({ query: query || undefined, limit: 100 });
			if (!active) return;
			if (res.error) {
				setAnalysesError(res.message ?? 'Failed to load analyses.');
				setAnalysisItems([]);
			} else {
				setAnalysesError(null);
				setAnalysisItems(res.data ?? []);
			}
			setAnalysesLoading(false);
		})();
		return () => {
			active = false;
		};
	}, [query]);

	const selectedTerm = termItems.find((item) => item.id === selectedTermId) ?? null;
	const selectedAnalysis = analysisItems.find((item) => item.id === selectedAnalysisId) ?? null;

	const updateTerm = useCallback((next: Term) => {
		setTermItems((current) => current.map((item) => (item.id === next.id ? next : item)));
	}, []);

	const updateAnalysis = useCallback((next: CustomAnalysis) => {
		setAnalysisItems((current) => current.map((item) => (item.id === next.id ? next : item)));
	}, []);

	const loading = mode === 'terms' ? termsLoading : analysesLoading;
	const error = mode === 'terms' ? termsError : analysesError;
	const items = mode === 'terms' ? termItems : analysisItems;
	const selectedId = mode === 'terms' ? selectedTermId : selectedAnalysisId;

	return (
		<div className="flex flex-col gap-5">
			<section className="flex flex-col gap-1 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
				<span className="text-sm font-medium text-heading dark:text-zinc-200">
					The catalog is the source of truth
				</span>
				<p className="max-w-3xl text-xs text-secondary dark:text-zinc-400">
					These edits write to the catalog and re-embed the affected rows. They are not
					written to the Ossie export: that conversion is one-way, and a description
					edited there is discarded on import.
				</p>
			</section>

			<div className="flex flex-wrap items-center gap-2">
				<div role="tablist" aria-label="Semantic objects" className="flex gap-1">
					{(
						[
							['terms', 'Terms'],
							['analyses', 'Custom analyses'],
						] as [Mode, string][]
					).map(([value, label]) => (
						<button
							key={value}
							role="tab"
							type="button"
							aria-selected={mode === value}
							onClick={() => setMode(value)}
							className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
								mode === value
									? 'bg-[#76b900]/15 font-medium text-heading ring-1 ring-[#76b900]/30 dark:text-zinc-100'
									: 'text-secondary hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800'
							}`}
						>
							{label}
						</button>
					))}
				</div>
				<SearchInput
					value={query}
					onChange={setQuery}
					placeholder="Filter by name"
					className="w-full sm:w-64"
					aria-label="Filter semantic objects"
				/>
			</div>

			<div className="grid min-h-[36rem] grid-cols-1 gap-4 lg:grid-cols-[20rem_1fr]">
				<section className="flex min-h-0 flex-col rounded-xl border border-zinc-200 bg-white dark:border-zinc-700 dark:bg-zinc-900">
					<div className="border-b border-zinc-200 px-3 py-2 text-xs text-secondary dark:border-zinc-700 dark:text-zinc-400">
						{loading ? 'Loading…' : `${items.length} object(s)`}
					</div>
					<div className="min-h-0 flex-1 overflow-y-auto p-2">
						{error ? (
							<p className="px-2 py-1 text-xs text-red-600 dark:text-red-400">
								{error}
							</p>
						) : null}
						{loading ? (
							<SkeletonRows rows={6} />
						) : items.length === 0 ? (
							<p className="px-2 py-1 text-xs text-secondary dark:text-zinc-400">
								Nothing matches that filter.
							</p>
						) : (
							<ul className="flex flex-col gap-1">
								{items.map((item) => (
									<li key={item.id}>
										<button
											type="button"
											onClick={() =>
												mode === 'terms'
													? setSelectedTermId(item.id)
													: setSelectedAnalysisId(item.id)
											}
											className={listRowClassName(selectedId === item.id)}
										>
											<span className="block truncate text-sm text-body dark:text-zinc-200">
												{item.name}
											</span>
											<span className="block truncate text-[11px] text-secondary dark:text-zinc-500">
												{item.description || 'No description'}
											</span>
										</button>
									</li>
								))}
							</ul>
						)}
					</div>
				</section>

				<section className="flex min-h-0 flex-col overflow-y-auto rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
					{mode === 'terms' ? (
						selectedTerm ? (
							<TermEditor
								key={selectedTerm.id}
								term={selectedTerm}
								onUpdated={updateTerm}
							/>
						) : (
							<EmptySelection noun="term" />
						)
					) : selectedAnalysis ? (
						<AnalysisEditor
							key={selectedAnalysis.id}
							analysis={selectedAnalysis}
							onUpdated={updateAnalysis}
						/>
					) : (
						<EmptySelection noun="analysis" />
					)}
				</section>
			</div>

			<ReindexPanel />
		</div>
	);
};

const EmptySelection = ({ noun }: { noun: string }) => (
	<div className="flex flex-1 flex-col items-center justify-center gap-2 py-16 text-center">
		<Icon name={IconName.Terms} className="h-6 w-6 text-zinc-300 dark:text-zinc-600" />
		<p className="text-sm text-secondary dark:text-zinc-400">
			Pick a {noun} on the left to edit its description.
		</p>
	</div>
);
