"""
Test VariablesManager with LangGraph-like state updates.

This simulates how LangGraph updates state objects to ensure
VariablesManager works correctly in that context.
"""

import json

import pytest

pytest.importorskip("langchain_openai", reason="langchain_openai not installed")

from cuga.backend.cuga_graph.state.agent_state import AgentState, VariablesManager


class TestVariablesManagerLangGraphIntegration:
    """Test VariablesManager behavior with LangGraph-style state management."""

    def setup_method(self):
        """Reset VariablesManager before each test."""
        VariablesManager().reset()

    def teardown_method(self):
        """Clean up after each test."""
        VariablesManager().reset()

    def test_state_updates_preserve_variable_access(self):
        """Test that updating state dict doesn't break variable access."""
        # Initial state
        state1 = AgentState(input="initial query", url="http://example.com", final_answer="")

        # Add variables through state1
        state1.variables_manager.add_variable("important_data", "data_var")

        # Simulate LangGraph updating state (creates new dict)
        state1_dict = state1.model_dump()
        state1_dict["final_answer"] = "Updated answer"
        state1_dict["next"] = "NextNode"

        # Create new state from updated dict (simulates LangGraph state flow)
        state2 = AgentState(**state1_dict)

        # Variables should still be accessible (singleton persists)
        assert state2.variables_manager.get_variable("data_var") == "important_data"
        assert state2.final_answer == "Updated answer"
        assert state2.next == "NextNode"

    def test_multi_node_state_flow(self):
        """Simulate multiple node executions with state updates."""
        # Node 1: API Planner
        state = AgentState(input="fetch user data", url="http://api.com")
        state.variables_manager.add_variable({"user_id": 123}, "user_input")

        # Update state after Node 1
        state_dict = state.model_dump()
        state_dict["next"] = "CodeAgent"
        state_dict["current_app"] = "api_service"
        state = AgentState(**state_dict)

        # Node 2: Code Agent
        user_data = state.variables_manager.get_variable("user_input")
        assert user_data == {"user_id": 123}

        # Code agent adds result
        state.variables_manager.add_variable(
            {"user_id": 123, "name": "John", "email": "john@example.com"}, "user_data"
        )

        # Update state after Node 2
        state_dict = state.model_dump()
        state_dict["next"] = "FinalAnswer"
        state = AgentState(**state_dict)

        # Node 3: Final Answer
        final_data = state.variables_manager.get_variable("user_data")
        assert final_data["name"] == "John"
        assert state.variables_manager.get_variable_count() == 2

    def test_state_json_serialization(self):
        """Test that state can be JSON serialized (important for persistence)."""
        state = AgentState(input="test query", url="http://example.com", final_answer="test answer")

        # Add variables
        state.variables_manager.add_variable([1, 2, 3], "numbers")

        # Serialize to JSON (simulates checkpointing)
        state_dict = state.model_dump()
        state_json = json.dumps(state_dict)

        # Deserialize from JSON
        recovered_dict = json.loads(state_json)
        recovered_state = AgentState(**recovered_dict)

        # State fields should be recovered
        assert recovered_state.input == "test query"
        assert recovered_state.final_answer == "test answer"

        # Variables should still be accessible (singleton)
        assert recovered_state.variables_manager.get_variable("numbers") == [1, 2, 3]

    def test_parallel_branch_execution(self):
        """Test state handling in parallel branches (LangGraph feature)."""
        # Initial state
        base_state = AgentState(input="parallel task", url="http://example.com")
        base_state.variables_manager.add_variable("shared_context", "context_var")

        # Simulate two parallel branches
        branch1_dict = base_state.model_dump()
        branch1_dict["next"] = "Branch1Node"
        branch1_state = AgentState(**branch1_dict)

        branch2_dict = base_state.model_dump()
        branch2_dict["next"] = "Branch2Node"
        branch2_state = AgentState(**branch2_dict)

        # Both branches can access shared context
        assert branch1_state.variables_manager.get_variable("context_var") == "shared_context"
        assert branch2_state.variables_manager.get_variable("context_var") == "shared_context"

        # Branch 1 adds a variable
        branch1_state.variables_manager.add_variable("branch1_result", "result1")

        # Branch 2 can see it immediately (singleton)
        assert branch2_state.variables_manager.get_variable("result1") == "branch1_result"

    def test_state_reset_between_sessions(self):
        """Test handling of different sessions with variable reset."""
        # Session 1
        session1_state = AgentState(input="session 1 query", url="http://s1.com")
        session1_state.variables_manager.add_variable("session1_data", "s1_var")

        # Complete session 1
        assert session1_state.variables_manager.get_variable("s1_var") == "session1_data"

        # Reset between sessions (simulate new thread)
        VariablesManager().reset()

        # Session 2
        session2_state = AgentState(input="session 2 query", url="http://s2.com")

        # Old variables should not exist
        assert session2_state.variables_manager.get_variable("s1_var") is None
        assert session2_state.variables_manager.get_variable_count() == 0

        # Add new session variables
        session2_state.variables_manager.add_variable("session2_data", "s2_var")
        assert session2_state.variables_manager.get_variable_count() == 1

    def test_state_with_optional_fields(self):
        """Test state with many optional fields (common in LangGraph)."""
        # Minimal state
        state1 = AgentState(input="test", url="http://test.com")
        state1.variables_manager.add_variable("data", "var1")

        # Dump and reload
        state1_dict = state1.model_dump()
        state2 = AgentState(**state1_dict)

        # Optional fields should be None/default
        assert state2.current_app is None
        assert state2.final_answer == ""
        assert state2.next == ""

        # But variables should work
        assert state2.variables_manager.get_variable("var1") == "data"

    def test_state_partial_updates(self):
        """Test partial state updates (LangGraph only updates changed fields)."""
        # Initial state with many fields
        state = AgentState(
            input="original query",
            url="http://original.com",
            current_app="app1",
            final_answer="",
            next="Node1",
        )
        state.variables_manager.add_variable("original_data", "data_var")

        # Partial update (only some fields change)
        state_dict = state.model_dump()
        state_dict["next"] = "Node2"  # Only update next
        state_dict["current_app"] = "app2"  # Only update current_app
        # Other fields remain the same

        new_state = AgentState(**state_dict)

        # Updated fields should change
        assert new_state.next == "Node2"
        assert new_state.current_app == "app2"

        # Unchanged fields should remain
        assert new_state.input == "original query"
        assert new_state.url == "http://original.com"

        # Variables should persist
        assert new_state.variables_manager.get_variable("data_var") == "original_data"

    def test_variables_survive_state_recreation(self):
        """Test that variables persist even when state is recreated multiple times."""
        original_state = AgentState(input="test", url="http://test.com")
        original_state.variables_manager.add_variable("persistent_value", "persistent_var")

        # Recreate state 10 times (simulating many node transitions)
        current_state = original_state
        for i in range(10):
            state_dict = current_state.model_dump()
            state_dict["next"] = f"Node{i}"
            current_state = AgentState(**state_dict)

            # Variable should always be accessible
            assert current_state.variables_manager.get_variable("persistent_var") == "persistent_value"

            # Add new variable each iteration
            current_state.variables_manager.add_variable(f"value_{i}", f"var_{i}")

        # All variables should exist
        assert current_state.variables_manager.get_variable_count() == 11  # original + 10 new


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
