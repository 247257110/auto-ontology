// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useState } from 'react';
import { BrowseExport } from './BrowseExport';
import { EditSemantics } from './EditSemantics';

/**
 * Semantic YAML workbench: the export is read-only, the semantics are editable.
 *
 * Those are two different jobs and the split is the point. The Ossie export can
 * only be *read* — its conversion is one-way, so an edit made to it is
 * discarded on import — while the objects it was generated from live in the
 * catalog and are edited through routes that re-embed them. Putting both behind
 * one tab strip makes that boundary visible instead of tempting.
 */

type Tab = 'browse' | 'edit';

const TAB_LABEL: Record<Tab, string> = {
	browse: 'Browse export',
	edit: 'Edit semantics',
};

export const SemanticYamlView = () => {
	const [tab, setTab] = useState<Tab>('browse');

	return (
		<div className="flex flex-col gap-5">
			<header className="flex flex-col gap-1">
				<h1 className="text-lg font-semibold text-heading dark:text-zinc-100">
					Semantic YAML
				</h1>
				<p className="max-w-3xl text-sm text-secondary dark:text-zinc-400">
					Inspect what the Apache Ossie export contains — including what it silently
					leaves out — and edit the semantic objects it was generated from. The export
					itself is read-only: the conversion is one-way, so changes made to those files
					are not read back on import.
				</p>
			</header>

			<div role="tablist" aria-label="Semantic YAML sections" className="flex gap-1">
				{(['browse', 'edit'] as Tab[]).map((value) => (
					<button
						key={value}
						role="tab"
						type="button"
						aria-selected={tab === value}
						onClick={() => setTab(value)}
						className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
							tab === value
								? 'bg-[#76b900]/15 font-medium text-heading ring-1 ring-[#76b900]/30 dark:text-zinc-100'
								: 'text-secondary hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800'
						}`}
					>
						{TAB_LABEL[value]}
					</button>
				))}
			</div>

			{tab === 'browse' ? <BrowseExport /> : <EditSemantics />}
		</div>
	);
};
