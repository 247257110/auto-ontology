// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { requireAdmin } from '@/auth/auth-guards';
import { SemanticYamlView } from './SemanticYamlView';

/**
 * Browse what an Apache Ossie export actually contains, and what it leaves out.
 *
 * Read-only by design: this page proves what the export says so that treating it
 * as the source of truth (or editing it, which the Ossie conversion cannot read
 * back) is a decision someone makes with the evidence in front of them.
 */
const SemanticYamlSettingsPage = async () => {
	await requireAdmin();
	return (
		<main className="min-h-0 min-w-0 flex-1 overflow-y-auto bg-[linear-gradient(180deg,rgba(255,255,255,1)_0%,rgba(250,250,250,0.6)_100%)] px-7 py-6 sm:px-10 sm:py-7 dark:bg-[linear-gradient(180deg,rgba(9,9,11,1)_0%,rgba(24,24,27,0.5)_100%)]">
			<SemanticYamlView />
		</main>
	);
};

export default SemanticYamlSettingsPage;
