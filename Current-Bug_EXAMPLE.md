# Current Bug

## Symptom
A test entry kept reappearing in `saved_entries`.

## Confirmed cause
`tests/integration/test_csrf.py` was posting `/entries/saved` against the real meta DB, so each pytest run reinserted `example.com/feed.xml / x1`.

## Status
- Root cause found.
- Fix started: redirect integration tests to temp DBs.
- Risky areas: shared test fixtures, app DB path wiring.

## Working hypothesis
The test harness still has at least one path pointing at production state.

## Next step
Finish DB isolation in the integration fixture and rerun the suite.

## Verify
- [ ] Run affected tests.
- [ ] Confirm `saved_entries` stays clean.
- [ ] Confirm `example.com/feed.xml / x1` does not return.
