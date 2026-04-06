"""
Test VariablesManager integration with AgentState.

This test verifies:
1. VariablesManager is a singleton (shared across all AgentState instances)
2. Variables are accessible from any AgentState instance
3. AgentState serialization/deserialization works correctly
4. Variables persist across state dumps and loads
"""

import pytest

pytest.importorskip("langchain_openai", reason="langchain_openai not installed")

from cuga.backend.cuga_graph.state.agent_state import AgentState, VariablesManager


class TestVariablesManagerWithState:
    """Test suite for VariablesManager accessed through AgentState."""

    def setup_method(self):
        """Reset VariablesManager before each test."""
        VariablesManager().reset()

    def teardown_method(self):
        """Clean up after each test."""
        VariablesManager().reset()

    def test_singleton_behavior(self):
        """Test that VariablesManager is a singleton shared across AgentState instances."""
        # Create two different AgentState instances
        state1 = AgentState(input="test query 1", url="http://example1.com")
        state2 = AgentState(input="test query 2", url="http://example2.com")

        # Add variable through state1
        var_name = state1.variables_manager.add_variable("value from state1", "var1")
        assert var_name == "var1"

        # Verify it's accessible from state2 (singleton behavior)
        value_from_state2 = state2.variables_manager.get_variable("var1")
        assert value_from_state2 == "value from state1"

        # Verify both states access the same singleton instance
        assert id(state1.variables_manager) == id(state2.variables_manager)
        assert state1.variables_manager.get_variable_count() == 1
        assert state2.variables_manager.get_variable_count() == 1

    def test_variables_not_in_state_dump(self):
        """Test that VariablesManager data is NOT stored in state serialization."""
        state = AgentState(input="test query", url="http://example.com")

        # Add some variables
        state.variables_manager.add_variable("test_value_1", "var1", "First variable")
        state.variables_manager.add_variable([1, 2, 3], "var2", "Second variable")
        state.variables_manager.add_variable({"key": "value"}, "var3", "Third variable")

        # Serialize the state
        state_dict = state.model_dump()

        # Variables should NOT be in the serialized state
        # (they're in the singleton, not the state itself)
        assert "variables" not in state_dict
        assert "variables_manager" not in state_dict

        # But the state's other fields should be there
        assert state_dict["input"] == "test query"
        assert state_dict["url"] == "http://example.com"

    def test_state_serialization_and_deserialization(self):
        """Test that AgentState can be serialized and deserialized correctly."""
        # Create initial state
        state1 = AgentState(
            input="original query",
            url="http://example.com",
            current_app="test_app",
            final_answer="test answer",
        )

        # Add variables through state1
        state1.variables_manager.add_variable("shared_value", "shared_var")

        # Serialize state1
        state1_dict = state1.model_dump()

        # Create new state from serialized data
        state2 = AgentState(**state1_dict)

        # Verify state fields are preserved
        assert state2.input == "original query"
        assert state2.url == "http://example.com"
        assert state2.current_app == "test_app"
        assert state2.final_answer == "test answer"

        # Verify variables are still accessible (singleton persists)
        assert state2.variables_manager.get_variable("shared_var") == "shared_value"

    def test_variable_isolation_with_reset(self):
        """Test that variables can be isolated using reset between different sessions."""
        # Session 1
        state1 = AgentState(input="session 1", url="http://session1.com")
        state1.variables_manager.add_variable("value1", "var1")
        state1.variables_manager.add_variable("value2", "var2")

        assert state1.variables_manager.get_variable_count() == 2
        assert state1.variables_manager.get_variable_names() == ["var1", "var2"]

        # Reset to start new session
        state1.variables_manager.reset()

        # Session 2 with different state
        state2 = AgentState(input="session 2", url="http://session2.com")

        # Old variables should be gone
        assert state2.variables_manager.get_variable_count() == 0
        assert state2.variables_manager.get_variable("var1") is None

        # Add new variables
        state2.variables_manager.add_variable("new_value", "new_var")
        assert state2.variables_manager.get_variable_count() == 1
        assert state2.variables_manager.get_variable("new_var") == "new_value"

    def test_multiple_states_concurrent_access(self):
        """Test multiple AgentState instances accessing variables concurrently."""
        # Create multiple states
        states = [AgentState(input=f"query {i}", url=f"http://example{i}.com") for i in range(5)]

        # Each state adds a variable (all go to the same singleton)
        for i, state in enumerate(states):
            state.variables_manager.add_variable(f"value_{i}", f"var_{i}")

        # All states should see all variables
        for state in states:
            assert state.variables_manager.get_variable_count() == 5
            for i in range(5):
                assert state.variables_manager.get_variable(f"var_{i}") == f"value_{i}"

    def test_complex_variable_types(self):
        """Test that complex data types can be stored and retrieved."""
        state = AgentState(input="test", url="http://example.com")

        # Test various data types
        test_data = {
            "string_var": "hello world",
            "int_var": 42,
            "float_var": 3.14,
            "list_var": [1, 2, 3, 4, 5],
            "dict_var": {"nested": {"key": "value"}, "list": [1, 2]},
            "bool_var": True,
            "none_var": None,
        }

        # Add all variables
        for var_name, var_value in test_data.items():
            state.variables_manager.add_variable(var_value, var_name)

        # Verify all can be retrieved correctly
        for var_name, expected_value in test_data.items():
            actual_value = state.variables_manager.get_variable(var_name)
            assert actual_value == expected_value, f"Mismatch for {var_name}"

    def test_variable_metadata_persistence(self):
        """Test that variable metadata persists across state instances."""
        state1 = AgentState(input="test1", url="http://example1.com")

        # Add variable with description
        state1.variables_manager.add_variable(
            value="important data", name="important_var", description="This is important data for the task"
        )

        # Access from different state
        state2 = AgentState(input="test2", url="http://example2.com")

        # Get metadata
        metadata = state2.variables_manager.get_variable_metadata("important_var")
        assert metadata is not None
        assert metadata.value == "important data"
        assert metadata.description == "This is important data for the task"
        assert metadata.type == "str"

    def test_variables_summary_from_different_states(self):
        """Test getting variables summary from different AgentState instances."""
        state1 = AgentState(input="test1", url="http://example1.com")
        state2 = AgentState(input="test2", url="http://example2.com")

        # Add variables from state1
        state1.variables_manager.add_variable([1, 2, 3], "numbers", "List of numbers")
        state1.variables_manager.add_variable("test", "text", "Some text")

        # Get summary from state2
        summary = state2.variables_manager.get_variables_summary()

        # Verify summary contains both variables
        assert "numbers" in summary
        assert "text" in summary
        assert "List of numbers" in summary
        assert "Some text" in summary

    def test_state_with_last_n_variables(self):
        """Test accessing last N variables through different states."""
        state1 = AgentState(input="test1", url="http://example1.com")

        # Add several variables
        for i in range(10):
            state1.variables_manager.add_variable(f"value_{i}", f"var_{i}")

        # Access last 3 from different state
        state2 = AgentState(input="test2", url="http://example2.com")

        last_3_names = state2.variables_manager.get_last_n_variable_names(3)
        assert last_3_names == ["var_7", "var_8", "var_9"]

        # Get summary of last 3
        summary = state2.variables_manager.get_variables_summary(last_n=3)
        assert "var_7" in summary
        assert "var_8" in summary
        assert "var_9" in summary
        assert "var_0" not in summary  # Earlier variables not in summary

    def test_state_model_dump_exclude_none(self):
        """Test that model_dump works with various options."""
        state = AgentState(input="test", url="http://example.com")
        state.variables_manager.add_variable("test_value", "test_var")

        # Test different dump options
        dump_normal = state.model_dump()
        dump_exclude_none = state.model_dump(exclude_none=True)
        dump_exclude_unset = state.model_dump(exclude_unset=True)

        # All should work without errors
        assert isinstance(dump_normal, dict)
        assert isinstance(dump_exclude_none, dict)
        assert isinstance(dump_exclude_unset, dict)

        # Variables still accessible after dumps
        assert state.variables_manager.get_variable("test_var") == "test_value"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
