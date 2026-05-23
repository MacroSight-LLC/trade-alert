## Documentation Pull Request

### Related Issue
<!-- Link to related issue if exists -->
Closes #

### Description
<!-- Brief description of what documentation changes are included -->


### Type of Changes
- [ ] Documentation update
- [ ] New documentation
- [ ] Documentation fix/correction
- [ ] README update
- [ ] API documentation
- [ ] Code comments/docstrings

### Changes Made
<!-- List the specific documentation changes -->
- 
- 
- 

### Verification
- [ ] I have reviewed the documentation for accuracy
- [ ] I have checked for spelling and grammar errors
- [ ] Links and references are working correctly
- [ ] Code examples (if any) have been tested

## Security review
- [ ] No secrets in code, YAML, or committed env files
- [ ] Workflow `code:` steps respect `workflow_sandbox.py` (no `mcp_call`, `os`, `redis`, `httpx`)
- [ ] Execution bridge changes tested with dry-run / paper mode
- [ ] `detect-secrets` baseline updated if new false positives were audited
- [ ] `ruff check .` and mypy paths for touched modules are clean

### Checklist
- [ ] My documentation follows the project's documentation style
- [ ] I have performed a self-review of my changes
