"""Material examples are model-output fixtures, never application dictionaries."""
from uuid import uuid4

import pytest

from app.rag.query_rewrite import QueryRewriter
from phase13_m2_support import FakeProvider, settings_for
from test_conversation_rewrite import history, result


@pytest.mark.parametrize("material", ["WCB", "QT500-7", "HT250", "未收录材料甲"])
def test_natural_material_rewrite_is_accepted_without_extractive_edits(material):
    context = history(f"{material}的力学性能如何？", "历史助手曾给出温度700℃，这不是用户条件。")
    response = result("rewritten", f"{material}的化学成分包括哪些元素？",
        history_scope="recent", referenced_message_ids=[str(context.messages[0].id)])
    provider = FakeProvider(response)
    details = QueryRewriter(provider, settings_for()).understand("它的成分含有什么？", uuid4(), context).details
    assert details.result.standalone_query == response["standalone_query"]
    assert not details.result.resolved_references and len(provider.calls) == 1
    assert details.history_message_ids == context.message_ids


def test_new_topic_is_model_standalone_and_input_history_remains_auditable():
    context = history("WCB在700℃下的力学性能如何？")
    provider = FakeProvider(result(query="QT500-7的化学成分"))
    details = QueryRewriter(provider, settings_for()).understand("QT500-7的化学成分是什么？", uuid4(), context).details
    assert details.result.decision == "standalone" and len(provider.calls) == 1
    assert details.history_message_ids == context.message_ids
