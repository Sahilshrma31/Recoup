"""The MCP surface must stay read-only.

The value of this server is that a merchant can ask questions in natural
language. Its safety property is that asking a question can never become
taking an action -- so the test that matters is the one asserting no tool
here can move money.
"""
from __future__ import annotations

import pytest

from app import mcp_server


@pytest.mark.asyncio
async def test_every_tool_is_declared_read_only():
    for tool in await mcp_server.server.list_tools():
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.read_only_hint is True, f"{tool.name} is not read-only"
        assert tool.annotations.destructive_hint is False


@pytest.mark.asyncio
async def test_no_tool_can_move_money():
    """A name-level guard against someone adding an action tool here later.

    Executing belongs behind the API's policy engine and audit trail. If a
    recover/retry/approve tool ever appears on this server, that guarantee is
    gone and this test should fail loudly.
    """
    forbidden = {
        "recover", "retry", "approve", "execute", "stop", "refund",
        "create_payment_link", "create_order", "capture", "payout",
    }
    names = {tool.name for tool in await mcp_server.server.list_tools()}
    assert not (names & forbidden)


@pytest.mark.asyncio
async def test_tools_are_documented_for_a_model_to_choose_between():
    """Descriptions are the model's only routing signal -- they must be real."""
    for tool in await mcp_server.server.list_tools():
        assert tool.description and len(tool.description) > 60, tool.name


@pytest.mark.asyncio
async def test_expected_tools_are_present():
    names = {tool.name for tool in await mcp_server.server.list_tools()}
    assert names == {
        "get_transaction", "explain_decision", "recovery_metrics",
        "list_at_risk", "failure_breakdown",
    }


class TestPayloads:
    """Tools are queried against a seeded database in `db`-less fashion, so we
    only assert the shape they return for a miss -- the happy path is covered
    end-to-end by the stdio probe in the README."""

    def test_missing_transaction_reports_not_found(self, monkeypatch, db):
        monkeypatch.setattr(mcp_server, "SessionLocal", lambda: db)
        result = mcp_server.get_transaction("pay_does_not_exist")
        assert result["found"] is False

    def test_missing_decision_explains_itself(self, monkeypatch, db):
        monkeypatch.setattr(mcp_server, "SessionLocal", lambda: db)
        result = mcp_server.explain_decision("pay_never_analysed")
        assert result["found"] is False
        assert "not analysed" in result["note"]

    def test_customer_contact_details_are_not_exposed(
        self, monkeypatch, db, make_transaction
    ):
        """An ops view needs history, not a phone number."""
        txn = make_transaction()
        db.commit()
        monkeypatch.setattr(mcp_server, "SessionLocal", lambda: db)

        result = mcp_server.get_transaction(txn.id)
        assert result["found"] is True
        assert set(result["customer"]) == {
            "id", "name", "successful_payments", "failed_payments",
            "success_rate", "lifetime_value_rupees", "opted_out",
        }
