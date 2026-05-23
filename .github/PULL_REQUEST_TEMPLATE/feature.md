## Feature Pull Request

### Related Issue
<!-- Link to related issue if exists -->
Closes #

### Description
<!-- Brief description of what this feature adds -->


### Type of Changes
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)

### Testing
- [ ] I have tested this feature locally
- [ ] I have added tests that prove my feature works
- [ ] All new and existing tests passed

### Documentation
- [ ] I have updated the documentation accordingly
- [ ] I have added docstrings to new functions/classes

## Security review
- [ ] No secrets in code, YAML, or committed env files
- [ ] Workflow `code:` steps respect `workflow_sandbox.py` (no `mcp_call`, `os`, `redis`, `httpx`)
- [ ] Execution bridge changes tested with dry-run / paper mode
- [ ] `detect-secrets` baseline updated if new false positives were audited
- [ ] `ruff check .` and mypy paths for touched modules are clean

### Checklist
- [ ] My code follows the code style of this project
- [ ] I have performed a self-review of my own code
- [ ] I have made corresponding changes to the documentation
