# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The scoring population must follow the question, not the model's mood.

Asked "which customers are most likely to churn in the next 90 days" twice, the
same setup once scored 568 customers and once 499: the second time the model
attached an entity-selection block filtering on five industry values the
question never mentioned. Every score, every rank and the denominator in the
answer move with that population, so an invented filter is not a detail.

These tests pin the guard: a block is honoured only when the question accounts
for it (a value or column it filters on is named there) or when the PQL already
declares the subset in its own ``FOR EACH ... WHERE``. Otherwise it is dropped
and everyone is scored, which is what an unscoped question asks for.
"""

from __future__ import annotations

import pytest

from auto_ontology.retrieval.kumo.pql_gen import drop_unasked_entity_scope

CHURN_QUESTION = "哪些客户最可能在未来 90 天内流失？请列出风险最高的 20 家。"
CHURN_PQL = "PREDICT COUNT(invoices.*, 0, 90, days) = 0 FOR EACH customers.customer_id"

# Verbatim from the run that scored 526 (499 after the never-active filter).
OBSERVED_BLOCK = (
    "SELECT customer_id FROM customers "
    "WHERE industry IN ('食品加工', '连锁餐饮', '商超零售', '建材批发', '电子制造') "
    "-- example subset filter; omit if scoring all customers"
)


def test_the_observed_invented_filter_is_dropped() -> None:
    assert drop_unasked_entity_scope(OBSERVED_BLOCK, CHURN_QUESTION, CHURN_PQL) is None


def test_no_block_stays_none() -> None:
    assert drop_unasked_entity_scope(None, CHURN_QUESTION, CHURN_PQL) is None


def test_a_subset_the_question_names_survives() -> None:
    question = "食品加工行业里，哪些客户最可能在未来 90 天内流失？"
    block = "SELECT customer_id FROM customers WHERE industry = '食品加工'"
    assert drop_unasked_entity_scope(block, question, CHURN_PQL) == block


def test_one_named_value_is_enough_among_several() -> None:
    question = "食品加工和医药制造的客户里，谁最可能流失？"
    block = (
        "SELECT customer_id FROM customers WHERE industry IN ('食品加工', '医药制造')"
    )
    assert drop_unasked_entity_scope(block, question, CHURN_PQL) == block


def test_a_column_the_question_names_also_grounds_it() -> None:
    question = "Which industry churns most in the next 90 days?"
    block = "SELECT customer_id FROM customers WHERE industry = 'Retail'"
    assert drop_unasked_entity_scope(block, question, CHURN_PQL) == block


def test_a_pql_declared_subset_keeps_the_block() -> None:
    # The value is an id, so no literal from the block appears in the question;
    # the PQL itself says the population is a subset, which is the ground here.
    question = "华东区的客户里，谁最可能在未来 90 天内流失？"
    pql = (
        "PREDICT COUNT(invoices.*, 0, 90, days) = 0 "
        "FOR EACH customers.customer_id WHERE customers.region_id = 3"
    )
    block = "SELECT customer_id FROM customers WHERE region_id = 3"
    assert drop_unasked_entity_scope(block, question, pql) == block


def test_a_comment_cannot_ground_the_filter_beside_it() -> None:
    # The observed block carried "-- example subset filter; omit if scoring all
    # customers". Prose must not certify the SQL it annotates.
    question = "哪些客户最可能在未来 90 天内流失？"
    block = (
        "SELECT customer_id FROM customers WHERE industry IN ('食品加工') "
        "-- 客户 subset filter for customers who churn"
    )
    assert drop_unasked_entity_scope(block, question, CHURN_PQL) is None


def test_a_block_that_filters_nothing_is_dropped() -> None:
    # No WHERE, but a LIMIT still makes the population arbitrary.
    block = "SELECT customer_id FROM customers LIMIT 200"
    assert drop_unasked_entity_scope(block, CHURN_QUESTION, CHURN_PQL) is None


def test_an_empty_question_grounds_nothing() -> None:
    assert drop_unasked_entity_scope(OBSERVED_BLOCK, "", CHURN_PQL) is None


def test_a_one_letter_value_is_not_a_match() -> None:
    # "'s'" is a substring of "customers"; a filter must name something a reader
    # would recognise, not a stray character.
    block = "SELECT customer_id FROM customers WHERE segment = 's'"
    question = "哪些 customers 最可能流失？"
    assert drop_unasked_entity_scope(block, question, CHURN_PQL) is None


@pytest.mark.parametrize(
    "block",
    [
        "SELECT customer_id FROM customers WHERE industry = '食品加工'",
        "select CUSTOMER_ID from customers where INDUSTRY in ('食品加工')",
    ],
)
def test_matching_ignores_case(block: str) -> None:
    assert (
        drop_unasked_entity_scope(block, "食品加工行业的客户谁最可能流失？", "")
        == block
    )
